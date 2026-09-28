"""Team worker parity: PostgreSQL backend vs SQLite (plan 6.8).

Loads the same corpus through the worker Runtime into a SQLite workspace and into a PostgreSQL
workspace schema, asks the same questions, and compares:

  quality   Recall@5, Recall@10, MRR and nDCG@10 of the gold fact per backend
  parity    top-10 Jaccard and Kendall tau between the backends, for the fused result and for the
            lexical tier alone (FTS5 BM25 vs the PostgreSQL BM25 port)
  latency   p50/p95 of save and recall at each corpus size (1k, 10k, 50k by default)

Acceptance (plan 6.8): Recall@10 >= SQLite - 1.0 pp; lexical top-10 Jaccard >= 0.9; p95 recall at
10k <= 1.5 x SQLite with PostgreSQL on the same host.

The corpus is the organisational-memory benchmark's synthetic company (deterministic from --seed:
department facts with a direct and a paraphrased question each) plus deterministic filler records
that grow the workspace to each size. No LLM, no network besides PostgreSQL.

    export TAM_BENCH_PG_ADMIN_URL=postgresql://postgres:secret@127.0.0.1:5433/postgres?sslmode=disable
    PYTHONPATH=src python benchmarks/pg_parity_bench.py --out benchmarks/results/pg-parity.json

The admin URI (a role allowed to create databases and roles) is read from the environment variable
named by --pg-admin-url-env and never passed on the command line. Every run creates a fresh
database with the prerequisites of docs/TEAM_POSTGRES.md and drops it (and its workspace role)
afterwards. Each backend runs in its own process so neither inherits the other's caches.
"""
import argparse
import json
import math
import os
import random
import secrets
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
ORGBENCH = REPO / "docs" / "benchmarks" / "org-memory-v14-20260925"
WORKSPACE_KEY = "shared"
TOP_K = 10
RECALL_AT = (5, 10)
DEFAULT_SIZES = "1000,10000,50000"
DEFAULT_SEED = 20260925
DEFAULT_SAVE_SAMPLE = 200
DEFAULT_QUERY_SAMPLE = 200
ADMIN_URL_ENV = "TAM_BENCH_PG_ADMIN_URL"
WORKER_URL_ENV = "TAM_BENCH_PG_URL"
PERCENTILES = (50, 95)
ACCEPT_RECALL_DROP_PP = 1.0
ACCEPT_LEXICAL_JACCARD = 0.9
ACCEPT_P95_RATIO = 1.5
ACCEPT_P95_SIZE = 10000
MASTER_KEY_BYTES = 32
CHILD_TIMEOUT_SECONDS = 6 * 60 * 60
FILLER_SUBJECTS = ("The archive job", "Quarterly planning", "The vendor portal", "Badge access", "The staging cluster",
                   "Travel booking", "The design review", "Printer fleet", "The data warehouse", "Office moves",
                   "The mentoring program", "Expense exports", "The status page", "Parking permits", "The wiki")
FILLER_VERBS = ("is reviewed", "gets rotated", "is migrated", "is audited", "is rescheduled", "is paused",
                "is announced", "is documented", "is benchmarked", "is renewed")
FILLER_WHEN = ("every Monday", "twice a month", "before each release", "at quarter end", "after incidents",
               "during onboarding week", "on the first business day", "when budgets change")
FILLER_OWNERS = ("the platform group", "facilities", "the PMO", "internal IT", "the finance desk",
                 "people operations", "the security office", "customer success")


def log(message: str) -> None:
    print(time.strftime("%H:%M:%S"), message, file=sys.stderr, flush=True)


# Corpus


def corpus(seed: int, facts_limit: int | None = None) -> dict:
    sys.path.insert(0, str(ORGBENCH))
    from orgbench import data as dataset

    with tempfile.TemporaryDirectory(prefix="pg-parity-data-") as scratch:
        company = json.loads(dataset.write(seed, Path(scratch)).read_text())
    facts, questions = [], []
    for department, items in company["departments"].items():
        for fact in items:
            facts.append({"key": fact["key"], "content": fact["content"], "project": department})
            questions.extend({"qid": q["qid"], "kind": q["kind"], "question": q["question"], "gold": fact["key"]}
                             for q in fact["questions"])
    for fact in company["shared"]:
        facts.append({"key": fact["key"], "content": fact["content"], "project": "shared"})
    if facts_limit is not None:
        facts = facts[:facts_limit]
        kept = {fact["key"] for fact in facts}
        questions = [question for question in questions if question["gold"] in kept]
    return {"seed": seed, "facts": facts, "questions": questions}


def filler(index: int, rng: random.Random) -> str:
    return (f"{rng.choice(FILLER_SUBJECTS)} {rng.choice(FILLER_VERBS)} {rng.choice(FILLER_WHEN)} by "
            f"{rng.choice(FILLER_OWNERS)}; tracking note F-{index:06d}.")


# Worker process: one backend


def percentiles(samples: list[float]) -> dict:
    if not samples:
        return {}
    ordered = sorted(samples)
    return {f"p{p}": round(ordered[min(len(ordered) - 1, math.ceil(p / 100 * len(ordered)) - 1)], 3)
            for p in PERCENTILES}


def runtime_for(backend: str, scratch: Path):
    import server
    from tam_db.contracts import StoreDatabase
    from team_memory.worker import Runtime

    data_dir = scratch / "workspace"
    server.MEMORY_DIR = data_dir
    database = None
    if backend == "postgres":
        from team_memory.pg_provision import PgProvisioner, PgWorkspaceProvisioner

        url = os.environ[WORKER_URL_ENV]
        instance_id = PgProvisioner(url).bootstrap(str(uuid.uuid4()))
        workspaces = PgWorkspaceProvisioner(url, instance_id, secrets.token_bytes(MASTER_KEY_BYTES))
        workspaces.ensure(WORKSPACE_KEY)
        database = workspaces.store_database(WORKSPACE_KEY)
    return Runtime(str(data_dir), database or StoreDatabase.sqlite())


def worker(backend: str, corpus_path: Path, sizes: list[int], save_sample: int, query_sample: int) -> dict:
    from team_memory.contracts import Actor, Save, Scope, Work, Workspace

    spec = json.loads(corpus_path.read_text())
    actor = Actor(user_id="bench", display_name="Bench", client="pg-parity")
    workspace = Workspace(key=WORKSPACE_KEY, scope=Scope(), writable=True)
    keys: dict[int, str] = {}

    def save(content: str, project: str) -> tuple[int, float]:
        started = time.perf_counter()
        result = runtime.execute(Work(actor=actor, workspace=workspace, operation="memory_save",
                                      arguments=Save(content=content, project=project).model_dump(
                                          mode="json", exclude={"scope"})))
        return result["id"], (time.perf_counter() - started) * 1000

    def ask(question: str) -> tuple[dict, float]:
        started = time.perf_counter()
        result = runtime.recall.search(question, limit=TOP_K, _explain=True, record_usage=False)
        elapsed = (time.perf_counter() - started) * 1000
        records = sorted((item for group in result.get("results", {}).values() for item in group),
                         key=lambda item: float(item.get("score", 0)), reverse=True)[:TOP_K]
        explain = result.get("_explain", {})
        return {"fused": [keys.get(item["id"], f"id-{item['id']}") for item in records],
                "lexical": [keys.get(item["id"], f"id-{item['id']}") for item in explain.get("fts", [])[:TOP_K]],
                "semantic": [keys.get(item["id"], f"id-{item['id']}") for item in explain.get("semantic", [])[:TOP_K]]}, elapsed

    with tempfile.TemporaryDirectory(prefix=f"pg-parity-{backend}-") as scratch:
        runtime = runtime_for(backend, Path(scratch))
        try:
            base_saves = []
            for fact in spec["facts"]:
                record_id, elapsed = save(fact["content"], fact["project"])
                keys[record_id] = fact["key"]
                base_saves.append(elapsed)
            answers, base_recalls = {}, []
            for question in spec["questions"]:
                answers[question["qid"]], elapsed = ask(question["question"])
                base_recalls.append(elapsed)
            log(f"{backend}: base corpus {len(spec['facts'])} records, {len(answers)} questions")
            scale, rng, loaded = [], random.Random(spec["seed"]), len(spec["facts"])
            questions = [question["question"] for question in spec["questions"]]
            for size in sizes:
                timings = []
                while loaded < size:
                    record_id, elapsed = save(filler(loaded, rng), "filler")
                    keys[record_id] = f"filler-{loaded}"
                    loaded += 1
                    if size - loaded < save_sample:
                        timings.append(elapsed)
                recalls = [ask(question)[1] for question in rng.sample(questions, min(query_sample, len(questions)))]
                scale.append({"size": size, "records": loaded, "save_ms": percentiles(timings),
                              "recall_ms": percentiles(recalls)})
                log(f"{backend}: {size} records, recall p95 {scale[-1]['recall_ms'].get('p95')} ms")
        finally:
            runtime.store.db.close()
    return {"backend": backend, "answers": answers, "base": {"save_ms": percentiles(base_saves),
                                                            "recall_ms": percentiles(base_recalls)},
            "scale": scale}


# Parent: databases, processes, metrics


@contextmanager
def fresh_database(admin_url: str):
    """The test suite's fresh_database (tests/pg_support.py): prerequisites met, dropped afterwards."""
    sys.path.insert(0, str(REPO))
    from tests.pg_support import PgServer
    from tests.pg_support import fresh_database as create

    with create(PgServer(admin_url=admin_url)) as database:
        yield database.url


def child_env(scratch: Path, extra: dict[str, str]) -> dict[str, str]:
    home, memory = scratch / "home", scratch / "memory"
    home.mkdir(parents=True, exist_ok=True)
    memory.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("MEMORY_", "TAM_", "CLAUDE_MEMORY", "OPENAI", "ANTHROPIC", "V9_"))}
    env.update(PYTHONPATH=str(SRC), HOME=str(home), TAM_MEMORY_DIR=str(memory), CLAUDE_MEMORY_DIR=str(memory),
               MEMORY_LLM_ENABLED="false", MEMORY_QUALITY_GATE_ENABLED="false", MEMORY_MODE="fast",
               MEMORY_ASYNC_ENRICHMENT="false", USE_BINARY_SEARCH="true", PYTHONHASHSEED="0", **extra)
    return env


def run_backend(backend: str, corpus_path: Path, args, scratch: Path, extra: dict[str, str]) -> dict:
    out = scratch / f"{backend}.json"
    command = [sys.executable, __file__, "--worker", backend, "--corpus", str(corpus_path), "--result", str(out),
               "--sizes", args.sizes, "--save-sample", str(args.save_sample), "--query-sample", str(args.query_sample)]
    log(f"{backend}: starting")
    subprocess.run(command, env=child_env(scratch / backend, extra), check=True, timeout=CHILD_TIMEOUT_SECONDS)
    return json.loads(out.read_text())


def rank(ranking: list[str], gold: str) -> int | None:
    return ranking.index(gold) + 1 if gold in ranking else None


def quality(result: dict, questions: list[dict]) -> dict:
    ranks = [rank(result["answers"][q["qid"]]["fused"], q["gold"]) for q in questions]
    total = len(ranks) or 1
    metrics = {f"recall_at_{k}": round(100 * sum(1 for r in ranks if r and r <= k) / total, 2) for k in RECALL_AT}
    metrics["mrr"] = round(sum(1 / r for r in ranks if r) / total, 4)
    metrics["ndcg_at_10"] = round(sum(1 / math.log2(r + 1) for r in ranks if r) / total, 4)
    return metrics


def jaccard(left: list[str], right: list[str]) -> float:
    a, b = set(left), set(right)
    return 1.0 if not a and not b else len(a & b) / len(a | b)


def kendall_tau(left: list[str], right: list[str]) -> float | None:
    """Tau-a over the items both rankings contain; None with fewer than two shared items."""
    shared = [item for item in left if item in right]
    if len(shared) < 2:
        return None
    position = {item: index for index, item in enumerate(right)}
    concordant = discordant = 0
    for i in range(len(shared)):
        for j in range(i + 1, len(shared)):
            if position[shared[i]] < position[shared[j]]:
                concordant += 1
            else:
                discordant += 1
    return (concordant - discordant) / (concordant + discordant)


def parity(sqlite: dict, postgres: dict, questions: list[dict], tier: str) -> dict:
    jaccards, taus = [], []
    for question in questions:
        left, right = sqlite["answers"][question["qid"]][tier], postgres["answers"][question["qid"]][tier]
        jaccards.append(jaccard(left, right))
        tau = kendall_tau(left, right)
        if tau is not None:
            taus.append(tau)
    return {"jaccard_mean": round(statistics.fmean(jaccards), 4) if jaccards else None,
            "kendall_tau_mean": round(statistics.fmean(taus), 4) if taus else None,
            "questions": len(jaccards)}


def acceptance(summary: dict) -> dict:
    sqlite, postgres = summary["quality"]["sqlite"], summary["quality"]["postgres"]
    recall_drop = round(sqlite["recall_at_10"] - postgres["recall_at_10"], 2)
    lexical = summary["parity"]["lexical"]["jaccard_mean"]
    sqlite_scale = {row["size"]: row for row in summary["scale"]["sqlite"]}
    postgres_scale = {row["size"]: row for row in summary["scale"]["postgres"]}
    ratio = None
    if ACCEPT_P95_SIZE in sqlite_scale and ACCEPT_P95_SIZE in postgres_scale:
        base = sqlite_scale[ACCEPT_P95_SIZE]["recall_ms"].get("p95")
        other = postgres_scale[ACCEPT_P95_SIZE]["recall_ms"].get("p95")
        ratio = round(other / base, 2) if base else None
    return {"recall_at_10_drop_pp": recall_drop, "recall_ok": recall_drop <= ACCEPT_RECALL_DROP_PP,
            "lexical_jaccard": lexical, "lexical_ok": lexical is not None and lexical >= ACCEPT_LEXICAL_JACCARD,
            f"p95_recall_ratio_at_{ACCEPT_P95_SIZE}": ratio,
            "latency_ok": None if ratio is None else ratio <= ACCEPT_P95_RATIO}


def markdown(summary: dict) -> str:
    header = (f"Seed {summary['seed']}, {summary['questions']} questions, {summary['facts']} facts; "
              f"sizes {summary['sizes']}.")
    lines = ["# PostgreSQL vs SQLite parity", "", header, "", "| metric | SQLite | PostgreSQL |", "|---|---|---|"]
    for name in ("recall_at_5", "recall_at_10", "mrr", "ndcg_at_10"):
        lines.append(f"| {name} | {summary['quality']['sqlite'][name]} | {summary['quality']['postgres'][name]} |")
    lines += ["", "| tier | top-10 Jaccard | Kendall tau |", "|---|---|---|"]
    for tier, values in summary["parity"].items():
        lines.append(f"| {tier} | {values['jaccard_mean']} | {values['kendall_tau_mean']} |")
    lines += ["", "| size | save p50/p95 SQLite | save p50/p95 PG | recall p50/p95 SQLite | recall p50/p95 PG |",
              "|---|---|---|---|---|"]
    for left, right in zip(summary["scale"]["sqlite"], summary["scale"]["postgres"], strict=True):
        lines.append(f"| {left['size']} | {left['save_ms'].get('p50')}/{left['save_ms'].get('p95')} | "
                     f"{right['save_ms'].get('p50')}/{right['save_ms'].get('p95')} | "
                     f"{left['recall_ms'].get('p50')}/{left['recall_ms'].get('p95')} | "
                     f"{right['recall_ms'].get('p50')}/{right['recall_ms'].get('p95')} |")
    lines += ["", "Acceptance: " + json.dumps(summary["acceptance"], sort_keys=True)]
    return "\n".join(lines) + "\n"


def parent(args) -> None:
    admin_url = os.environ.get(args.pg_admin_url_env, "").strip()
    if not admin_url:
        raise SystemExit(f"set {args.pg_admin_url_env} to an admin postgresql:// URI (see the module docstring)")
    spec = corpus(args.seed, args.facts)
    sizes = [int(size) for size in args.sizes.split(",") if size]
    with tempfile.TemporaryDirectory(prefix="pg-parity-") as scratch_name:
        scratch = Path(scratch_name)
        corpus_path = scratch / "corpus.json"
        corpus_path.write_text(json.dumps(spec))
        sqlite = run_backend("sqlite", corpus_path, args, scratch, {})
        with fresh_database(admin_url) as url:
            postgres = run_backend("postgres", corpus_path, args, scratch, {WORKER_URL_ENV: url})
    questions = spec["questions"]
    summary = {"seed": args.seed, "facts": len(spec["facts"]), "questions": len(questions), "sizes": sizes,
               "quality": {"sqlite": quality(sqlite, questions), "postgres": quality(postgres, questions)},
               "parity": {tier: parity(sqlite, postgres, questions, tier) for tier in ("fused", "lexical", "semantic")},
               "base": {"sqlite": sqlite["base"], "postgres": postgres["base"]},
               "scale": {"sqlite": sqlite["scale"], "postgres": postgres["scale"]},
               "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    summary["acceptance"] = acceptance(summary)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, indent=1) + "\n")
    args.out.with_suffix(".md").write_text(markdown(summary))
    sys.stdout.write(markdown(summary))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=REPO / "benchmarks" / "results" / "pg-parity.json")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--facts", type=int, default=None, help="use only the first N facts (quick runs)")
    parser.add_argument("--sizes", default=DEFAULT_SIZES, help="comma-separated corpus sizes for latency")
    parser.add_argument("--save-sample", type=int, default=DEFAULT_SAVE_SAMPLE,
                        help="last N saves before each size are timed")
    parser.add_argument("--query-sample", type=int, default=DEFAULT_QUERY_SAMPLE, help="recalls timed per size")
    parser.add_argument("--pg-admin-url-env", default=ADMIN_URL_ENV,
                        help="name of the environment variable holding the admin URI")
    parser.add_argument("--worker", choices=("sqlite", "postgres"), help=argparse.SUPPRESS)
    parser.add_argument("--corpus", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--result", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        sizes = [int(size) for size in args.sizes.split(",") if size]
        result = worker(args.worker, args.corpus, sizes, args.save_sample, args.query_sample)
        args.result.write_text(json.dumps(result))
        return
    parent(args)


if __name__ == "__main__":
    main()

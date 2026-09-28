"""One command for the whole organisational-memory benchmark.

    PYTHONPATH=src python docs/benchmarks/org-memory-v14-20260925/run_all.py --seed 20260925 --label base

All server data lives in mktemp -d directories that are removed afterwards. No LLM calls.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from orgbench import data as dataset
from orgbench import harness

sys.path.insert(0, str(harness.SRC))
EXPERIMENTS = ("e1", "e1local", "e2", "e3", "e4", "e4multi")
OPTIONAL = ("quality",)  # retrieval quality only; run with --only quality


def log(message):
    print(time.strftime("%H:%M:%S"), message, flush=True)


def subprocess_step(module_args, env):
    subprocess.run([harness.PYTHON, *module_args], env={**env, "PYTHONPATH": f"{harness.SRC}:{HERE}"},
                   check=True, cwd=HERE)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=20260925)
    parser.add_argument("--label", default="base")
    parser.add_argument("--only", default=",".join(EXPERIMENTS))
    parser.add_argument("--workers", type=int, default=3, help="TAM_TEAM_MAX_WORKERS for E1-E3")
    parser.add_argument("--e4-passes", choices=("both", "forward", "reverse"), default="both",
                        help="E4 condition order; forward/reverse run one pass for ABBA across code versions")
    parser.add_argument("--cross-rerank", choices=("", "auto", "on", "off"), default="",
                        help="MEMORY_CROSS_RERANK for servers and workers; empty keeps the default (auto)")
    parser.add_argument("--backend", choices=harness.BACKENDS, default=harness.BACKEND,
                        help=f"team server backend; postgres needs {harness.PG_ADMIN_URL_ENV} (an admin URI)")
    args = parser.parse_args()
    harness.CROSS_RERANK = args.cross_rerank
    # Subprocess steps import harness afresh and read the backend from the environment.
    harness.BACKEND = os.environ[harness.BACKEND_ENV] = args.backend
    only = set(args.only.split(","))
    data_path = dataset.write(args.seed, HERE / "data")
    company = json.loads(data_path.read_text())
    out = HERE / "raw" / args.label
    out.mkdir(parents=True, exist_ok=True)
    env_info = harness.environment(args.workers)
    env_info.update(seed=args.seed, label=args.label, started=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    load_at_start=harness.loadavg(), experiments=sorted(only),
                    cross_rerank=args.cross_rerank or "default (auto)", backend=args.backend)
    (out / f"environment-{'-'.join(sorted(only))}.json").write_text(json.dumps(env_info, indent=1) + "\n")
    if "e1" in only:
        from orgbench import e1
        log("E1 team server")
        result = e1.run(company, out, args.workers, log)
        harness.write_jsonl(out / "e1.jsonl", result["rows"])
        (out / "e1-meta.json").write_text(json.dumps({k: v for k, v in result.items() if k != "rows"}, indent=1) + "\n")
    if "e1local" in only:
        log("E1 local single store")
        with harness.scratch_dir("orgbench-e1local-") as scratch:
            env = harness.base_env(scratch)
            target = scratch / "e1local.json"
            subprocess_step(["-m", "orgbench.e1_local", "--data", str(data_path), "--out", str(target)], env)
            result = json.loads(target.read_text())
        harness.write_jsonl(out / "e1-local.jsonl", result["rows"])
        (out / "e1-local-meta.json").write_text(json.dumps({k: v for k, v in result.items() if k != "rows"}, indent=1) + "\n")
    if "e2" in only:
        from orgbench import e2
        log("E2 lifecycle")
        result = e2.run(company, out, args.workers, log)
        harness.write_jsonl(out / "e2.jsonl", result["rows"])
        (out / "e2-notes.json").write_text(json.dumps(result["notes"], indent=1, ensure_ascii=False) + "\n")
    if "e3" in only:
        from orgbench import e3
        log("E3 concurrency and retries")
        rows = []
        e3.concurrency(rows, log)
        harness.write_jsonl(out / "e3.jsonl", rows)
        with harness.scratch_dir("orgbench-e3proc-") as scratch:
            env = harness.base_env(scratch, args.workers)
            subprocess_step(["-m", "orgbench.e3", "--out", str(out / "e3-server-timeout.jsonl")], env)
    if "quality" in only:
        from orgbench import e2
        log("Retrieval quality (E2a set)")
        harness.write_jsonl(out / "quality.jsonl", e2.quality(company, args.workers, log))
    if "e4" in only:
        log("E4 cost of isolation")
        with harness.scratch_dir("orgbench-e4-") as scratch:
            env = harness.base_env(scratch)
            subprocess_step(["-m", "orgbench.e4", "--data", str(data_path), "--out", str(out / "e4.jsonl"),
                             "--scratch", str(scratch), "--passes", args.e4_passes], env)
    if "e4multi" in only:
        log("E4 multi-user")
        with harness.scratch_dir("orgbench-e4multi-") as scratch:
            env = harness.base_env(scratch)
            subprocess_step(["-m", "orgbench.e4_multiuser", "--data", str(data_path),
                             "--out", str(out / "e4-multiuser.jsonl"), "--scratch", str(scratch)], env)
    from orgbench import report
    summary = report.summarise(out)
    (out / "summary.json").write_text(json.dumps(summary, indent=1) + "\n")
    (out / "summary.md").write_text(report.markdown(summary) + "\n")
    log(f"done: {out}")


if __name__ == "__main__":
    main()

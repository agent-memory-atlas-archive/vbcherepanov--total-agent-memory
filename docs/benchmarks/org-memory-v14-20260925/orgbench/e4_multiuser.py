"""E4 multi-user: 4 users of different teams search in turn with TAM_TEAM_MAX_WORKERS=3.

The worker limit is server-wide and every user has an own personal area, so users evict each other's
workers. Two orders over the same 80 searches (20 per user): `rotate` (u1, u2, u3, u4, u1, ...) and
`blocks` (20 x u1, then 20 x u2, ...), run A B B A. A search is cold when it had to start at least one
worker process. In-process gateway as in e4.py; run as a subprocess with harness.base_env.
"""
import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

TEAMS = ("engineering", "sales", "hr", "finance")
SEARCHES_PER_USER = 20
WORKERS = 3
ORDER = ("rotate", "blocks", "blocks", "rotate")


def pct(values, q):
    ordered = sorted(values)
    return ordered[max(0, round(q * len(ordered)) - 1)]


async def populate(root: Path, data: dict):
    from team_memory.registry import Registry
    from team_memory.service import MemoryService
    from team_memory.worker import WorkerPool
    registry = Registry(root)
    tokens = {}
    for team in TEAMS:
        registry.add_team(team, team.title())
        user = f"mu-{team}"
        registry.add_user(user, user)
        registry.membership(user, team, "editor")
        tokens[team] = registry.issue_token(user, "e4-multiuser")
    pool = WorkerPool(root, maximum=len(TEAMS) + 2)
    service = MemoryService(registry, pool)
    try:
        for index, team in enumerate(TEAMS):
            for fact in data["departments"][team]:
                await service.call(tokens[team], "memory_save", {"scope": {"kind": "team", "team_id": team},
                                                                 "content": fact["content"], "project": "company"})
            for note in list(data["personal"].values())[index]:
                await service.call(tokens[team], "memory_save", {"content": note["content"], "project": "company"})
        for fact in data["shared"]:
            await service.call(tokens[TEAMS[0]], "memory_save", {"scope": {"kind": "shared"},
                                                                 "content": fact["content"], "project": "company"})
    finally:
        pool.close()
    return registry, tokens


def schedule(order: str, data: dict) -> list[tuple[str, str]]:
    questions = {team: [f["questions"][0]["question"] for f in data["departments"][team][:SEARCHES_PER_USER]]
                 for team in TEAMS}
    if order == "rotate":
        return [(team, questions[team][i]) for i in range(SEARCHES_PER_USER) for team in TEAMS]
    return [(team, question) for team in TEAMS for question in questions[team]]


async def run_order(registry, tokens, order: str, data: dict) -> dict:
    from team_memory.service import MemoryService
    from team_memory.worker import WorkerPool
    pool = WorkerPool(registry.root, maximum=WORKERS)
    service = MemoryService(registry, pool)
    samples = []
    try:
        for team, question in schedule(order, data):
            before = {p.pid for p, _ in pool.workers.values()}
            started = time.perf_counter()
            await service.call(tokens[team], "memory_recall", {"query": question, "limit": 10})
            elapsed = (time.perf_counter() - started) * 1000
            spawned = len({p.pid for p, _ in pool.workers.values()} - before)
            samples.append({"team": team, "ms": elapsed, "workers_started": spawned})
    finally:
        await asyncio.to_thread(pool.close)
    cold = [s["ms"] for s in samples if s["workers_started"]]
    warm = [s["ms"] for s in samples if not s["workers_started"]]
    latencies = [s["ms"] for s in samples]
    return {"order": order, "searches": len(samples), "cold_searches": len(cold),
            "cold_share": len(cold) / len(samples), "workers_started": sum(s["workers_started"] for s in samples),
            "p50_ms": statistics.median(latencies), "p95_ms": pct(latencies, 0.95),
            "cold_p50_ms": statistics.median(cold) if cold else None,
            "warm_p50_ms": statistics.median(warm) if warm else None, "samples": samples}


def loadavg():
    import subprocess
    raw = subprocess.run(["sysctl", "-n", "vm.loadavg"], capture_output=True, text=True, check=False).stdout
    return [float(x) for x in raw.strip().strip("{}").split()]


async def main_async(args):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from orgbench.harness import wait_for_quiet
    data = json.loads(args.data.read_text())
    registry, tokens = await populate(args.scratch / "root", data)
    rows = []
    for run, order in enumerate(ORDER, 1):
        guard = wait_for_quiet(lambda m: print(m, flush=True))
        before = loadavg()
        row = await run_order(registry, tokens, order, data)
        after = loadavg()
        rows.append({"experiment": "E4-multiuser", "run": run, "workers": WORKERS, **row,
                     "load_before": before, "load_after": after, "load_guard": guard})
        print(f"E4 multi-user run{run} {order}: cold {row['cold_share']:.2f} p50 {row['p50_ms']:.0f}ms "
              f"p95 {row['p95_ms']:.0f}ms load {before[0]:.2f}->{after[0]:.2f}", flush=True)
    args.out.write_text("\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    asyncio.run(main_async(parser.parse_args()))

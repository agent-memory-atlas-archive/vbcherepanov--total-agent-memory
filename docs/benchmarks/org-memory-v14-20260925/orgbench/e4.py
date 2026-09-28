"""E4: cost of isolation. In-process gateway (MemoryService + WorkerPool), as in browser-cpu-v14-20260915.

Run as a subprocess with harness.base_env. Measures, per condition (scopes x TAM_TEAM_MAX_WORKERS, plus one
single store holding the same records): cold first query, warm p50/p95, RSS of all processes, CPU seconds.
Conditions are interleaved A B C ... then ... C B A (two passes); load average is recorded around each batch.
"""
import argparse
import asyncio
import json
import os
import resource
import statistics
import subprocess
import sys
import time
from pathlib import Path

SCOPE_COUNTS = (1, 2, 3, 5, 8)
WORKER_LIMITS = (1, 2, 3)
QUERIES_PER_PASS = 30
TEAMS = ("engineering", "sales", "hr", "finance", "legal", "ops")


def ps(pids):
    if not pids:
        return {}
    out = subprocess.run(["ps", "-o", "pid=,rss=,time=", "-p", ",".join(map(str, pids))],
                         capture_output=True, text=True, check=False).stdout
    result = {}
    for line in out.strip().splitlines():
        pid, rss, cpu = line.split()
        minutes, seconds = cpu.split(":")
        result[int(pid)] = {"rss_kib": int(rss), "cpu_s": int(minutes) * 60 + float(seconds)}
    return result


def children_cpu():
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


def self_cpu():
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


def conditions():
    out = []
    for scopes in SCOPE_COUNTS:
        limits = sorted({w for w in WORKER_LIMITS if w <= scopes} | {scopes})
        out += [{"scopes": scopes, "workers": w, "system": "team-server"} for w in limits]
        out.append({"scopes": scopes, "workers": 1, "system": "single-store"})
    return out


async def populate(root: Path, data: dict):
    from team_memory.registry import Registry
    from team_memory.service import MemoryService
    from team_memory.worker import WorkerPool
    registry = Registry(root)
    for t in TEAMS:
        registry.add_team(t, t.title())
    users = {}
    for scopes in SCOPE_COUNTS:
        user = f"load{scopes}"
        registry.add_user(user, user)
        teams = TEAMS[:1] if scopes == 1 else TEAMS[:scopes - 2]
        for t in teams:
            registry.membership(user, t, "editor")
        single = f"single{scopes}"
        registry.add_user(single, single)
        users[scopes] = {"user": registry.issue_token(user, "e4"), "single": registry.issue_token(single, "e4"),
                         "teams": teams}
    pool = WorkerPool(root, maximum=16)
    service = MemoryService(registry, pool)
    try:
        writer = registry.issue_token("load8", "e4-writer")
        for t in TEAMS:
            for fact in data["departments"][t]:
                await service.call(writer, "memory_save", {"scope": {"kind": "team", "team_id": t},
                                                           "content": fact["content"], "project": "company"})
        for fact in data["shared"]:
            await service.call(writer, "memory_save", {"scope": {"kind": "shared"}, "content": fact["content"],
                                                       "project": "company"})
        for scopes, entry in users.items():
            notes = [n["content"] for n in list(data["personal"].values())[scopes % 12]]
            for note in notes:
                await service.call(entry["user"], "memory_save", {"content": note, "project": "company"})
            # single store: the same records this user can search, in one workspace
            contents = list(notes)
            if scopes >= 2:
                contents += [f["content"] for f in data["shared"]]
            for t in entry["teams"]:
                contents += [f["content"] for f in data["departments"][t]]
            if scopes == 1:
                contents = [f["content"] for f in data["departments"]["engineering"]]
            for content in contents:
                await service.call(entry["single"], "memory_save", {"content": content, "project": "company"})
            entry["records"] = len(contents)
    finally:
        pool.close()
    return registry, users


def queries(data):
    out = []
    for t in TEAMS:
        out += [q["question"] for f in data["departments"][t][:5] for q in f["questions"][:1]]
    return out[:QUERIES_PER_PASS]


async def measure(registry, users, condition, qs, workers_cap):
    from team_memory.service import MemoryService
    from team_memory.worker import WorkerPool
    entry = users[condition["scopes"]]
    workers = condition["workers"]
    pool = WorkerPool(registry.root, maximum=workers)
    service = MemoryService(registry, pool)
    if condition["system"] == "single-store":
        token, args = entry["single"], {"scope": {"kind": "personal"}}
    elif condition["scopes"] == 1:
        token, args = entry["user"], {"scope": {"kind": "team", "team_id": "engineering"}}
    else:
        token, args = entry["user"], {}
    cpu_children0, cpu_self0 = children_cpu(), self_cpu()
    try:
        started = time.perf_counter()
        await service.call(token, "memory_recall", {"query": qs[0], "limit": 10, **args})
        cold_ms = (time.perf_counter() - started) * 1000
        live_after_cold = {p.pid for p, _ in pool.workers.values()}
        samples, seen = [], set(live_after_cold)
        # warm CPU = CPU of workers reaped during the batch (whole life) + CPU of live workers now - CPU at batch start
        cpu_live_before = sum(v["cpu_s"] for v in ps(sorted(seen)).values())
        children_before = children_cpu()
        for q in qs:
            started = time.perf_counter()
            await service.call(token, "memory_recall", {"query": q, "limit": 10, **args})
            samples.append((time.perf_counter() - started) * 1000)
            seen |= {p.pid for p, _ in pool.workers.values()}
        live = {p.pid for p, _ in pool.workers.values()}
        stats = ps(sorted(live | {os.getpid()}))
        worker_rss = sum(v["rss_kib"] for pid, v in stats.items() if pid != os.getpid()) / 1024
        gateway_rss = stats.get(os.getpid(), {}).get("rss_kib", 0) / 1024
        cpu_live_after = sum(v["cpu_s"] for pid, v in stats.items() if pid != os.getpid())
        warm_worker_cpu = (children_cpu() - children_before) + cpu_live_after - cpu_live_before
    finally:
        await asyncio.to_thread(pool.close)
    total_cpu = (children_cpu() - cpu_children0)
    samples_sorted = sorted(samples)
    return {**condition, "records_searchable": entry["records"], "cold_first_query_ms": cold_ms,
            "warm_p50_ms": statistics.median(samples),
            "warm_p95_ms": samples_sorted[max(0, round(0.95 * len(samples_sorted)) - 1)],
            "warm_samples_ms": samples, "workers_alive_end": len(live), "worker_processes_started": len(seen),
            "worker_rss_sum_mib": worker_rss, "gateway_rss_mib": gateway_rss,
            "total_rss_mib": worker_rss + gateway_rss,
            "worker_cpu_s_total": total_cpu, "worker_cpu_s_warm_batch": warm_worker_cpu,
            "gateway_cpu_s": self_cpu() - cpu_self0}


def gateway_model_load() -> dict:
    """With the gateway re-rank, the gateway process holds one cross-encoder for its whole life.

    Load it once before the conditions, as a long-running server would have, and record the cost.
    Code without `team_memory.rerank` keeps the model in the workers and loads nothing here.
    """
    try:
        from team_memory.rerank import GatewayReranker
    except ImportError:
        return {"gateway_rerank": False}
    encoder = GatewayReranker().encoder()
    if encoder is None:
        return {"gateway_rerank": True, "model": None}
    rss_before = ps([os.getpid()])[os.getpid()]["rss_kib"] / 1024
    started = time.perf_counter()
    ready = encoder.wait_ready()
    return {"gateway_rerank": True, "model": encoder.model, "ready": ready,
            "load_seconds": time.perf_counter() - started,
            "gateway_rss_added_mib": ps([os.getpid()])[os.getpid()]["rss_kib"] / 1024 - rss_before}


def loadavg():
    raw = subprocess.run(["sysctl", "-n", "vm.loadavg"], capture_output=True, text=True, check=False).stdout
    return [float(x) for x in raw.strip().strip("{}").split()]


async def main_async(args):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from orgbench.harness import wait_for_quiet
    data = json.loads(args.data.read_text())
    root = args.scratch / "root"
    registry, users = await populate(root, data)
    qs = queries(data)
    gateway_model = gateway_model_load()
    plan = conditions()
    # both: A B C ... C B A in one process. forward / reverse: one pass, so that runs of two code versions
    # can be interleaved A(forward) B(forward) B(reverse) A(reverse).
    order = {"both": [(1, c) for c in plan] + [(2, c) for c in reversed(plan)],
             "forward": [(1, c) for c in plan], "reverse": [(2, c) for c in reversed(plan)]}[args.passes]
    rows = []
    for pass_no, condition in order:
        guard = wait_for_quiet(lambda m: print(m, flush=True))
        before = loadavg()
        row = await measure(registry, users, condition, qs, max(SCOPE_COUNTS))
        after = loadavg()
        rows.append({"experiment": "E4", "pass": pass_no, **row, "load_before": before, "load_after": after,
                     "load_guard": guard, "gateway_model": gateway_model})
        print(f"E4 pass{pass_no} {condition}: cold {row['cold_first_query_ms']:.0f}ms p50 {row['warm_p50_ms']:.1f}ms "
              f"rss {row['total_rss_mib']:.0f}MiB load {before[0]:.2f}->{after[0]:.2f}", flush=True)
    args.out.write_text("\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--passes", choices=("both", "forward", "reverse"), default="both")
    asyncio.run(main_async(parser.parse_args()))

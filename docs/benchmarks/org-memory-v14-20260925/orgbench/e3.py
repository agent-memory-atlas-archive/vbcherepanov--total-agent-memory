"""E3: concurrent edits of one record, and request_id retries.

Part 1 and the client-timeout retries go over HTTP. Server-side timeouts are injected in-process
(E3 idempotency subprocess) by lowering WorkerPool.timeout after warm-up, which is the real code path.
"""
import argparse
import asyncio
import json
import sys
import threading
import time
import uuid
from pathlib import Path

import httpx

from orgbench.harness import (
    Client,
    Server,
    admin,
    apply_env_in_process,
    base_env,
    scratch_dir,
    team,
)

CLIENT_COUNTS = (2, 4, 8, 16)
ROUNDS = 100
RETRY_REPS = 20
SERVER_TIMEOUTS_S = (0.0005, 0.002, 0.005, 0.01, 0.015)
TEAM = "engineering"


def different_payload(op: str, args: dict) -> dict:
    """Same request_id, different arguments."""
    changed = dict(args)
    if "content" in changed:
        changed["content"] += " changed"
    if op != "memory_save":
        changed["reason"] = "different reason"
    return changed


def concurrency(out_rows, log=print):
    with scratch_dir("orgbench-e3-") as scratch:
        env = base_env(scratch)
        root = scratch / "root"
        admin(root, env, "team-add", TEAM, "Engineering")
        tokens = {}
        for i in range(max(CLIENT_COUNTS)):
            user = f"editor{i:02d}"
            admin(root, env, "user-add", user, f"Editor {i}")
            admin(root, env, "member", user, TEAM, "editor")
            path = scratch / f"{user}.token"
            admin(root, env, "token-create", user, "--client", "orgbench", "--out", str(path))
            tokens[user] = path.read_text().strip()
        users = sorted(tokens)
        with Server(root, env, scratch / "server.log") as server:
            coordinator = Client(server.url)
            summary = []
            for clients in CLIENT_COUNTS:
                status, body = coordinator.call(tokens[users[0]], "memory_save", {
                    "scope": team(TEAM), "content": f"Shared runbook for {clients} editors, version 0.", "project": "e3"})
                head = body["data"]
                first_id = head["id"]
                successes = []
                for round_no in range(ROUNDS):
                    barrier = threading.Barrier(clients)
                    replies = [None] * clients

                    def worker(k, head=head, round_no=round_no, barrier=barrier, replies=replies, clients=clients):
                        own = Client(server.url)
                        content = f"Shared runbook for {clients} editors, round {round_no}, written by client {k}."
                        barrier.wait()
                        started = time.monotonic()
                        replies[k] = (content, *own.call(tokens[users[k]], "memory_update", {
                            "scope": team(TEAM), "id": head["id"], "expected_revision": head["revision"],
                            "content": content, "reason": f"round {round_no}"}), (time.monotonic() - started) * 1000)
                        own.close()

                    threads = [threading.Thread(target=worker, args=(k,)) for k in range(clients)]
                    for t in threads:
                        t.start()
                    for t in threads:
                        t.join()
                    ok = [(c, b) for c, s, b, _ in replies if s == 200]
                    conflicts = sum(1 for _, s, b, _ in replies if s != 200 and b.get("code") == "conflict")
                    other = [(s, b.get("code"), b.get("error")) for _, s, b, _ in replies if s != 200 and b.get("code") != "conflict"]
                    new_head = ok[0][1]["data"] if ok else head
                    stored = coordinator.call(tokens[users[0]], "memory_get", {"scope": team(TEAM), "id": new_head["id"]})[1]["data"]
                    lost = [c for c, b in ok if stored["content"] != c]
                    out_rows.append({"experiment": "E3-concurrency", "clients": clients, "round": round_no,
                                     "successes": len(ok), "conflicts": conflicts, "other_errors": other,
                                     "lost_updates": len(lost), "latency_ms": [r[3] for r in replies]})
                    successes.extend(c for c, _ in ok)
                    head = new_head
                # full history of the chain
                events, after = [], 0
                while True:
                    page = coordinator.call(tokens[users[0]], "memory_history",
                                            {"scope": team(TEAM), "id": head["id"], "after": after, "limit": 50})[1]["data"]
                    if not page:
                        break
                    events.extend(page)
                    after = page[-1]["sequence"]
                inserted = [e["after_state"]["content"] for e in events if e["operation"] == "insert"]
                superseded = [e for e in events if e["operation"] == "update" and e["after_state"]["status"] == "superseded"]
                chain = [first_id]
                summary.append({"experiment": "E3-history", "clients": clients, "rounds": ROUNDS,
                                "successful_updates": len(successes), "history_events": len(events),
                                "insert_events": len(inserted), "supersede_events": len(superseded),
                                "every_success_in_history": all(c in inserted for c in successes),
                                "distinct_actors": len({e["actor"]["user_id"] for e in events}),
                                "final_revision_chain_length": len(inserted), "first_id": chain[0], "final_id": head["id"]})
                log(f"E3 {clients} clients done")
            out_rows.extend(summary)
            # client-side timeout, then retry with the same request_id
            token = tokens[users[0]]
            for rep in range(RETRY_REPS):
                for op in ("memory_save", "memory_update", "memory_delete"):
                    if op == "memory_save":
                        args = {"scope": team(TEAM), "content": f"Client-timeout save {rep} {uuid.uuid4().hex}", "project": "e3"}
                    else:
                        base = coordinator.call(token, "memory_save", {"scope": team(TEAM), "project": "e3",
                                                                       "content": f"Retry base {op} {rep} {uuid.uuid4().hex}"})[1]["data"]
                        args = {"scope": team(TEAM), "id": base["id"], "expected_revision": base["revision"],
                                "reason": "retry test"}
                        if op == "memory_update":
                            args["content"] = f"Retry updated {rep} {uuid.uuid4().hex}"
                    args["request_id"] = str(uuid.uuid4())
                    impatient = httpx.Client(base_url=server.url, timeout=0.001)
                    try:
                        impatient.post("/api/call", headers={"Authorization": "Bearer " + token},
                                       json={"name": op, "arguments": args})
                        first = "completed"
                    except httpx.TimeoutException:
                        first = "client-timeout"
                    finally:
                        impatient.close()
                    time.sleep(0.3)
                    status, body = coordinator.call(token, op, args)
                    _, body2 = coordinator.call(token, op, args)
                    text = args.get("content") or ""
                    if op == "memory_save":
                        hits = coordinator.call(token, "memory_recall", {"query": text, "scope": team(TEAM), "limit": 10})[1]
                        copies = sum(1 for i in hits.get("results", []) if i["record"]["content"] == text)
                    elif op == "memory_update":
                        hist = coordinator.call(token, "memory_history", {"scope": team(TEAM), "id": body["data"]["id"]})[1]["data"]
                        copies = sum(1 for e in hist if e["operation"] == "insert" and e["after_state"]["content"] == text)
                    else:
                        hist = coordinator.call(token, "memory_history", {"scope": team(TEAM), "id": args["id"]})[1]["data"]
                        copies = sum(1 for e in hist if e["operation"] == "update" and e["after_state"]["status"] == "deleted")
                    changed = different_payload(op, args)
                    status3, body3 = coordinator.call(token, op, changed)
                    out_rows.append({"experiment": "E3-retry", "timeout": "client", "op": op, "rep": rep, "first": first,
                                     "retry_status": status, "retry_same_result": body == body2, "copies": copies,
                                     "different_payload_status": status3, "different_payload_code": body3.get("code")})
            coordinator.close()


async def server_timeouts(out: Path):
    """In-process: lower the pool timeout so the gateway gives up and kills the worker mid-operation."""
    from team_memory.contracts import Conflict, Unavailable
    from team_memory.registry import Registry
    from team_memory.service import MemoryService
    from team_memory.worker import WorkerPool
    rows = []
    with scratch_dir("orgbench-e3-timeout-") as scratch:
        apply_env_in_process(scratch)  # workers inherit the backend (TAM_TEAM_DATABASE_URL on postgres)
        registry = Registry(scratch / "root")
        registry.add_user("vasya", "Vasya")
        registry.add_team(TEAM, "Engineering")
        registry.membership("vasya", TEAM, "editor")
        token = registry.issue_token("vasya", "orgbench")
        pool = WorkerPool(registry.root, maximum=3)
        service = MemoryService(registry, pool)
        scope = team(TEAM)
        try:
            await service.call(token, "memory_save", {"scope": scope, "content": "Warm-up record for the timeout test."})
            for rep in range(RETRY_REPS):
                for op in ("memory_save", "memory_update", "memory_delete"):
                    marker = uuid.uuid4().hex
                    if op == "memory_save":
                        args = {"scope": scope, "content": f"Server-timeout save {marker}"}
                    else:
                        base = (await service.call(token, "memory_save", {"scope": scope, "content": f"Timeout base {op} {marker}"}))["data"]
                        args = {"scope": scope, "id": base["id"], "expected_revision": base["revision"], "reason": "timeout test"}
                        if op == "memory_update":
                            args["content"] = f"Server-timeout update {marker}"
                    args["request_id"] = str(uuid.uuid4())
                    pool.timeout = SERVER_TIMEOUTS_S[rep % len(SERVER_TIMEOUTS_S)]
                    try:
                        await service.call(token, op, args)
                        first = "completed"
                    except Unavailable:
                        first = "server-timeout"
                    pool.timeout = 120
                    if op == "memory_save":
                        probe = await service.call(token, "memory_recall", {"query": args["content"], "scope": scope, "limit": 5})
                        committed = any(i["record"]["content"] == args["content"] for i in probe["results"])
                    else:
                        probe = await service.call(token, "memory_get", {"scope": scope, "id": args["id"]})
                        committed = probe["data"]["status"] != "active"
                    retry = await service.call(token, op, args)
                    again = await service.call(token, op, args)
                    if op == "memory_save":
                        hits = await service.call(token, "memory_recall", {"query": args["content"], "scope": scope, "limit": 10})
                        copies = sum(1 for i in hits["results"] if i["record"]["content"] == args["content"])
                    elif op == "memory_update":
                        hist = (await service.call(token, "memory_history", {"scope": scope, "id": retry["data"]["id"]}))["data"]
                        copies = sum(1 for e in hist if e["operation"] == "insert" and e["after_state"]["content"] == args["content"])
                    else:
                        hist = (await service.call(token, "memory_history", {"scope": scope, "id": args["id"]}))["data"]
                        copies = sum(1 for e in hist if e["operation"] == "update" and e["after_state"]["status"] == "deleted")
                    try:
                        await service.call(token, op, different_payload(op, args))
                        different = "accepted"
                    except Conflict:
                        different = "conflict"
                    rows.append({"experiment": "E3-retry", "timeout": "server", "op": op, "rep": rep,
                                 "pool_timeout_s": SERVER_TIMEOUTS_S[rep % len(SERVER_TIMEOUTS_S)], "first": first,
                                 "first_committed": committed, "retry_same_result": retry == again, "copies": copies, "different_payload": different})
            # Same UUID, same user, different scope (not covered by the per-workspace request table).
            request_id = str(uuid.uuid4())
            first = await service.call(token, "memory_save", {"content": "Cross-scope UUID probe A.", "request_id": request_id})
            try:
                second = await service.call(token, "memory_save", {"scope": {"kind": "shared"}, "content": "Cross-scope UUID probe B.",
                                                                   "request_id": request_id})
                cross = {"accepted": True, "second_id": second["data"]["id"]}
            except Conflict as exc:
                cross = {"accepted": False, "error": str(exc)}
            try:
                third = await service.call(token, "memory_save", {"scope": scope, "content": "Cross-scope UUID probe A.",
                                                                  "request_id": request_id})
                same_payload_other_scope = {"accepted": True, "third_id": third["data"]["id"]}
            except Conflict as exc:
                same_payload_other_scope = {"accepted": False, "error": str(exc)}
            rows.append({"experiment": "E3-retry-cross-scope", "first_id": first["data"]["id"],
                         "different_payload_other_scope": cross, "same_payload_other_scope": same_payload_other_scope})
        finally:
            pool.close()
    out.write_text("\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    asyncio.run(server_timeouts(args.out))

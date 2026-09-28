"""E1: department isolation on the team server (HTTP /api/call, the same service as /mcp)."""
import random
import time
import uuid
from pathlib import Path

from orgbench import company
from orgbench.data import DEPARTMENTS
from orgbench.harness import Client, Server, admin, base_env, scratch_dir, team
from orgbench.leaks import classify, ranked_canaries

FACTS_PER_FOREIGN_DEPT = 10
IDS_PER_FOREIGN_DEPT = 5
REMOVAL_QUERIES = 20
LIMIT = 10
CONTROL_TOP = 5


def error_of(status, body):
    if status == 200:
        return None
    return {"status": status, "code": body.get("code"), "error": body.get("error", "")[:200]}


def snapshot(client, tokens, state):
    """Every record of every department, read by that department's editor1."""
    out = {}
    for dept in DEPARTMENTS:
        records, after = [], 0
        while True:
            _, body = client.call(tokens[f"{dept}-editor1"], "memory_export",
                                       {"scope": team(dept), "after": after, "limit": 50})
            page = body["data"]
            if not page:
                break
            records.extend((r["id"], r["status"], r["revision"], r["content"]) for r in page)
            after = page[-1]["id"]
        out[dept] = records
    return out


def run(data: dict, out: Path, workers: int, log=print) -> dict:
    rows = []
    rng = random.Random(data["seed"] + 1)
    with scratch_dir("orgbench-e1-") as scratch:
        env = base_env(scratch, workers)
        root = scratch / "root"
        tokens = company.create_identities(root, env, data, scratch / "tokens")
        with Server(root, env, scratch / "server.log") as server:
            client = Client(server.url)
            state = company.populate(client, data, tokens)
            before = snapshot(client, tokens, state)

            def record(attempt, user, dept, op, status, body, **extra):
                verdict = classify(body, user["id"], {dept}) if status == 200 else {
                    "leaked_canaries": [], "foreign_scopes": [], "own_canaries": []}
                rows.append({"experiment": "E1", "system": "team-server", "attempt": attempt, "user": user["id"],
                             "role": user["role"], "department": dept, "op": op, "http_status": status,
                             "error": error_of(status, body), **verdict, **extra})
                return body

            for user in data["users"]:
                dept, token = user["team"], tokens[user["id"]]
                started = time.monotonic()
                foreign = [d for d in DEPARTMENTS if d != dept]
                picks = {f: rng.sample(data["departments"][f], FACTS_PER_FOREIGN_DEPT) for f in foreign}
                # 1. direct text and canary
                for f in foreign:
                    for fact in picks[f]:
                        for form, query in (("text", fact["content"]), ("canary", fact["canary"])):
                            status, body = client.call(token, "memory_recall", {"query": query, "limit": LIMIT})
                            record("1-direct-" + form, user, dept, "memory_recall", status, body, target=f)
                # 1p. other people's personal notes
                for other, notes in data["personal"].items():
                    if other == user["id"]:
                        continue
                    status, body = client.call(token, "memory_recall", {"query": notes[0]["content"], "limit": LIMIT})
                    record("1-personal-text", user, dept, "memory_recall", status, body, target="personal:" + other)
                # 2. paraphrase
                for f in foreign:
                    for fact in picks[f]:
                        question = next(q["question"] for q in fact["questions"] if q["kind"] == "paraphrase")
                        status, body = client.call(token, "memory_recall", {"query": question, "limit": LIMIT})
                        record("2-paraphrase", user, dept, "memory_recall", status, body, target=f)
                # 3. identical text stored in two departments
                for twin in data["twins"]:
                    status, body = client.call(token, "memory_recall", {"query": twin["content"], "limit": LIMIT})
                    twin_hits = [(item["scope"].get("team_id"), item["record"]["id"]) for item in body.get("results", [])
                                 if item["record"]["content"] == twin["content"]]
                    record("3-twin", user, dept, "memory_recall", status, body, target=",".join(twin["departments"]),
                           twin_hits=twin_hits, own_twin_expected=dept in twin["departments"])
                # 4. foreign team_id in scope, read and write
                for f in foreign:
                    fact = picks[f][0]
                    entry = state["facts"][fact["key"]]
                    scope = team(f)
                    calls = [
                        ("memory_recall", {"query": fact["content"], "scope": scope}),
                        ("memory_get", {"scope": scope, "id": entry["current"]}),
                        ("memory_history", {"scope": scope, "id": entry["current"]}),
                        ("memory_export", {"scope": scope}),
                        ("memory_save", {"scope": scope, "content": f"Injected by {user['id']} into {f}."}),
                        ("memory_update", {"scope": scope, "id": entry["current"], "expected_revision": 1,
                                           "content": f"Overwritten by {user['id']}.", "reason": "attack"}),
                        ("memory_delete", {"scope": scope, "id": entry["current"], "expected_revision": 1,
                                           "reason": "attack"}),
                    ]
                    for op, args in calls:
                        status, body = client.call(token, op, args)
                        record("4-foreign-team-id", user, dept, op, status, body, target=f)
                # 5. access tags
                for tags in (["scope:team"], [f"team:{foreign[0]}"], ["scope:shared"], [f"user:{foreign[0]}-editor1"]):
                    for scope in (None, team(dept)):
                        args = {"content": f"Tag probe {tags[0]} by {user['id']}.", "tags": tags}
                        if scope:
                            args["scope"] = scope
                        status, body = client.call(token, "memory_save", args)
                        record("5-tag-save", user, dept, "memory_save", status, body, target=tags[0])
                fact = picks[foreign[0]][1]
                for query in (f"scope:team team:{foreign[0]} {fact['subject']}",
                              f"tags:team:{foreign[0]} {fact['questions'][0]['question']}"):
                    status, body = client.call(token, "memory_recall", {"query": query, "limit": LIMIT})
                    record("5-tag-query", user, dept, "memory_recall", status, body, target=foreign[0])
                status, body = client.call(token, "memory_recall", {"query": fact["subject"], "tags": [f"team:{foreign[0]}"]})
                record("5-tag-field", user, dept, "memory_recall", status, body, target=foreign[0])
                # 6. reader writes to own team
                if user["role"] == "reader":
                    own = state["facts"][data["departments"][dept][0]["key"]]
                    for op, args in (
                        ("memory_save", {"scope": team(dept), "content": f"Reader {user['id']} write attempt."}),
                        ("memory_update", {"scope": team(dept), "id": own["current"], "expected_revision": 1,
                                           "content": "Reader overwrite.", "reason": "attempt"}),
                        ("memory_delete", {"scope": team(dept), "id": own["current"], "expected_revision": 1,
                                           "reason": "attempt"}),
                    ):
                        status, body = client.call(token, op, args)
                        record("6-reader-write", user, dept, op, status, body, target=dept)
                # 7. revoked token
                path = scratch / "tokens" / f"{user['id']}-revoked.token"
                admin(root, env, "token-create", user["id"], "--client", "revoked", "--out", str(path))
                extra_token = path.read_text().strip()
                status, body = client.call(extra_token, "memory_scopes")
                record("7-revoked-token-control", user, dept, "memory_scopes", status, body, target=dept)
                admin(root, env, "token-revoke", "--file", str(path))
                for op, args in (("memory_scopes", {}), ("memory_recall", {"query": data["departments"][dept][0]["content"]}),
                                 ("memory_get", {"scope": team(dept), "id": 1}),
                                 ("memory_save", {"content": "after revoke", "request_id": str(uuid.uuid4())})):
                    status, body = client.call(extra_token, op, args)
                    record("7-revoked-token", user, dept, op, status, body, target=dept)
                # 9. exact-id tools with foreign ids but own scope (ids are per-workspace)
                for f in foreign:
                    for fact in picks[f][:IDS_PER_FOREIGN_DEPT]:
                        foreign_id = state["facts"][fact["key"]]["current"]
                        for op, args in (("memory_get", {"scope": team(dept), "id": foreign_id}),
                                         ("memory_history", {"scope": team(dept), "id": foreign_id}),
                                         ("memory_get", {"id": foreign_id}),
                                         ("memory_export", {"scope": team(dept), "after": max(0, foreign_id - 1), "limit": 1})):
                            status, body = client.call(token, op, args)
                            returned = (body.get("data") or {}) if isinstance(body.get("data"), dict) else None
                            record("9-foreign-id-own-scope", user, dept, op, status, body, target=f,
                                   foreign_content_returned=bool(returned and returned.get("content") in
                                                                 (fact["content"], fact["content_v2"])))
                # control: own questions, top-5
                for fact in data["departments"][dept]:
                    for q in fact["questions"]:
                        status, body = client.call(token, "memory_recall", {"query": q["question"], "limit": CONTROL_TOP})
                        top = ranked_canaries(body)
                        record("control-own", user, dept, "memory_recall", status, body, target=dept, qid=q["qid"],
                               gold_in_top5=any(fact["canary"] in c for c in top[:CONTROL_TOP]),
                               gold_at_1=bool(top) and fact["canary"] in top[0])
                log(f"E1 {user['id']}: {time.monotonic() - started:.1f}s")
            # 8. membership removed: first requests after removal
            for dept in DEPARTMENTS:
                user = next(u for u in data["users"] if u["id"] == f"{dept}-editor2")
                token = tokens[user["id"]]
                questions = [q["question"] for f in data["departments"][dept][:REMOVAL_QUERIES] for q in f["questions"][:1]]
                for question in questions[:5]:
                    client.call(token, "memory_recall", {"query": question, "limit": LIMIT})
                admin(root, env, "member", user["id"], dept, "remove")
                for index, question in enumerate(questions):
                    status, body = client.call(token, "memory_recall", {"query": question, "limit": LIMIT})
                    verdict = classify(body, user["id"], set())
                    rows.append({"experiment": "E1", "system": "team-server", "attempt": "8-removed-member",
                                 "user": user["id"], "role": user["role"], "department": dept, "op": "memory_recall",
                                 "http_status": status, "error": error_of(status, body), "sequence": index, **verdict,
                                 "target": dept})
                status, body = client.call(token, "memory_get", {"scope": team(dept), "id": 1})
                rows.append({"experiment": "E1", "system": "team-server", "attempt": "8-removed-member",
                             "user": user["id"], "role": user["role"], "department": dept, "op": "memory_get",
                             "http_status": status, "error": error_of(status, body), "sequence": REMOVAL_QUERIES,
                             **classify(body, user["id"], set()), "target": dept})
                admin(root, env, "member", user["id"], dept, "editor")
            after = snapshot(client, tokens, state)
            integrity = {dept: {"records_before": len(before[dept]), "records_after": len(after[dept]),
                                "unchanged": before[dept] == after[dept]} for dept in DEPARTMENTS}
            client.close()
    return {"rows": rows, "integrity": integrity, "twin_saves": state["twins"]}

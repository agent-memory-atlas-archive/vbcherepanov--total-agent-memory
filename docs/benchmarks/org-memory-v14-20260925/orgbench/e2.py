"""E2: new employee retrieval (no LLM), transfer between departments, offboarding."""
import json
import random
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from orgbench import company
from orgbench.data import DEPARTMENTS
from orgbench.harness import Client, Server, admin, base_env, scratch_dir, team
from orgbench.leaks import classify

LIMIT = 10
TRANSFER_SERIES = 50
FLIPS = 20
FLIP_READERS = 3
FLIP_GAP_SECONDS = (1.0, 3.0)
WRITE_RACES = 40
RACE_DELAYS_MS = (0, 2, 5, 10, 20, 40)


def rank_of(body, dept, record_id):
    for rank, item in enumerate(body.get("results", []), 1):
        if item["scope"].get("team_id") == dept and item["record"]["id"] == record_id:
            return rank
    return None


def retrieval(client, token, data, state, dept, variant, scope):
    rows = []
    for fact in data["departments"][dept]:
        entry = state["facts"][fact["key"]]
        for q in fact["questions"]:
            args = {"query": q["question"], "limit": LIMIT}
            if scope:
                args["scope"] = scope
            status, body = client.call(token, "memory_recall", args)
            gold = rank_of(body, dept, entry["current"]) if status == 200 else None
            stale = rank_of(body, dept, entry["v1"]) if status == 200 and fact["content_v2"] else None
            rows.append({"experiment": "E2a", "variant": variant, "department": dept, "qid": q["qid"],
                         "kind": q["kind"], "updated": bool(fact["content_v2"]), "http_status": status,
                         "gold_rank": gold, "stale_rank": stale,
                         "result_scopes": [i["scope"].get("team_id") or i["scope"]["kind"] for i in body.get("results", [])],
                         "result_ids": [[i["scope"].get("team_id") or i["scope"]["kind"], i["record"]["id"]]
                                        for i in body.get("results", [])],
                         "ordering": body.get("ordering")})
    return rows


def quality(data: dict, workers: int, log=print) -> list[dict]:
    """E2a variants only, plus D: an existing editor (with personal notes) asking without a scope."""
    rows = []
    with scratch_dir("orgbench-quality-") as scratch:
        env = base_env(scratch, workers)
        root = scratch / "root"
        tokens = company.create_identities(root, env, data, scratch / "tokens")
        for dept in DEPARTMENTS:
            admin(root, env, "user-add", f"new-{dept}", f"New {dept}")
            admin(root, env, "member", f"new-{dept}", dept, "reader")
            path = scratch / "tokens" / f"new-{dept}.token"
            admin(root, env, "token-create", f"new-{dept}", "--client", "orgbench", "--out", str(path))
            tokens[f"new-{dept}"] = path.read_text().strip()
        with Server(root, env, scratch / "server.log") as server:
            client = Client(server.url)
            state = company.populate(client, data, tokens)
            for dept in DEPARTMENTS:
                rows += retrieval(client, tokens[f"new-{dept}"], data, state, dept, "B-reader", None)
                rows += retrieval(client, tokens[f"new-{dept}"], data, state, dept, "C-team-only", team(dept))
                rows += retrieval(client, tokens[f"{dept}-editor1"], data, state, dept, "D-editor-all-scopes", None)
                log(f"quality {dept} done")
            client.close()
    return rows


def run(data: dict, out: Path, workers: int, log=print) -> dict:
    rows = []
    notes = {}
    with scratch_dir("orgbench-e2-") as scratch:
        env = base_env(scratch, workers)
        root = scratch / "root"
        tokens = company.create_identities(root, env, data, scratch / "tokens")
        for dept in DEPARTMENTS:
            admin(root, env, "user-add", f"new-{dept}", f"New {dept}")
            path = scratch / "tokens" / f"new-{dept}.token"
            admin(root, env, "token-create", f"new-{dept}", "--client", "orgbench", "--out", str(path))
            tokens[f"new-{dept}"] = path.read_text().strip()
        with Server(root, env, scratch / "server.log") as server:
            client = Client(server.url)
            state = company.populate(client, data, tokens)
            # E2a: A = not a member, B = reader (all scopes), C = ceiling (team scope only)
            for dept in DEPARTMENTS:
                token = tokens[f"new-{dept}"]
                rows += retrieval(client, token, data, state, dept, "A-not-member", None)
                admin(root, env, "member", f"new-{dept}", dept, "reader")
                rows += retrieval(client, token, data, state, dept, "B-reader", None)
                rows += retrieval(client, token, data, state, dept, "C-team-only", team(dept))
                log(f"E2a {dept} done")
            from team_memory.database_config import open_control_plane
            from team_memory.registry import Registry
            registry = Registry(root, open_control_plane(root, env))
            # Transfer: engineering-editor2 moves to sales. Sequential series right after the change.
            mover = "engineering-editor2"
            token = tokens[mover]
            eng_q = [q["question"] for f in data["departments"]["engineering"] for q in f["questions"]]
            sales_q = [q["question"] for f in data["departments"]["sales"] for q in f["questions"]]
            for question in eng_q[:10]:
                client.call(token, "memory_recall", {"query": question, "limit": LIMIT})
            changed_at = time.monotonic()
            registry.membership(mover, "engineering", None)
            registry.membership(mover, "sales", "editor")
            for index in range(TRANSFER_SERIES):
                question = eng_q[index] if index % 2 == 0 else sales_q[index]
                sent = time.monotonic()
                status, body = client.call(token, "memory_recall", {"query": question, "limit": LIMIT})
                verdict = classify(body, mover, {"sales"})
                rows.append({"experiment": "E2-transfer", "phase": "sequential", "sequence": index,
                             "sent_after_change_ms": (sent - changed_at) * 1000, "http_status": status,
                             "asked": "engineering" if index % 2 == 0 else "sales", **verdict,
                             "sales_visible": any(c.startswith("CANARY-sales") for c in verdict["own_canaries"])})
            for op, args in (("memory_get", {"scope": team("engineering"), "id": 1}),
                             ("memory_export", {"scope": team("engineering")}),
                             ("memory_save", {"scope": team("engineering"), "content": "Moved user write"}),
                             ("memory_save", {"scope": team("sales"), "content": "Moved user writes to sales now."})):
                status, body = client.call(token, op, args)
                rows.append({"experiment": "E2-transfer", "phase": "exact-tools", "op": op,
                             "scope": args["scope"]["team_id"], "http_status": status, "code": body.get("code"),
                             **classify(body, mover, {"sales"})})
            # Concurrent reader during repeated flips: does any request sent after a change see the old access?
            events, stop = [], threading.Event()

            def reader_loop():
                own = Client(server.url)
                i = 0
                while not stop.is_set():
                    question = eng_q[i % len(eng_q)]
                    sent = time.monotonic()
                    status, body = own.call(token, "memory_recall", {"query": question, "limit": LIMIT})
                    done = time.monotonic()
                    events.append((sent, done, status, body))
                    i += 1
                own.close()

            flips = []
            readers = [threading.Thread(target=reader_loop) for _ in range(FLIP_READERS)]
            for thread in readers:
                thread.start()
            rng = random.Random(data["seed"] + 2)
            member_of = "sales"
            for _ in range(FLIPS):
                time.sleep(rng.uniform(*FLIP_GAP_SECONDS))
                target = "engineering" if member_of == "sales" else "sales"
                at = time.monotonic()
                registry.membership(mover, member_of, None)
                registry.membership(mover, target, "editor")
                done = time.monotonic()
                flips.append((at, done, target))
                member_of = target
            time.sleep(FLIP_GAP_SECONDS[1])
            stop.set()
            for thread in readers:
                thread.join()

            def membership_at(t):
                current = "sales"
                for at, done, target in flips:
                    if t >= done:
                        current = target
                return current

            def ambiguous(sent, done):
                return any(sent <= f_done and done >= f_at for f_at, f_done, _ in flips)

            from orgbench.leaks import canaries, owner
            for sent, done, status, body in events:
                at_send, at_done = membership_at(sent), membership_at(done)
                seen = sorted({owner(c) for c in canaries(body)[0]} - {"shared"}) if status == 200 else []
                rows.append({"experiment": "E2-transfer", "phase": "concurrent-flips", "http_status": status,
                             "code": body.get("code"), "member_at_send": at_send, "member_at_done": at_done,
                             "overlaps_change": ambiguous(sent, done), "latency_ms": (done - sent) * 1000,
                             "teams_seen": seen,
                             "strict_leak": [t for t in seen if t not in (at_send, at_done)],
                             "old_team_after_send": [t for t in seen if t != at_send]})
            registry.membership(mover, "sales", None)
            registry.membership(mover, "engineering", "editor")
            # Write racing with a membership removal: can the client see an error for a write that committed?
            racer = "finance-editor2"
            witness = tokens["finance-editor1"]
            race_rows = []
            for i in range(WRITE_RACES):
                registry.membership(racer, "finance", "editor")
                marker = f"race-{i}-{uuid.uuid4().hex[:8]}"
                content = f"Race write {marker}: " + " ".join(["finance close checklist"] * 400)
                result = {}

                def writer(result=result, content=content):
                    result["reply"] = Client(server.url).call(tokens[racer], "memory_save",
                                                              {"scope": team("finance"), "content": content})

                thread = threading.Thread(target=writer)
                thread.start()
                time.sleep(RACE_DELAYS_MS[i % len(RACE_DELAYS_MS)] / 1000)
                registry.membership(racer, "finance", None)
                thread.join()
                status, body = result["reply"]
                found = client.call(witness, "memory_recall", {"query": f"Race write {marker}", "limit": 3,
                                                               "scope": team("finance")})[1]
                committed = any(marker in item["record"]["content"] for item in found.get("results", []))
                race_rows.append({"experiment": "E2-write-race", "attempt": i, "delay_ms": RACE_DELAYS_MS[i % len(RACE_DELAYS_MS)],
                                  "http_status": status, "code": body.get("code"), "committed": committed,
                                  "error_but_committed": status != 200 and committed})
            rows += race_rows
            registry.membership(racer, "finance", "editor")
            # Offboarding: sales-editor1 loses tokens and membership.
            leaver = "sales-editor1"
            token = tokens[leaver]
            leaver_facts = [f for f in data["departments"]["sales"] if state["facts"][f["key"]]["author_v1"] == leaver]
            admin(root, env, "token-revoke", "--file", str(scratch / "tokens" / f"{leaver}.token"))
            admin(root, env, "member", leaver, "sales", "remove")
            for op, args in (("memory_scopes", {}), ("memory_recall", {"query": leaver_facts[0]["content"]}),
                             ("memory_get", {"id": state["personal"][leaver][0]}),
                             ("memory_save", {"content": "after offboarding"})):
                status, body = client.call(token, op, args)
                rows.append({"experiment": "E2-offboarding", "check": "leaver-request", "op": op, "http_status": status,
                             "error": body.get("error")})
            for colleague in ("sales-editor2", "sales-reader"):
                visible = preserved = 0
                for fact in leaver_facts:
                    entry = state["facts"][fact["key"]]
                    status, body = client.call(tokens[colleague], "memory_get", {"scope": team("sales"), "id": entry["current"]})
                    if status == 200:
                        visible += 1
                        preserved += body["data"]["created_by"]["user_id"] == leaver
                rows.append({"experiment": "E2-offboarding", "check": "colleague-access", "colleague": colleague,
                             "leaver_records": len(leaver_facts), "visible": visible, "created_by_preserved": preserved})
            # Personal area: can anyone reach it through the API?
            leaver_note = data["personal"][leaver][0]["content"]
            for other in ("sales-editor2", "engineering-editor1"):
                status, body = client.call(tokens[other], "memory_recall", {"query": leaver_note, "limit": LIMIT})
                rows.append({"experiment": "E2-offboarding", "check": "personal-search-by-other", "user": other,
                             "http_status": status, **classify(body, other, {o["team"] for o in data["users"] if o["id"] == other})})
                status, body = client.call(tokens[other], "memory_get", {"scope": {"kind": "personal", "owner_id": leaver},
                                                                         "id": state["personal"][leaver][0]})
                rows.append({"experiment": "E2-offboarding", "check": "personal-owner-id-scope", "user": other,
                             "http_status": status, "error": (body.get("error") or "")[:160]})
            # Read through the control plane so the check is the same on SQLite and PostgreSQL.
            from tam_db.contracts import ControlKind
            from team_memory.offboarding import personal_key
            workspace_key = personal_key(registry, leaver)
            with registry.plane.workspace_reader(workspace_key) as db:
                on_disk = None if db is None else \
                    db.execute("SELECT COUNT(*) FROM knowledge WHERE status='active'").fetchone()[0]
            with registry.plane.connect(ControlKind.IDENTITY) as db:
                user_row = db.execute("SELECT active FROM users WHERE id=?", (leaver,)).fetchone()
                live_tokens = db.execute("SELECT COUNT(*) FROM tokens WHERE user_id=? AND revoked=0", (leaver,)).fetchone()[0]
                events_log = [tuple(r) for r in db.execute("SELECT action, subject FROM admin_events WHERE subject LIKE ?",
                                                           (leaver + "%",))]
            notes["personal_after_offboarding"] = {"workspace_exists": registry.plane.workspaces.exists(workspace_key),
                                                   "active_records_on_disk": on_disk,
                                                   "users.active": user_row[0], "unrevoked_tokens": live_tokens,
                                                   "admin_events": events_log}
            # Re-issue: an admin can issue a new token for the same user id; the personal area comes back.
            path = scratch / "tokens" / f"{leaver}-reissued.token"
            admin(root, env, "token-create", leaver, "--client", "reissued", "--out", str(path))
            status, body = client.call(path.read_text().strip(), "memory_recall", {"query": leaver_note, "limit": 3,
                                                                                    "scope": {"kind": "personal"}})
            notes["personal_after_token_reissue"] = {"http_status": status,
                                                     "own_note_found": data["personal"][leaver][0]["canary"] in json.dumps(body)}
            # Offboarding when the admin does not hold the user's token file.
            second = "hr-editor1"
            if "user-disable" in admin(root, env, "--help").stdout:
                admin(root, env, "user-disable", second)
                admin(root, env, "member", second, "hr", "remove")
                replies = [client.call(tokens[second], op, args)[0] for op, args in
                           (("memory_scopes", {}), ("memory_recall", {"query": "salary band"}),
                            ("memory_save", {"scope": {"kind": "shared"}, "content": "after user-disable"}))]
                reissue = subprocess.run([sys.executable, "-m", "team_memory.cli", "--root", str(root), "token-create",
                                          second, "--client", "reissue", "--out", str(scratch / "tokens" / "reissue2.token")],
                                         env=env, capture_output=True, text=True, check=False)
                notes["user_disable"] = {"available": True, "leaver_http_statuses": replies,
                                         "token_reissue_exit_code": reissue.returncode}
            else:
                shared_write = client.call(tokens[second], "memory_save", {"scope": {"kind": "shared"},
                                                                           "content": "Written after member remove only."})
                admin(root, env, "member", second, "hr", "remove")
                after_remove = client.call(tokens[second], "memory_save", {"scope": {"kind": "shared"},
                                                                           "content": "Written after member remove only."})
                notes["user_disable"] = {"available": False,
                                         "without_token_file_only_member_remove_possible": True,
                                         "shared_write_after_member_remove_status": after_remove[0],
                                         "control_status": shared_write[0]}
            client.close()
        snapshot = scratch / "backup"
        admin(root, env, "backup", "--out", str(snapshot))
        manifest = json.loads((snapshot / "manifest.json").read_text())
        # SQLite manifests list database files, PostgreSQL manifests (pg_backup) list workspace keys.
        notes["backup_contains_leaver_personal"] = (
            workspace_key in manifest.get("workspaces", [])
            or any(workspace_key in entry["path"] for entry in manifest.get("databases", [])))
        cli_help = admin(root, env, "--help").stdout
        notes["cli_commands"] = cli_help.split("{", 1)[1].split("}", 1)[0] if "{" in cli_help else cli_help
    return {"rows": rows, "notes": notes}

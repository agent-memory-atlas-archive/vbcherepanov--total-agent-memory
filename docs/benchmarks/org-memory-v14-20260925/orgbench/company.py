"""Populate a temp team server with the synthetic company through the public API."""
from pathlib import Path

from orgbench.data import DEPARTMENTS
from orgbench.harness import Client, admin, team

PROJECT = "company"


class SetupError(RuntimeError):
    pass


def must(status_body, what):
    status, body = status_body
    if status != 200:
        raise SetupError(f"{what}: HTTP {status} {body}")
    return body


def create_identities(root: Path, env: dict, data: dict, tokens_dir: Path, departments=DEPARTMENTS) -> dict:
    tokens_dir.mkdir(parents=True, exist_ok=True)
    for dept in departments:
        admin(root, env, "team-add", dept, dept.title())
    tokens = {}
    for user in data["users"]:
        if user["team"] not in departments:
            continue
        admin(root, env, "user-add", user["id"], user["name"])
        admin(root, env, "member", user["id"], user["team"], user["role"])
        path = tokens_dir / f"{user['id']}.token"
        admin(root, env, "token-create", user["id"], "--client", "orgbench", "--out", str(path))
        tokens[user["id"]] = path.read_text().strip()
    return tokens


def populate(client: Client, data: dict, tokens: dict, departments=DEPARTMENTS, personal=True, twins=True) -> dict:
    """Returns ids: facts[key] = {scope, v1, current, author_v1, author_v2}."""
    state = {"facts": {}, "shared": {}, "personal": {}, "twins": {}}
    for dept in departments:
        editors = [f"{dept}-editor1", f"{dept}-editor2"]
        for i, fact in enumerate(data["departments"][dept]):
            author = editors[i % 2]
            body = must(client.call(tokens[author], "memory_save", {
                "scope": team(dept), "content": fact["content"], "project": PROJECT, "tags": [dept, fact["category"]]}),
                f"save {fact['key']}")
            if body["data"].get("saved") is False:
                raise SetupError(f"save {fact['key']} rejected: {body}")
            state["facts"][fact["key"]] = {"scope": dept, "v1": body["data"]["id"], "current": body["data"]["id"],
                                           "revision": body["data"]["revision"], "author_v1": author}
    for dept in departments:
        for fact in data["departments"][dept]:
            if not fact["content_v2"]:
                continue
            entry = state["facts"][fact["key"]]
            editor = f"{dept}-editor2" if entry["author_v1"].endswith("editor1") else f"{dept}-editor1"
            body = must(client.call(tokens[editor], "memory_update", {
                "scope": team(dept), "id": entry["v1"], "expected_revision": entry["revision"],
                "content": fact["content_v2"], "reason": "Value changed"}), f"update {fact['key']}")
            entry.update(current=body["data"]["id"], author_v2=editor)
    first_user = next(u["id"] for u in data["users"] if u["team"] in departments)
    for i, fact in enumerate(data["shared"]):
        body = must(client.call(tokens[first_user], "memory_save", {
            "scope": {"kind": "shared"}, "content": fact["content"], "project": PROJECT, "tags": ["company"]}),
            f"save {fact['key']}")
        state["shared"][fact["key"]] = body["data"]["id"]
    if personal:
        for user_id, notes in data["personal"].items():
            if user_id not in tokens:
                continue
            state["personal"][user_id] = []
            for note in notes:
                body = must(client.call(tokens[user_id], "memory_save", {
                    "content": note["content"], "project": PROJECT}), f"personal {user_id}")
                state["personal"][user_id].append(body["data"]["id"])
    if twins:
        for twin in data["twins"]:
            ids = {}
            for dept in twin["departments"]:
                if dept not in departments:
                    continue
                body = must(client.call(tokens[f"{dept}-editor1"], "memory_save", {
                    "scope": team(dept), "content": twin["content"], "project": PROJECT}), f"twin {twin['key']}")
                ids[dept] = {"id": body["data"]["id"], "deduplicated": body["data"].get("deduplicated")}
            state["twins"][twin["key"]] = ids
    return state

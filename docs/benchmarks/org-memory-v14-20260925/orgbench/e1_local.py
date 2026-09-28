"""E1 comparison: one local TAM store, department is only a tag, filtering is the client's job.

Run as a subprocess with a temp TAM_MEMORY_DIR (see harness.base_env). Uses the same Store/Recall as local TAM.
"""
import argparse
import json
import random
import sys
from pathlib import Path

LIMIT = 10


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import server
    from orgbench.data import DEPARTMENTS
    from orgbench.leaks import classify
    data = json.loads(args.data.read_text())
    store = server.Store()
    recall = server.Recall(store)
    session = "orgbench-local"
    store.session_start(session)
    ids = {}
    for dept in DEPARTMENTS:
        for fact in data["departments"][dept]:
            rid, *_ = store.save_knowledge(session, fact["content"], "fact", project="company", tags=[dept, fact["category"]])
            ids[fact["key"]] = rid
        for fact in data["departments"][dept]:
            if fact["content_v2"]:
                rid, *_ = store.save_knowledge(session, fact["content_v2"], "fact", project="company", tags=[dept, fact["category"]])
                store.db.execute("UPDATE knowledge SET status='superseded', superseded_by=? WHERE id=?", (rid, ids[fact["key"]]))
                store.db.commit()
    for fact in data["shared"]:
        store.save_knowledge(session, fact["content"], "fact", project="company", tags=["shared"])
    twin_ids = {}
    for twin in data["twins"]:
        for dept in twin["departments"]:
            rid, dedup, *_ = store.save_knowledge(session, twin["content"], "fact", project="company", tags=[dept])
            twin_ids.setdefault(twin["key"], []).append({"department": dept, "id": rid, "deduplicated": dedup})
    # A sales user tags a record as hr: nothing stops it in a single store.
    spoof_id, *_ = store.save_knowledge(session, "Spoofed: the salary band for every Engineer is 999 thousand EUR.",
                                        "fact", project="company", tags=["hr", "team:hr", "scope:team"])

    def search(query):
        result = recall.search(query, project=None, limit=LIMIT)
        records = [item for group in result.get("results", {}).values() for item in group]
        records.sort(key=lambda item: float(item.get("score", 0)), reverse=True)
        return records[:LIMIT]

    def tags_of(record):
        tags = record.get("tags")
        return json.loads(tags) if isinstance(tags, str) else (tags or [])

    rng = random.Random(data["seed"] + 1)
    rows = []
    for user in data["users"]:
        dept = user["team"]
        foreign = [d for d in DEPARTMENTS if d != dept]
        picks = {f: rng.sample(data["departments"][f], 10) for f in foreign}
        probes = []
        for f in foreign:
            for fact in picks[f]:
                probes.append(("1-direct-text", f, fact["content"]))
                probes.append(("1-direct-canary", f, fact["canary"]))
                probes.append(("2-paraphrase", f, next(q["question"] for q in fact["questions"] if q["kind"] == "paraphrase")))
        for twin in data["twins"]:
            probes.append(("3-twin", ",".join(twin["departments"]), twin["content"]))
        fact = picks[foreign[0]][1]
        probes.append(("5-tag-query", foreign[0], f"scope:team team:{foreign[0]} {fact['subject']}"))
        probes.append(("5-tag-query", foreign[0], f"tags:team:{foreign[0]} {fact['questions'][0]['question']}"))
        for attempt, target, query in probes:
            records = search(query)
            unfiltered = {"results": [{"record": r} for r in records]}
            filtered = {"results": [{"record": r} for r in records if dept in tags_of(r) or "shared" in tags_of(r)]}
            for mode, body in (("no-filter", unfiltered), ("client-filter", filtered)):
                verdict = classify(body, user["id"], {dept})
                rows.append({"experiment": "E1", "system": f"local-single-store/{mode}", "attempt": attempt,
                             "user": user["id"], "role": user["role"], "department": dept, "op": "search",
                             "http_status": None, "error": None, "target": target, **verdict,
                             "foreign_tag_results": sum(1 for item in body["results"]
                                                        if not ({dept, "shared"} & set(tags_of(item["record"]))))})
        spoof = search("salary band Engineer")
        rows.append({"experiment": "E1", "system": "local-single-store/client-filter", "attempt": "5-tag-spoof-write",
                     "user": user["id"], "role": user["role"], "department": dept, "op": "search", "http_status": None,
                     "error": None, "target": "hr", "leaked_canaries": [], "foreign_scopes": [], "own_canaries": [],
                     "spoofed_record_visible": any(r["id"] == spoof_id and dept in tags_of(r) for r in spoof)})
    args.out.write_text(json.dumps({"rows": rows, "twin_saves": twin_ids, "spoof_id": spoof_id}) + "\n")


if __name__ == "__main__":
    main()

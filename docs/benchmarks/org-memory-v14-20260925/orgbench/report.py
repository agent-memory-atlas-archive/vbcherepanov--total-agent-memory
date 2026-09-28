"""Aggregate raw JSONL into summary.json and markdown tables."""
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path


def load(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def pct(values, q):
    ordered = sorted(values)
    if not ordered:
        return None
    return ordered[max(0, round(q * len(ordered)) - 1)]


def e1(rows):
    groups = defaultdict(list)
    for r in rows:
        if r["attempt"] == "control-own":
            continue
        groups[(r["system"], r["attempt"])].append(r)
    table = []
    for (system, attempt), items in sorted(groups.items()):
        leaked_calls = sum(1 for r in items if r["leaked_canaries"] or r["foreign_scopes"])
        distinct = sorted({c for r in items for c in r["leaked_canaries"]})
        codes = Counter(f"{r['http_status']}:{(r.get('error') or {}).get('code') if isinstance(r.get('error'), dict) else r.get('error')}"
                        for r in items)
        extra = {}
        if attempt == "3-twin" and system == "team-server":
            extra["own_twin_found"] = sum(1 for r in items if r.get("own_twin_expected") and r.get("twin_hits"))
            extra["own_twin_expected"] = sum(1 for r in items if r.get("own_twin_expected"))
        if attempt == "9-foreign-id-own-scope":
            extra["foreign_content_returned"] = sum(1 for r in items if r.get("foreign_content_returned"))
        if attempt == "5-tag-spoof-write":
            extra["spoofed_record_visible"] = sum(1 for r in items if r.get("spoofed_record_visible"))
        if "foreign_tag_results" in items[0]:
            extra["foreign_tag_results"] = sum(r["foreign_tag_results"] for r in items)
        if attempt == "6-reader-write" or attempt == "4-foreign-team-id":
            extra["writes_accepted"] = sum(1 for r in items if r["op"] in ("memory_save", "memory_update", "memory_delete")
                                           and r["http_status"] == 200)
        table.append({"system": system, "attempt": attempt, "calls": len(items), "calls_with_leak": leaked_calls,
                      "distinct_leaked_canaries": len(distinct), "responses": dict(codes), **extra})
    control = [r for r in rows if r["attempt"] == "control-own"]
    ctrl = {}
    if control:
        ctrl = {"questions": len(control), "gold_in_top5": sum(r["gold_in_top5"] for r in control) / len(control),
                "gold_at_1": sum(r["gold_at_1"] for r in control) / len(control),
                "any_own_canary_in_top5": sum(1 for r in control if r["own_canaries"]) / len(control)}
        by_role = defaultdict(list)
        for r in control:
            by_role[r["role"]].append(r["gold_in_top5"])
        ctrl["gold_in_top5_by_role"] = {k: sum(v) / len(v) for k, v in by_role.items()}
    return {"attempts": table, "control": ctrl}


def e2a(rows):
    out = []
    for variant in sorted({r["variant"] for r in rows}):
        items = [r for r in rows if r["variant"] == variant]
        n = len(items)
        upd = [r for r in items if r["updated"]]
        row = {"variant": variant, "questions": n,
               "hit@1": sum(1 for r in items if r["gold_rank"] == 1) / n,
               "hit@5": sum(1 for r in items if r["gold_rank"] and r["gold_rank"] <= 5) / n,
               "mrr@10": sum(1 / r["gold_rank"] for r in items if r["gold_rank"]) / n,
               "updated_questions": len(upd),
               "fresh@5": sum(1 for r in upd if r["gold_rank"] and r["gold_rank"] <= 5) / len(upd) if upd else None,
               "stale@5": sum(1 for r in upd if r["stale_rank"] and r["stale_rank"] <= 5) / len(upd) if upd else None,
               "fresh_share_of_found": (sum(1 for r in upd if r["gold_rank"] and (not r["stale_rank"] or r["gold_rank"] < r["stale_rank"]))
                                        / max(1, sum(1 for r in upd if r["gold_rank"] or r["stale_rank"]))) if upd else None,
               "by_kind_hit@5": {k: sum(1 for r in items if r["kind"] == k and r["gold_rank"] and r["gold_rank"] <= 5)
                                 / max(1, sum(1 for r in items if r["kind"] == k)) for k in ("direct", "paraphrase")},
               "non_team_results_in_top5": sum(sum(1 for s in r["result_scopes"][:5] if s in ("personal", "shared")) for r in items) / n}
        per_dept = {}
        for dept in sorted({r["department"] for r in items}):
            d = [r for r in items if r["department"] == dept]
            per_dept[dept] = sum(1 for r in d if r["gold_rank"] and r["gold_rank"] <= 5) / len(d)
        row["hit@5_by_department"] = per_dept
        out.append(row)
    return out


def e2(rows, notes):
    transfer_seq = [r for r in rows if r.get("experiment") == "E2-transfer" and r.get("phase") == "sequential"]
    exact = [r for r in rows if r.get("experiment") == "E2-transfer" and r.get("phase") == "exact-tools"]
    flips = [r for r in rows if r.get("experiment") == "E2-transfer" and r.get("phase") == "concurrent-flips"]
    races = [r for r in rows if r.get("experiment") == "E2-write-race"]
    off = [r for r in rows if r.get("experiment") == "E2-offboarding"]
    clean = [r for r in flips if not r["overlaps_change"]]
    overlap = [r for r in flips if r["overlaps_change"]]
    return {
        "transfer_sequential": {"requests": len(transfer_seq),
                                "first_request_ms_after_change": transfer_seq[0]["sent_after_change_ms"] if transfer_seq else None,
                                "requests_with_engineering_data": sum(1 for r in transfer_seq if r["leaked_canaries"] or r["foreign_scopes"]),
                                "first_request_leaked": bool(transfer_seq and (transfer_seq[0]["leaked_canaries"] or transfer_seq[0]["foreign_scopes"])),
                                "sales_visible_in": sum(1 for r in transfer_seq if r["sales_visible"])},
        "transfer_exact_tools": [{"op": r["op"], "scope": r["scope"], "status": r["http_status"], "code": r["code"]} for r in exact],
        "transfer_concurrent": {"requests": len(flips), "not_overlapping_change": len(clean),
                                "not_overlapping_with_old_access": sum(1 for r in clean if r["old_team_after_send"]),
                                "overlapping_change": len(overlap),
                                "overlapping_rejected": sum(1 for r in overlap if r["http_status"] != 200),
                                "overlapping_with_team_not_held_at_send": sum(1 for r in overlap if r["old_team_after_send"]),
                                "strict_leaks": sum(1 for r in flips if r["strict_leak"]),
                                "codes": dict(Counter(f"{r['http_status']}:{r['code']}" for r in flips)),
                                "latency_p50_ms": pct([r["latency_ms"] for r in flips], 0.5)},
        "write_race": {"attempts": len(races), "committed": sum(r["committed"] for r in races),
                       "client_error": sum(1 for r in races if r["http_status"] != 200),
                       "error_but_committed": sum(r["error_but_committed"] for r in races),
                       "codes": dict(Counter(f"{r['http_status']}:{r['code']}" for r in races))},
        "offboarding": off, "notes": notes,
    }


def e3(rows, server_rows):
    conc = defaultdict(list)
    history = []
    retries = []
    for r in rows:
        if r["experiment"] == "E3-concurrency":
            conc[r["clients"]].append(r)
        elif r["experiment"] == "E3-history":
            history.append(r)
        elif r["experiment"] == "E3-retry":
            retries.append(r)
    table = []
    for clients, items in sorted(conc.items()):
        lat = [x for r in items for x in r["latency_ms"]]
        table.append({"clients": clients, "rounds": len(items),
                      "rounds_exactly_one_success": sum(1 for r in items if r["successes"] == 1),
                      "successes": sum(r["successes"] for r in items), "conflicts": sum(r["conflicts"] for r in items),
                      "other_errors": sum(len(r["other_errors"]) for r in items),
                      "lost_updates": sum(r["lost_updates"] for r in items),
                      "latency_p50_ms": pct(lat, 0.5), "latency_p95_ms": pct(lat, 0.95), "latency_max_ms": max(lat)})
    retry_table = []
    all_retries = retries + [r for r in server_rows if r["experiment"] == "E3-retry"]
    for key in sorted({(r["timeout"], r["op"]) for r in all_retries}):
        items = [r for r in all_retries if (r["timeout"], r["op"]) == key]
        rejected = sum(1 for r in items if r.get("different_payload") == "conflict" or r.get("different_payload_code") == "conflict")
        retry_table.append({"timeout": key[0], "op": key[1], "reps": len(items),
                            "first_attempt_timed_out": sum(1 for r in items if r["first"] != "completed"),
                            "timed_out_but_committed": sum(1 for r in items if r["first"] != "completed" and r.get("first_committed")),
                            "exactly_one_effect": sum(1 for r in items if r["copies"] == 1),
                            "retry_same_result": sum(1 for r in items if r["retry_same_result"]),
                            "different_payload_rejected": rejected})
    cross = [r for r in server_rows if r["experiment"] == "E3-retry-cross-scope"]
    return {"concurrency": table, "history": history, "retries": retry_table, "cross_scope": cross}


def e4(rows):
    groups = defaultdict(list)
    for r in rows:
        groups[(r["system"], r["scopes"], r["workers"])].append(r)
    table = []
    for (system, scopes, workers), items in sorted(groups.items(), key=lambda kv: (kv[0][1], kv[0][0] != "team-server", kv[0][2])):
        samples = [x for r in items for x in r["warm_samples_ms"]]
        table.append({"system": system, "scopes": scopes, "workers": workers, "passes": len(items),
                      "records": items[0]["records_searchable"],
                      "cold_first_query_ms": statistics.mean(r["cold_first_query_ms"] for r in items),
                      "warm_p50_ms": pct(samples, 0.5), "warm_p95_ms": pct(samples, 0.95),
                      "worker_rss_sum_mib": statistics.mean(r["worker_rss_sum_mib"] for r in items),
                      "gateway_rss_mib": statistics.mean(r["gateway_rss_mib"] for r in items),
                      "workers_alive_end": items[0]["workers_alive_end"],
                      "worker_processes_started": statistics.mean(r["worker_processes_started"] for r in items),
                      "worker_cpu_s_total": statistics.mean(r["worker_cpu_s_total"] for r in items),
                      "worker_cpu_s_warm_batch": statistics.mean(r["worker_cpu_s_warm_batch"] for r in items),
                      "gateway_cpu_s": statistics.mean(r["gateway_cpu_s"] for r in items),
                      "load1_before": [r["load_before"][0] for r in items], "load1_after": [r["load_after"][0] for r in items]})
    return table


def summarise(out: Path) -> dict:
    result = {}
    e1_rows = load(out / "e1.jsonl") + load(out / "e1-local.jsonl")
    if e1_rows:
        result["e1"] = e1(e1_rows)
        meta = out / "e1-meta.json"
        if meta.exists():
            result["e1"]["meta"] = json.loads(meta.read_text())
        meta = out / "e1-local-meta.json"
        if meta.exists():
            result["e1"]["local_meta"] = json.loads(meta.read_text())
    e2_rows = load(out / "e2.jsonl")
    if e2_rows:
        notes = json.loads((out / "e2-notes.json").read_text())
        result["e2a"] = e2a([r for r in e2_rows if r.get("experiment") == "E2a"])
        result["e2"] = e2(e2_rows, notes)
    e3_rows = load(out / "e3.jsonl")
    if e3_rows:
        result["e3"] = e3(e3_rows, load(out / "e3-server-timeout.jsonl"))
    quality_rows = load(out / "quality.jsonl")
    if quality_rows:
        result["quality"] = e2a(quality_rows)
    multi = load(out / "e4-multiuser.jsonl")
    if multi:
        result["e4_multiuser"] = [{k: v for k, v in r.items() if k != "samples"} for r in multi]
    e4_rows = load(out / "e4.jsonl")
    if e4_rows:
        result["e4"] = e4(e4_rows)
    return result


def _fmt(v):
    if isinstance(v, float):
        return f"{v:.3f}" if v < 10 else f"{v:.1f}"
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def table(rows, columns):
    if not rows:
        return ""
    lines = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    for r in rows:
        lines.append("| " + " | ".join(_fmt(r.get(c, "")) for c in columns) + " |")
    return "\n".join(lines)


def markdown(summary: dict) -> str:
    parts = []
    if "e1" in summary:
        parts.append("## E1\n" + table(summary["e1"]["attempts"], ["system", "attempt", "calls", "calls_with_leak",
                                                                    "distinct_leaked_canaries", "responses", "writes_accepted",
                                                                    "foreign_tag_results", "own_twin_found",
                                                                    "own_twin_expected", "foreign_content_returned",
                                                                    "spoofed_record_visible"]))
        parts.append("control: " + json.dumps(summary["e1"]["control"]))
    if "e2a" in summary:
        parts.append("## E2a\n" + table(summary["e2a"], ["variant", "questions", "hit@1", "hit@5", "mrr@10", "fresh@5",
                                                         "stale@5", "fresh_share_of_found", "by_kind_hit@5",
                                                         "non_team_results_in_top5", "hit@5_by_department"]))
        parts.append("## E2\n```json\n" + json.dumps(summary["e2"], indent=1, ensure_ascii=False) + "\n```")
    if "quality" in summary:
        parts.append("## Quality\n" + table(summary["quality"], ["variant", "questions", "hit@1", "hit@5", "mrr@10",
                                                                 "fresh@5", "stale@5", "non_team_results_in_top5"]))
    if "e3" in summary:
        parts.append("## E3\n" + table(summary["e3"]["concurrency"], list(summary["e3"]["concurrency"][0].keys())))
        parts.append(table(summary["e3"]["retries"], list(summary["e3"]["retries"][0].keys())))
        parts.append("```json\n" + json.dumps({"history": summary["e3"]["history"], "cross_scope": summary["e3"]["cross_scope"]},
                                              indent=1) + "\n```")
    if "e4" in summary:
        parts.append("## E4\n" + table(summary["e4"], ["system", "scopes", "workers", "records", "cold_first_query_ms",
                                                       "warm_p50_ms", "warm_p95_ms", "worker_rss_sum_mib", "gateway_rss_mib",
                                                       "worker_processes_started", "worker_cpu_s_total",
                                                       "worker_cpu_s_warm_batch", "gateway_cpu_s", "load1_before", "load1_after"]))
    if "e4_multiuser" in summary:
        parts.append("## E4 multi-user\n" + table(summary["e4_multiuser"], [
            "run", "order", "searches", "cold_searches", "cold_share", "workers_started", "p50_ms", "p95_ms",
            "cold_p50_ms", "warm_p50_ms", "load_before", "load_after"]))
    return "\n\n".join(parts)

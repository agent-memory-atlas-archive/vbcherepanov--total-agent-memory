#!/usr/bin/env python3
"""Free, independent-set evaluation of context assembly through the bench TAM worker.

Datasets (never AMA-Bench / LongMemEval-V2):
  locomo : LoCoMo conversations 0-2, one store per conversation, one fragment per turn,
           source = session; gold = QA `evidence` dia_ids; categories 1-4 (5 = adversarial,
           no evidence) are excluded from coverage.
  lme    : LongMemEval-S first 100 questions ("dev-100"), one store per question, one
           fragment per turn (split at 2,000 chars on line boundaries, as the AMA adapter
           does), source = haystack session; gold = turns flagged has_answer (questions
           without such turns are skipped).

Per variant and character budget B, the context is what an adapter would hand the reader:
  base   : top_k=10 hits, whole, in rank order, stop at the first hit that overflows B
           (the pilot AMA adapter's assemble_context)
  fill   : search 100 deep, TAM budget fill (whole hits in rank order, skip non-fitting);
           the deep list is fetched once and the worker's own _fill runs per budget
  fill_rN: as fill, each hit with N same-session neighbours on each side (unit = hit + neighbours)
Metrics: cov_any (>=1 gold fragment in context), cov_all (every gold fragment), mean chars
used, and r@10 (>=1 gold in the top 10 hits, i.e. ranking only). No LLM, record_usage=False.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "docs" / "benchmarks"))
sys.path.insert(0, str(REPO / "src"))

from tam_bench_common.tam_worker import (
    TamStoreProcess,
    TamWorkerSettings,
    _fill,
)

LOCOMO = REPO / "benchmarks/data/locomo/data/locomo10.json"
LME = REPO / "benchmarks/data/longmemeval_s.json"
BUDGETS = {"locomo": (4000, 8000), "lme": (16000, 40000)}
SEPARATOR = 2
FILL_POOL = 100


def split_text(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > max_chars:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:max_chars])
            line = line[max_chars:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > max_chars:
            parts.append(current)
            current = line
        else:
            current = candidate
    if current.strip():
        parts.append(current)
    return [part for part in parts if part.strip()]


def locomo_units(limit_convs: int):
    data = json.loads(LOCOMO.read_text())
    for index, sample in enumerate(data[:limit_convs]):
        conv = sample["conversation"]
        keys = sorted((k for k in conv if k.startswith("session_") and not k.endswith("_date_time")),
                      key=lambda k: int(k.split("_")[1]))
        fragments = []
        for key in keys:
            date = conv.get(f"{key}_date_time", "")
            for turn in conv[key] if isinstance(conv[key], list) else []:
                if not turn.get("text") or not turn.get("dia_id"):
                    continue
                content = f"[{date}] {turn.get('speaker', '')}: {turn['text']}"
                if turn.get("blip_caption"):
                    content += f"\n(image: {turn['blip_caption']})"
                fragments.append({"index_text": content, "content": content,
                                  "session": key, "meta": {"source": key, "gold": turn["dia_id"]}})
        questions = []
        for qn, qa in enumerate(sample.get("qa", [])):
            if qa.get("category") == 5:
                continue
            gold = {g.strip() for raw in qa.get("evidence") or [] for g in re.split(r"[;,\s]+", raw) if g.strip()}
            if gold:
                questions.append({"id": f"c{index}q{qn}", "question": qa["question"], "gold": sorted(gold),
                                  "category": qa.get("category")})
        yield f"locomo_{index}", fragments, questions


def lme_units(limit_questions: int):
    data = json.loads(LME.read_text())
    for entry in data[:limit_questions]:
        fragments = []
        for sid, date, session in zip(entry["haystack_session_ids"], entry["haystack_dates"],
                                      entry["haystack_sessions"], strict=True):
            for tn, turn in enumerate(session):
                if not turn["content"].strip():
                    continue
                pieces = split_text(f"[{date}] {turn['role']}: {turn['content']}", 2000)
                for pn, piece in enumerate(pieces):
                    fragments.append({"index_text": piece, "content": piece, "session": sid,
                                      "meta": {"source": sid,
                                               "gold": f"{sid}:{tn}:{pn}" if turn.get("has_answer") else None}})
        gold = sorted(f["meta"]["gold"] for f in fragments if f["meta"]["gold"])
        questions = ([{"id": entry["question_id"], "question": entry["question"], "gold": gold,
                       "category": entry["question_type"]}] if gold else [])
        yield entry["question_id"], fragments, questions


def assemble_base(hits, budget):
    kept, used = [], 0
    for hit in hits:
        size = len(hit["content"]) + SEPARATOR
        if kept and used + size > budget:
            break
        kept.append(hit)
        used += size
    return kept


def evaluate(kind: str, variants: list[str], out_path: Path, limit: int, cross_rerank: str, work_root: str):
    settings = TamWorkerSettings(tam_src=str(REPO / "src"), project="eval", top_k=10, cross_rerank=cross_rerank,
                                 worker_timeout_s=3600, work_root=work_root,
                                 python_executable=sys.executable)
    units = locomo_units(limit) if kind == "locomo" else lme_units(limit)
    # Rows are appended per unit to a JSONL next to the output, so a killed run resumes
    # from the last finished unit instead of losing everything.
    rows_path = out_path.with_suffix(".rows.jsonl")
    rows = [json.loads(line) for line in rows_path.read_text().splitlines() if line.strip()] \
        if rows_path.exists() else []
    done_units = {row["unit"] for row in rows}
    timing = {"build_s": 0.0, "search_s": 0.0}
    started = time.time()
    for unit_id, fragments, questions in units:
        if not questions or unit_id in done_units:
            continue
        unit_start = len(rows)
        store = TamStoreProcess(settings, prefix="ctx-eval-")
        try:
            built = time.time()
            store.add(fragments)
            timing["build_s"] += time.time() - built
            radii = sorted({int(v[len("fill_r"):]) for v in variants if v.startswith("fill_r")})
            for q in questions:
                gold = set(q["gold"])
                row = {"unit": unit_id, **q, "results": {}}
                searched = time.time()
                top = store.search(q["question"], limit=10)
                deep = {0: store.search(q["question"], limit=FILL_POOL)}
                for radius in radii:
                    deep[radius] = store.search(q["question"], limit=FILL_POOL, radius=radius)
                timing["search_s"] += time.time() - searched
                row["r@10"] = any(h["meta"]["gold"] in gold for h in top)
                for budget in BUDGETS[kind]:
                    for variant in variants:
                        if variant == "base":
                            kept = assemble_base(top, budget)
                        elif variant == "fill":
                            kept = _fill(deep[0], budget, SEPARATOR, None)
                        elif variant.startswith("fill_r"):
                            kept = _fill(deep[int(variant[len("fill_r"):])], budget, SEPARATOR, None)
                        else:
                            raise ValueError(f"unknown variant {variant}")
                        found = {h["meta"]["gold"] for h in kept} & gold
                        row["results"][f"{variant}@{budget}"] = {
                            "any": bool(found), "all": found == gold, "n": len(kept),
                            "chars": sum(len(h["content"]) + SEPARATOR for h in kept)}
                rows.append(row)
        finally:
            store.close()
        rows_path.parent.mkdir(parents=True, exist_ok=True)
        with rows_path.open("a") as sink:
            for row in rows[unit_start:]:
                sink.write(json.dumps(row) + "\n")
        print(json.dumps({"unit": unit_id, "questions": len(rows), "elapsed_s": round(time.time() - started),
                          **{k: round(v) for k, v in timing.items()}}), flush=True)
    summary = {"kind": kind, "cross_rerank": cross_rerank, "questions": len(rows),
               "r@10": round(sum(r["r@10"] for r in rows) / len(rows), 4), "variants": {}}
    for key in rows[0]["results"]:
        vals = [r["results"][key] for r in rows]
        summary["variants"][key] = {
            "cov_any": round(sum(v["any"] for v in vals) / len(vals), 4),
            "cov_all": round(sum(v["all"] for v in vals) / len(vals), 4),
            "mean_records": round(sum(v["n"] for v in vals) / len(vals), 1),
            "mean_chars": round(sum(v["chars"] for v in vals) / len(vals)),
        }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"summary": summary, "rows": rows}, indent=1))
    print(json.dumps(summary, indent=1))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=["locomo", "lme"], required=True)
    parser.add_argument("--variants", default="base,fill")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--cross-rerank", default="on", choices=["on", "off"])
    parser.add_argument("--work-root", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    limit = args.limit if args.limit is not None else (3 if args.kind == "locomo" else 100)
    evaluate(args.kind, [v for v in args.variants.split(",") if v], args.output, limit, args.cross_rerank,
             args.work_root)


if __name__ == "__main__":
    main()

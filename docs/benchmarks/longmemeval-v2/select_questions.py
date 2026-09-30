#!/usr/bin/env python3
"""Pick a pilot subset of LongMemEval-V2 questions without looking at answers.

Within one domain the questions are stratified by question_type (largest-remainder
proportional allocation) and sampled with a fixed seed from the sorted question ids, so
the subset is reproducible and fixed before any run.

    python select_questions.py --data-root DATA --domain web --count 10
    -> prints a comma-separated id list for run_tam.py --question-ids
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

DEFAULT_SEED = 20260928


def allocate(sizes: dict[str, int], count: int) -> dict[str, int]:
    total = sum(sizes.values())
    if not 0 < count <= total:
        raise ValueError(f"count must be in 1..{total}")
    exact = {key: count * size / total for key, size in sizes.items()}
    quota = {key: int(value) for key, value in exact.items()}
    for key in sorted(exact, key=lambda k: (-(exact[k] - quota[k]), k))[:count - sum(quota.values())]:
        quota[key] += 1
    return quota


def select(questions: list[dict], domain: str, count: int, seed: int) -> list[str]:
    by_type: dict[str, list[str]] = defaultdict(list)
    for question in questions:
        if question.get("domain") == domain:
            by_type[str(question["question_type"])].append(str(question["id"]))
    if not by_type:
        raise ValueError(f"no questions for domain {domain!r}")
    quota = allocate({key: len(ids) for key, ids in by_type.items()}, count)
    rng = random.Random(f"{seed}:{domain}")
    chosen: list[str] = []
    for question_type in sorted(by_type):
        chosen.extend(rng.sample(sorted(by_type[question_type]), quota[question_type]))
    return sorted(chosen)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--domain", required=True, choices=["web", "enterprise"])
    parser.add_argument("--count", required=True, type=int)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()
    with (args.data_root / "questions.jsonl").open(encoding="utf-8") as handle:
        questions = [json.loads(line) for line in handle if line.strip()]
    print(",".join(select(questions, args.domain, args.count, args.seed)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

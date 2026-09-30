#!/usr/bin/env python3
"""Pick a pilot subset of AMA-Bench episodes, stratified by domain, without looking at
questions or answers.

Allocation is proportional to each domain's episode count (largest remainder), and the
episodes inside a domain are a seeded random sample of its sorted episode ids. The seed
is fixed in advance so the subset is reproducible and not chosen after seeing results.

    python select_episodes.py --test-file dataset/test/open_end_qa_set.jsonl --count 20
    -> prints a comma-separated id list for run.py --episode-ids
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict

DEFAULT_SEED = 20260928


def allocate(sizes: dict[str, int], count: int) -> dict[str, int]:
    total = sum(sizes.values())
    if not 0 < count <= total:
        raise ValueError(f"count must be in 1..{total}")
    exact = {domain: count * size / total for domain, size in sizes.items()}
    quota = {domain: int(value) for domain, value in exact.items()}
    remainder = count - sum(quota.values())
    for domain in sorted(exact, key=lambda d: (-(exact[d] - quota[d]), d))[:remainder]:
        quota[domain] += 1
    return quota


def select(episodes: list[dict], count: int, seed: int) -> list[str]:
    by_domain: dict[str, list[str]] = defaultdict(list)
    for episode in episodes:
        by_domain[str(episode["domain"])].append(str(episode["episode_id"]))
    quota = allocate({domain: len(ids) for domain, ids in by_domain.items()}, count)
    rng = random.Random(seed)
    chosen: list[str] = []
    for domain in sorted(by_domain):
        ids = sorted(by_domain[domain], key=int)
        chosen.extend(rng.sample(ids, quota[domain]))
    return sorted(chosen, key=int)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--test-file", required=True)
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()
    with open(args.test_file, encoding="utf-8") as handle:
        episodes = [json.loads(line) for line in handle if line.strip()]
    print(",".join(select(episodes, args.count, args.seed)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

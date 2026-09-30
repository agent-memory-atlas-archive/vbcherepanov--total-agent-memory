from __future__ import annotations

from collections.abc import Callable, Sequence

from memory_core.retrieval import MemoryHit
from memory_core.telemetry import counters

FILL_SEARCH_LIMIT = 100


def fill_budget(
    groups: Sequence[Sequence[MemoryHit]],
    max_chars: int,
    *,
    cost: Callable[[MemoryHit], int],
) -> list[MemoryHit]:
    """Keep ranked groups whole, in rank order, while they fit into ``max_chars``.

    A group is one search hit, or a hit together with the records around it. A fixed
    top-k leaves most of a context budget empty when records are short and cuts it off
    when they are long; filling by size spends the budget the caller has anyway on the
    next-best records. A group that does not fit is skipped, so a smaller later group can
    still use the room left. The first group is always kept, so a budget smaller than
    the best hit still returns it (the caller excerpts it). A record already kept through
    an earlier group is not counted twice.
    """
    if type(max_chars) is not int or max_chars < 1:
        raise ValueError("max_chars must be a positive integer")
    kept: list[MemoryHit] = []
    seen: set[object] = set()
    used = 0
    for group in groups:
        fresh = [hit for hit in group if _key(hit) not in seen]
        if not fresh:
            continue
        size = sum(cost(hit) for hit in fresh)
        if kept and used + size > max_chars:
            continue
        kept.extend(fresh)
        seen.update(_key(hit) for hit in fresh)
        used += size
        if used >= max_chars:
            break
    counters.bump("context_fill_records", len(kept))
    return kept


def group_by_anchor(evidence: Sequence[MemoryHit]) -> list[list[MemoryHit]]:
    """Group EvidenceWindow output: each anchor, in rank order, followed by its neighbours."""
    groups: dict[object, list[MemoryHit]] = {}
    for hit in evidence:
        anchor = hit.get("anchor_id")
        if anchor is None:
            groups.setdefault(hit["id"], []).insert(0, hit)
        else:
            groups.setdefault(anchor, []).append(hit)
    return list(groups.values())


def _key(hit: MemoryHit) -> object:
    identity = hit.get("id")
    return identity if identity is not None else id(hit)

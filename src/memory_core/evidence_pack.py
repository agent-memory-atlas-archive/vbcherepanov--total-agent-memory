from __future__ import annotations

import json
import math
from collections.abc import Sequence

from memory_core.evidence_excerpt import OMISSION, excerpt
from memory_core.retrieval import MemoryHit
from memory_core.telemetry import counters, op_timer

DEFAULT_EVIDENCE_CHARS = 24000
MIN_EVIDENCE_CHARS = 512


def pack_evidence(
    evidence: Sequence[MemoryHit],
    *,
    query: str = "",
    max_chars: int = DEFAULT_EVIDENCE_CHARS,
    max_bytes: int | None = None,
) -> str:
    packed = pack_evidence_records(evidence, query=query, max_chars=max_chars, max_bytes=max_bytes)
    if not packed:
        return "(none yet)"
    return "\n".join(header + "\n" + hit.get("content", "")
                     for header, hit in zip(_headers(packed), packed, strict=True))


def _headers(evidence: Sequence[MemoryHit]) -> list[str]:
    return [
        json.dumps(
            {"ref": index, "id": hit.get("id"),
             "source": hit.get("source_ref", hit.get("source")),
             "date": hit.get("created_at"), "session": hit.get("session_id"),
             **({"anchor_id": hit["anchor_id"]} if hit.get("anchor_id") is not None else {}),
             **({"evidence_ids": hit["evidence_ids"]} if hit.get("evidence_ids") else {}),
             **({"events": hit["events"]} if hit.get("events") else {}),
             **({"passages": hit["passages"]} if hit.get("passages") else {})},
            ensure_ascii=False, separators=(",", ":"), default=str,
        )
        for index, hit in enumerate(evidence, 1)
    ]


def packed_size(hit: MemoryHit, *, use_bytes: bool = False) -> int:
    """Characters (or UTF-8 bytes) one record takes in a packed context: header, content, separators."""
    text = _headers([hit])[0] + str(hit.get("content", "")).strip()
    return (len(text.encode("utf-8")) if use_bytes else len(text)) + 2


def pack_evidence_records(
    evidence: Sequence[MemoryHit], *, query: str = "",
    max_chars: int = DEFAULT_EVIDENCE_CHARS, max_bytes: int | None = None,
) -> list[MemoryHit]:
    with op_timer("evidence_pack_ms"):
        return _pack_records(evidence, query=query, max_chars=max_chars, max_bytes=max_bytes)


def _pack_records(
    evidence: Sequence[MemoryHit], *, query: str, max_chars: int, max_bytes: int | None,
) -> list[MemoryHit]:
    if type(max_chars) is not int or max_chars < MIN_EVIDENCE_CHARS:
        raise ValueError(f"max_chars must be at least {MIN_EVIDENCE_CHARS}")
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes < MIN_EVIDENCE_CHARS):
        raise ValueError(f"max_bytes must be at least {MIN_EVIDENCE_CHARS}")
    if not evidence:
        return []
    evidence = [
        {**hit, "source_ref": hit.get("source_ref") or f"knowledge:{hit['id']}"}
        if hit.get("id") is not None else {**hit}
        for hit in evidence
    ]
    def cost(text: str) -> int:
        return len(text.encode("utf-8")) if max_bytes is not None else len(text)

    limit = min(max_chars, max_bytes) if max_bytes is not None else max_chars
    headers = _headers(evidence)
    contents = [str(hit.get("content", "")).strip() for hit in evidence]
    sizes = [cost(content) for content in contents]
    overhead = sum(cost(header) + 2 for header in headers)
    if overhead + cost(OMISSION) * len(evidence) > limit:
        raise ValueError("evidence metadata exceeds context budget")
    budget = limit - overhead
    allocations = [0] * len(contents)
    weights = [float(hit.get('evidence_weight', 1.0)) for hit in evidence]
    if any(not math.isfinite(weight) or weight <= 0 for weight in weights):
        raise ValueError('Evidence weights must be finite and positive')
    pending = list(range(len(contents)))
    while pending and budget:
        total_weight = sum(weights[index] for index in pending)
        available = budget
        remaining = []
        for index in pending:
            share = max(1, int(available * weights[index] / total_weight))
            take = min(share, sizes[index] - allocations[index], budget)
            allocations[index] += take
            budget -= take
            if allocations[index] < sizes[index]:
                remaining.append(index)
        pending = remaining
    packed = []
    for hit, content, allowance in zip(evidence, contents, allocations, strict=True):
        selected = excerpt(content, query, allowance, cost)
        if selected != content:
            counters.bump("evidence_pack_truncated_sources")
        packed.append({**hit, "content": selected})
    counters.bump("evidence_pack_sources", len(packed))
    return packed

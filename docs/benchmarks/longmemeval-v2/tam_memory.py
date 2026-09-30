"""total-agent-memory (TAM) as a LongMemEval-V2 memory backend (memory_type "tam").

insert(trajectory) turns one web-agent trajectory into text fragments and adds them to a
private TAM store (tam_bench_common.tam_worker, one worker process per memory):

- one fragment per state: URL, the agent's thought and action, a short goal line, then
  the page's accessibility tree. TAM indexes the whole text (FTS5 sees every page token;
  the local embedding model sees the leading part, i.e. URL/thought/action/goal);
- one overview fragment per trajectory: goal, outcome, start URL and the ordered list of
  (URL, action, short thought) for every step.

Screenshots are not indexed and question images are not used for retrieval: this backend
is text-only. query() runs TAM's recall on the question text (FTS5 + vectors + RRF, and
the local cross-encoder when cross_rerank is "on") and, with fill_budget (default), TAM's
budget fill: up to fill_pool ranked hits, each with the context_radius states before and
after it in the same trajectory, each state counted at most per_hit_max_chars, are kept
whole in rank order while they fit into max_context_chars. It returns them as text items,
best first. A state hit longer than its share of the context budget is cut to the window
of accessibility-tree lines that shares the most terms with the question.

No LLM runs inside TAM; the reader and the judge are the harness's.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

_BENCH_ROOT = Path(__file__).resolve().parents[1]
if str(_BENCH_ROOT) not in sys.path:
    sys.path.insert(0, str(_BENCH_ROOT))

from memory_modules.memory import Memory, MemoryContextItem, register_memory
from tam_bench_common.tam_worker import TamStoreProcess, TamWorkerSettings

PROJECT = "lmev2"
MAX_CONTEXT_RADIUS = 3
RECORD_SEPARATOR = "\n\n"
TERM = re.compile(r"[a-z0-9]+")
STOPWORDS = frozenset(
    ["the", "and", "for", "are", "but", "not", "you", "all", "any", "can", "had", "her", "was", "one", "our", "out", "has", "have", "how", "its", "may", "new", "now", "see", "two", "way", "who", "did", "get", "let", "put", "say", "she", "too", "use", "what", "when", "where", "which", "while", "with", "this", "that", "from", "they", "them", "then", "than", "there", "their", "these", "those", "into", "your", "about", "after", "before", "would", "could", "should", "will", "been", "being", "does", "done", "each", "some", "such", "only", "other", "more", "most", "very", "also", "just", "over", "under", "again", "once", "here", "why", "whom", "mark", "final", "answer", "boxed"])


@dataclass(frozen=True)
class TamLmeSettings:
    tam_src: str
    python_executable: str | None = None
    top_k: int = 10
    fill_budget: bool = True
    fill_pool: int = 100
    context_radius: int = 1
    max_context_chars: int = 48000
    per_hit_max_chars: int = 8000
    goal_chars: int = 300
    overview_thought_chars: int = 200
    embed_batch: int = 64
    embed_provider: str = "fastembed"
    cross_rerank: str = "on"
    worker_timeout_s: float = 3600.0
    work_root: str | None = None

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> TamLmeSettings:
        known = {field.name for field in fields(cls)}
        unknown = set(params) - known
        if unknown:
            raise ValueError(f"unknown TAM memory_params: {sorted(unknown)}")
        values = dict(params)
        values.setdefault("tam_src", os.environ.get("TAM_SRC_DIR"))
        if not values["tam_src"]:
            raise ValueError("memory_params.tam_src (or TAM_SRC_DIR) is required")
        settings = cls(**values)
        for name in ("top_k", "fill_pool", "max_context_chars", "per_hit_max_chars", "goal_chars",
                     "overview_thought_chars", "embed_batch"):
            if int(getattr(settings, name)) < 1:
                raise ValueError(f"{name} must be >= 1")
        if not 0 <= int(settings.context_radius) <= MAX_CONTEXT_RADIUS:
            raise ValueError(f"context_radius must be between 0 and {MAX_CONTEXT_RADIUS}")
        return settings

    def worker_settings(self) -> TamWorkerSettings:
        return TamWorkerSettings(tam_src=self.tam_src, project=PROJECT, top_k=self.top_k,
                                 embed_batch=self.embed_batch, embed_provider=self.embed_provider,
                                 cross_rerank=self.cross_rerank, worker_timeout_s=self.worker_timeout_s,
                                 work_root=self.work_root, python_executable=self.python_executable)


# ---------------------------------------------------------------------------
# Trajectory -> fragments (pure functions)
# ---------------------------------------------------------------------------

def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + " ..."


def state_fragment(trajectory: dict[str, Any], state: dict[str, Any], state_count: int,
                   settings: TamLmeSettings) -> dict[str, Any]:
    trajectory_id = _text(trajectory.get("id"))
    header_lines = [
        (f"Trajectory {trajectory_id} ({_text(trajectory.get('environment'))}, outcome: "
         f"{_text(trajectory.get('outcome')) or 'unknown'}) - step {state.get('state_index')} of {state_count}"),
        f"URL: {_text(state.get('url'))}",
    ]
    if _text(state.get("thought")):
        header_lines.append(f"Thought: {_text(state.get('thought'))}")
    header_lines.append(f"Action: {_text(state.get('action')) or '(none: initial state)'}")
    header_lines.append(f"Goal: {_clip(' '.join(_text(trajectory.get('goal')).split()), settings.goal_chars)}")
    header = "\n".join(header_lines)
    page = _text(state.get("accessibility_tree"))
    content = f"{header}\nPage (accessibility tree):\n{page}" if page else header
    return {
        "index_text": content,
        "content": content,
        "session": trajectory_id,
        "meta": {"kind": "state", "trajectory_id": trajectory_id, "state_index": state.get("state_index"),
                 "page_offset": len(header) + len("\nPage (accessibility tree):\n") if page else len(content)},
    }


def overview_fragment(trajectory: dict[str, Any], states: Sequence[dict[str, Any]],
                      settings: TamLmeSettings) -> dict[str, Any]:
    trajectory_id = _text(trajectory.get("id"))
    lines = [
        (f"Trajectory {trajectory_id} overview ({_text(trajectory.get('environment'))}, outcome: "
         f"{_text(trajectory.get('outcome')) or 'unknown'}, {len(states)} states)"),
        f"Goal: {_text(trajectory.get('goal'))}",
        f"Start URL: {_text(trajectory.get('start_url'))}",
        "Steps:",
    ]
    for state in states:
        action = _text(state.get("action"))
        if not action:
            continue
        thought = _clip(" ".join(_text(state.get("thought")).split()), settings.overview_thought_chars)
        line = f"{state.get('state_index')}. [{_text(state.get('url'))}] {action}"
        lines.append(f"{line} - {thought}" if thought else line)
    content = "\n".join(lines)
    return {"index_text": content, "content": content, "session": trajectory_id,
            "meta": {"kind": "overview", "trajectory_id": trajectory_id, "page_offset": len(content)}}


def trajectory_fragments(trajectory: dict[str, Any], settings: TamLmeSettings) -> list[dict[str, Any]]:
    states = [state for state in trajectory.get("states") or [] if isinstance(state, dict)]
    fragments = [overview_fragment(trajectory, states, settings)]
    fragments.extend(state_fragment(trajectory, state, len(states), settings) for state in states)
    return fragments


# ---------------------------------------------------------------------------
# Query-time presentation (pure functions)
# ---------------------------------------------------------------------------

def query_terms(query: str) -> frozenset:
    return frozenset(term for term in TERM.findall(query.lower()) if len(term) >= 3 and term not in STOPWORDS)


def best_window(lines: Sequence[str], terms: frozenset, budget: int) -> tuple[int, int]:
    """[start, end) of the contiguous line window within `budget` chars that covers the most term hits."""
    scores = [len(terms.intersection(TERM.findall(line.lower()))) for line in lines]
    best = (0, 0, -1)
    start = 0
    size = 0
    score = 0
    for end, line in enumerate(lines):
        size += len(line) + 1
        score += scores[end]
        while size > budget and start <= end:
            size -= len(lines[start]) + 1
            score -= scores[start]
            start += 1
        if start <= end and score > best[2]:
            best = (start, end + 1, score)
    if best[2] <= 0:
        end = 0
        size = 0
        while end < len(lines) and size + len(lines[end]) + 1 <= budget:
            size += len(lines[end]) + 1
            end += 1
        return 0, end
    return best[0], best[1]


def render_hit(hit: dict[str, Any], terms: frozenset, budget: int) -> str:
    content = hit["content"]
    if len(content) <= budget:
        return content
    offset = int(hit["meta"].get("page_offset", len(content)))
    head, page = content[:offset], content[offset:]
    room = budget - len(head) - len("[...]\n") * 2
    if room <= 0:
        return _clip(content, budget)
    lines = page.split("\n")
    start, end = best_window(lines, terms, room)
    if end <= start:
        return _clip(content, budget)
    excerpt = "\n".join(lines[start:end])
    if len(excerpt) > room:
        excerpt = excerpt[:room]
    prefix = "[...]\n" if start > 0 else ""
    suffix = "\n[...]" if end < len(lines) else ""
    return f"{head}{prefix}{excerpt}{suffix}"


def item_label(position: int) -> str:
    return f"[Memory {position}]\n"


def hit_groups(hits: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """A ranked hit and the neighbours the worker put after it (records with an `anchor`)."""
    groups: list[list[dict[str, Any]]] = []
    for hit in hits:
        if hit.get("anchor") is None or not groups:
            groups.append([hit])
        else:
            groups[-1].append(hit)
    return groups


def assemble_items(query: str, hits: list[dict[str, Any]], settings: TamLmeSettings) -> list[MemoryContextItem]:
    """One text item per ranked hit, best first; a hit's neighbouring states are shown with
    it in trajectory order. Each record is cut to at most per_hit_max_chars."""
    terms = query_terms(query)
    items: list[MemoryContextItem] = []
    remaining = settings.max_context_chars
    for position, group in enumerate(hit_groups(hits), start=1):
        label = item_label(position)
        remaining -= len(label)
        parts: list[str] = []
        for record in sorted(group, key=lambda record: record.get("position", 0)):
            budget = min(settings.per_hit_max_chars, remaining)
            if budget <= 0:
                break
            text = render_hit(record, terms, budget)
            parts.append(text)
            remaining -= len(text) + len(RECORD_SEPARATOR)
        if not parts:
            break
        items.append({"type": "text", "value": label + RECORD_SEPARATOR.join(parts)})
    return items


# ---------------------------------------------------------------------------
# Harness backend
# ---------------------------------------------------------------------------

@register_memory
class TamMemory(Memory):
    """LongMemEval-V2 backend backed by a private total-agent-memory store."""

    memory_type = "tam"

    def __init__(self, memory_params: dict[str, object]) -> None:
        super().__init__(memory_params)
        self.settings = TamLmeSettings.from_params(dict(memory_params))
        self._store: TamStoreProcess | None = None
        self._store_lock = threading.Lock()
        self._fragment_count = 0
        self.insert_stats: dict[str, float] = {"embed_seconds": 0.0, "save_seconds": 0.0, "trajectories": 0}
        self._build_started: float | None = None
        self._build_wall_seconds: float | None = None
        self._last_hits = threading.local()

    def _ensure_store(self) -> TamStoreProcess:
        with self._store_lock:
            if self._store is None:
                self._store = TamStoreProcess(self.settings.worker_settings(), prefix="lmev2-tam-")
            return self._store

    def insert(self, trajectory: dict[str, object]) -> None:
        if self._build_started is None:
            self._build_started = time.perf_counter()
        self._build_wall_seconds = None
        fragments = trajectory_fragments(dict(trajectory), self.settings)
        stats = self._ensure_store().add(fragments)
        self._fragment_count += int(stats.get("fragments", 0))
        self.insert_stats["embed_seconds"] += float(stats.get("embed_seconds", 0.0))
        self.insert_stats["save_seconds"] += float(stats.get("save_seconds", 0.0))
        self.insert_stats["trajectories"] += 1

    def query(self, query: str, query_image: str | None = None) -> list[MemoryContextItem]:
        if self._build_wall_seconds is None and self._build_started is not None:
            self._build_wall_seconds = time.perf_counter() - self._build_started
        if self._fragment_count == 0 or not query.strip():
            self._last_hits.value = []
            return []
        if self.settings.fill_budget:
            hits = self._ensure_store().search(query, limit=self.settings.fill_pool,
                                               radius=self.settings.context_radius,
                                               fill_chars=self.settings.max_context_chars,
                                               fill_overhead=len(item_label(self.settings.fill_pool)),
                                               fill_record_cap=self.settings.per_hit_max_chars)
        else:
            hits = self._ensure_store().search(query)
        self._last_hits.value = [{"rank": hit["rank"], **hit["meta"]} for hit in hits]
        return assemble_items(query, hits, self.settings)

    def post_query_hook(self, *, query: str, query_image: str | None,
                        memory_context: list[MemoryContextItem]) -> dict[str, object]:
        return {
            "tam_hits": getattr(self._last_hits, "value", []),
            "tam_fragments_indexed": self._fragment_count,
            "tam_cross_rerank": self.settings.cross_rerank,
            "tam_index_build": {**self.insert_stats, "wall_seconds_until_first_query": self._build_wall_seconds},
            "query_image_used": False,
        }

    def close(self) -> None:
        with self._store_lock:
            if self._store is not None:
                self._store.close()
                self._store = None

"""total-agent-memory (TAM) as an AMA-Bench memory method.

Install into an AMA-Bench checkout with setup_harness.sh (symlinks this file to
src/method/tam_memory.py and registers the method as `tam`).

Construction writes the episode's trajectory into a fresh TAM store (one store per
episode, in its own temporary directory and its own worker process, see
tam_bench_common.tam_worker). Retrieval runs TAM's recall (FTS5 + vectors + RRF + local
cross-encoder) on the question and, with fill_budget (default), TAM's budget fill: up to
fill_pool ranked hits, each with the context_radius steps before and after it, are kept
whole in rank order while they fit into max_context_chars.
The kept hits are returned in trajectory order, under the task description.

No LLM runs inside TAM. The only LLMs in the loop are the harness's answerer and judge,
configured through --llm-config / --judge-config.
"""

from __future__ import annotations

import os
import re
import sys
import threading
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

# This file is symlinked into the harness; resolve() finds docs/benchmarks in the TAM repo.
_BENCH_ROOT = Path(__file__).resolve().parents[1]
if str(_BENCH_ROOT) not in sys.path:
    sys.path.insert(0, str(_BENCH_ROOT))

from src.method.base_method import BaseMethod
from tam_bench_common.tam_worker import (
    TamStoreProcess,
    TamWorkerSettings,
    check_work_root,
)

PROJECT = "ama"
STEP_HEADER = re.compile(r"^Step (-?\d+):\s*$")
WORK_ROOT_ENV = "TAM_BENCH_WORK_ROOT"
HIT_SEPARATOR_CHARS = 2
EPISODE_SESSION = "episode"
MAX_CONTEXT_RADIUS = 3
# A question that names steps ("step 38", "steps 14-19", "turn index 7", "indices 3 and 5").
STEP_REFERENCE = re.compile(
    r"\b(?:steps?|turns?(?:\s+ind(?:ex|ices))?|ind(?:ex|ices))\s*[#:]?\s*"
    r"(\d+(?:\s*(?:,|and|or|to|through|-|\u2013|\u2014)\s*(?:steps?\s*|turns?\s*)?\d+)*)",
    re.IGNORECASE)
STEP_RANGE_WORDS = re.compile(r"^(?:to|through|-|\u2013|\u2014)$")
MAX_ANCHOR_RANGE = 25
# A quoted span in a question ("...", '...', curly quotes) long enough to identify a step.
QUOTED_SPAN = re.compile(r'"([^"]{30,})"|\u201c([^\u201d]{30,})\u201d|(?<![A-Za-z])\'([^\']{30,})\'(?![A-Za-z])')
QUOTE_PROBE_CHARS = 150
DIGEST_ACTION_SHARE = 0.6
DIGEST_HEAD_CHARS = 400
# An entity named by an action: a word and an instance number ("cabinet 1", "soapbar 2").
ACTION_ENTITY = re.compile(r"\b([a-z][a-z_-]*) (\d+)\b", re.IGNORECASE)
NON_ENTITY_WORDS = frozenset({"step", "steps", "turn", "turns", "index", "indices", "to", "from", "in", "on",
                              "at", "of", "and", "or", "by", "with", "x", "y"})
INVENTORY_WORDS = re.compile(r"\b(?:inventory|carrying|carried|holding|held|picked up)\b", re.IGNORECASE)
TIMELINE_OBSERVATION_CHARS = 160
MAX_TIMELINE_ENTITIES = 4
# A question that asks for a count or a frequency.
COUNT_WORDS = re.compile(r"\b(?:how many|how often|how frequently|frequency|frequencies|number of|count|counts|times)\b",
                         re.IGNORECASE)
MAX_DISTINCT_ACTIONS = 25
MAX_COUNTED_ACTION_CHARS = 40


@dataclass(frozen=True)
class TamSettings:
    tam_src: str
    top_k: int = 10
    fill_budget: bool = True
    fill_pool: int = 100
    context_radius: int = 1
    fragment_max_chars: int = 2000
    max_context_chars: int = 40000
    max_task_chars: int = 4000
    embed_batch: int = 64
    embed_provider: str = "fastembed"
    cross_rerank: str = "on"
    max_live_workers: int = 4
    worker_timeout_s: float = 1800.0
    work_root: str | None = None
    python_executable: str | None = None
    # Opt-in structure (all off by default, so the pilot configuration is unchanged):
    step_anchors: bool = False     # steps the question names are fetched from the store first
    anchor_radius: int = 1         # ... together with this many steps before and after each
    anchor_step_chars: int = 8000  # at most this many chars of one anchored step (its first parts)
    digest_chars: int = 0          # > 0: a per-step outline (action + start of observation) of this size
    quote_anchors: bool = False    # steps that contain a span the question quotes are fetched like named steps
    quote_max_matches: int = 4     # at most this many steps per quoted span
    timeline_chars: int = 0        # > 0: per-entity step timeline for entities the question names
    inventory_verbs: tuple[str, ...] = ()  # first words of actions that change what the agent carries
    action_stats: bool = False     # a counting question gets action counts up to the last step it names

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> TamSettings:
        config = dict(config)
        tam_src = config.pop("tam_src", None) or os.environ.get("TAM_SRC_DIR")
        if not tam_src:
            raise ValueError("TAM method needs `tam_src` in the method config or TAM_SRC_DIR in the environment")
        if not (Path(tam_src) / "server.py").is_file():
            raise ValueError(f"tam_src={tam_src!r} does not contain TAM's server.py")
        known = {name for name in cls.__dataclass_fields__ if name != "tam_src"}
        unknown = set(config) - known
        if unknown:
            raise ValueError(f"unknown TAM method settings: {sorted(unknown)}")
        if config.get("work_root") is None and os.environ.get(WORK_ROOT_ENV):
            config["work_root"] = os.environ[WORK_ROOT_ENV]
        settings = cls(tam_src=str(Path(tam_src).resolve()), **config)
        for name in ("top_k", "fill_pool", "fragment_max_chars", "max_context_chars", "max_task_chars",
                     "embed_batch", "max_live_workers", "anchor_step_chars", "quote_max_matches"):
            if int(getattr(settings, name)) < 1:
                raise ValueError(f"{name} must be >= 1")
        if not 0 <= int(settings.context_radius) <= MAX_CONTEXT_RADIUS:
            raise ValueError(f"context_radius must be between 0 and {MAX_CONTEXT_RADIUS}")
        if not 0 <= int(settings.anchor_radius) <= MAX_CONTEXT_RADIUS:
            raise ValueError(f"anchor_radius must be between 0 and {MAX_CONTEXT_RADIUS}")
        if int(settings.digest_chars) < 0:
            raise ValueError("digest_chars must be >= 0")
        if int(settings.timeline_chars) < 0:
            raise ValueError("timeline_chars must be >= 0")
        if not isinstance(settings.inventory_verbs, (list, tuple)) or not all(
                isinstance(verb, str) and verb.strip() for verb in settings.inventory_verbs):
            raise ValueError("inventory_verbs must be a list of non-empty words")
        settings = replace(settings, inventory_verbs=tuple(verb.strip().lower() for verb in settings.inventory_verbs))
        if settings.work_root is not None:
            check_work_root(settings.work_root)
        return settings

    def worker_settings(self) -> TamWorkerSettings:
        return TamWorkerSettings(tam_src=self.tam_src, project=PROJECT, top_k=self.top_k,
                                 embed_batch=self.embed_batch, embed_provider=self.embed_provider,
                                 cross_rerank=self.cross_rerank, worker_timeout_s=self.worker_timeout_s,
                                 work_root=self.work_root, python_executable=self.python_executable)


# ---------------------------------------------------------------------------
# Trajectory -> fragments (pure functions)
# ---------------------------------------------------------------------------

def split_steps(traj_text: str) -> list[tuple[int, str]]:
    """Split the harness's `Step N:` / `Action:` / `Observation:` text into (step, text)."""
    steps: list[tuple[int, str]] = []
    current_step: int | None = None
    buffer: list[str] = []
    for line in traj_text.split("\n"):
        header = STEP_HEADER.match(line.strip())
        if header:
            if current_step is not None or any(part.strip() for part in buffer):
                steps.append((current_step if current_step is not None else -1, "\n".join(buffer).strip()))
            current_step = int(header.group(1))
            buffer = []
        else:
            buffer.append(line)
    if current_step is not None or any(part.strip() for part in buffer):
        steps.append((current_step if current_step is not None else -1, "\n".join(buffer).strip()))
    return [(step, text) for step, text in steps if text]


def split_text(text: str, max_chars: int) -> list[str]:
    """Split on line boundaries; a single line longer than max_chars is cut hard."""
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


def step_heads(text: str) -> tuple[str, str]:
    """The step's action and the first paragraph of its observation, whitespace collapsed
    and cut to DIGEST_HEAD_CHARS each (the harness writes `Action: ...` / `Observation: ...`)."""
    action, _, observation = text.partition("\nObservation:")
    action = action.removeprefix("Action:")
    if not observation and not text.startswith("Action:"):
        action, observation = "", text
    paragraph = next((part for part in observation.split("\n\n") if part.strip()), "")
    return (" ".join(action.split())[:DIGEST_HEAD_CHARS], " ".join(paragraph.split())[:DIGEST_HEAD_CHARS])


def build_fragments(traj_text: str, max_chars: int) -> list[dict[str, Any]]:
    fragments: list[dict[str, Any]] = []
    for step, text in split_steps(traj_text):
        pieces = split_text(text, max_chars)
        action, observation = step_heads(text)
        for index, piece in enumerate(pieces, start=1):
            label = f"Step {step}" if len(pieces) == 1 else f"Step {step} (part {index}/{len(pieces)})"
            content = f"{label}\n{piece}"
            meta: dict[str, Any] = {"step": step, "part": index, "parts": len(pieces)}
            if index == 1:
                meta.update(action=action, observation=observation)
            fragments.append({"index_text": content, "content": content, "session": EPISODE_SESSION,
                              "meta": meta})
    return fragments


def step_references(question: str) -> list[int]:
    """Step numbers a question names, in order of first mention. `steps 14-19` names every
    step of the range when it is at most MAX_ANCHOR_RANGE long, otherwise its two ends."""
    steps: list[int] = []
    for match in STEP_REFERENCE.finditer(question):
        tokens = re.findall(r"\d+|,|and|or|to|through|-|\u2013|\u2014", match.group(1), re.IGNORECASE)
        between = question[max(0, match.start() - 9):match.start()].strip().lower().endswith("between")
        previous: int | None = None
        pending_range = False
        for token in tokens:
            if token.isdigit():
                number = int(token)
                if pending_range and previous is not None and 0 < number - previous <= MAX_ANCHOR_RANGE:
                    steps.extend(range(previous + 1, number + 1))
                else:
                    steps.append(number)
                previous, pending_range = number, False
            else:
                word = token.lower()
                pending_range = bool(STEP_RANGE_WORDS.match(word)) or (between and word == "and")
                between = between and word != "and"
    return list(dict.fromkeys(steps))


def quoted_spans(question: str) -> list[str]:
    """Quoted spans of at least 30 chars, whitespace collapsed, cut to QUOTE_PROBE_CHARS and
    before any ellipsis (questions often quote the start of a long observation)."""
    spans: list[str] = []
    for match in QUOTED_SPAN.finditer(question):
        span = " ".join(next(group for group in match.groups() if group).split())
        span = re.split(r"\.\.\.|\u2026", span, maxsplit=1)[0].strip()[:QUOTE_PROBE_CHARS].strip()
        if len(span) >= 30:
            spans.append(span)
    return list(dict.fromkeys(spans))


def anchor_positions(question: str, step_index: dict[int, list[int]], settings: TamSettings,
                     part_chars: int, extra_steps: list[int] | None = None) -> list[int]:
    """Store positions of the steps a question names (then extra_steps), each with
    anchor_radius steps around it, anchors first; an anchored step contributes its first
    parts up to anchor_step_chars."""
    named = [step for step in step_references(question) if step in step_index]
    named = list(dict.fromkeys(named + [step for step in (extra_steps or []) if step in step_index]))
    ordered: list[int] = list(named)
    for step in named:
        for offset in range(1, settings.anchor_radius + 1):
            ordered.extend(near for near in (step - offset, step + offset) if near in step_index)
    max_parts = max(1, settings.anchor_step_chars // max(1, part_chars))
    positions: list[int] = []
    for step in dict.fromkeys(ordered):
        positions.extend(step_index[step][:max_parts])
    return positions


def build_digest(outline: list[dict[str, Any]], max_chars: int) -> str:
    """One line per step (`Step N: action => observation start`), every line cut to the same
    length so the whole trajectory fits into max_chars; steps that still do not fit are
    dropped from the end and counted."""
    entries = [entry["meta"] for entry in outline if "action" in entry.get("meta", {})]
    if not entries or max_chars < 1:
        return ""
    per_line = max(1, max_chars // len(entries) - 1)
    lines: list[str] = []
    used = 0
    for meta in entries:
        prefix = f"Step {meta['step']}: "
        room = max(0, per_line - len(prefix))
        action = meta["action"][:max(1, int(room * DIGEST_ACTION_SHARE))] if meta["action"] else ""
        line = f"{prefix}{action} => {meta['observation']}" if action else f"{prefix}{meta['observation']}"
        line = line[:max(per_line, len(prefix) + 1)]
        if used + len(line) + 1 > max_chars:
            lines.append(f"... {len(entries) - len(lines)} later steps not shown")
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


def entity_index(outline: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Entity (lower-cased `word N`) -> the step metas, in trajectory order, whose action names it."""
    index: dict[str, list[dict[str, Any]]] = {}
    for entry in outline:
        meta = entry.get("meta", {})
        if "action" not in meta:
            continue
        for word, number in dict.fromkeys(ACTION_ENTITY.findall(meta["action"])):
            if word.lower() not in NON_ENTITY_WORDS:
                index.setdefault(f"{word.lower()} {number}", []).append(meta)
    return index


def question_entities(question: str, index: dict[str, list[dict[str, Any]]]) -> list[str]:
    """Indexed entities the question names outside quoted spans (a quoted observation lists
    every reachable object), in order of first mention, at most MAX_TIMELINE_ENTITIES."""
    unquoted = QUOTED_SPAN.sub(" ", question)
    named = [f"{word.lower()} {number}" for word, number in ACTION_ENTITY.findall(unquoted)]
    return [entity for entity in dict.fromkeys(named) if entity in index][:MAX_TIMELINE_ENTITIES]


def inventory_steps(outline: list[dict[str, Any]], verbs: tuple[str, ...]) -> list[dict[str, Any]]:
    """Step metas whose action starts with one of the verbs, in trajectory order."""
    return [entry["meta"] for entry in outline
            if "action" in entry.get("meta", {}) and entry["meta"]["action"].split(" ", 1)[0].lower() in verbs]


def acts_on(action: str, entity: str) -> bool:
    """The entity is the action's direct object: it follows the action's first word
    (`open cabinet 1`, `take soapbar 2 from ...`, not `go to cabinet 1` or `move x to cabinet 1`)."""
    _, _, rest = action.partition(" ")
    return rest.lower() == entity or rest.lower().startswith(entity + " ")


def build_timeline(groups: list[tuple[str, list[dict[str, Any]]]], max_chars: int) -> str:
    """One block per group (`name:` then `  Step N: action => start of observation`), the groups
    sharing max_chars evenly; lines that do not fit into a group's share are dropped and counted."""
    groups = [(name, metas) for name, metas in groups if metas]
    if not groups or max_chars < 1:
        return ""
    share = max_chars // len(groups)
    blocks: list[str] = []
    for name, metas in groups:
        lines = [f"{name}:"]
        used = len(lines[0]) + 1
        for shown, meta in enumerate(metas):
            observation = meta.get("observation", "")[:TIMELINE_OBSERVATION_CHARS]
            marker = " [acts on it]" if acts_on(meta["action"], name) else ""
            line = (f"  Step {meta['step']}: {meta['action']}{marker}"
                    + (f" => {observation}" if observation else ""))
            if used + len(line) + 1 > share:
                lines.append(f"  ... {len(metas) - shown} more steps not shown")
                break
            lines.append(line)
            used += len(line) + 1
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


def action_counts(outline: list[dict[str, Any]], last_step: int | None) -> str:
    """Counts of the actions of steps 0..last_step (all steps when None): by first word, and by
    whole action when there are few short distinct actions."""
    actions = [entry["meta"]["action"] for entry in outline
               if entry.get("meta", {}).get("action") and (last_step is None or entry["meta"]["step"] <= last_step)]
    if not actions:
        return ""
    verbs = Counter(action.split(" ", 1)[0].lower() for action in actions)
    lines = ["by first word: " + "; ".join(f"{verb}: {count}" for verb, count in verbs.most_common())]
    whole = Counter(actions)
    if len(whole) <= MAX_DISTINCT_ACTIONS and all(len(action) <= MAX_COUNTED_ACTION_CHARS for action in whole):
        lines.append("by action: " + "; ".join(f"{action}: {count}" for action, count in whole.most_common()))
    scope = "all steps" if last_step is None else f"steps up to and including step {last_step}"
    return f"{len(actions)} actions over {scope}\n" + "\n".join(lines)


def assemble_context(task: str, hits: list[dict[str, Any]], settings: TamSettings, digest: str = "",
                     timeline: str = "", stats: str = "") -> str:
    """Keep hits in rank order until the budget is spent, then present them in trajectory order."""
    kept: list[dict[str, Any]] = []
    used = 0
    for hit in hits:
        size = len(hit["content"]) + HIT_SEPARATOR_CHARS
        if kept and used + size > settings.max_context_chars:
            break
        kept.append(hit)
        used += size
    kept.sort(key=lambda hit: hit["position"])
    task_text = task.strip()
    if len(task_text) > settings.max_task_chars:
        task_text = task_text[:settings.max_task_chars] + "\n...[task truncated]"
    sections = []
    if task_text:
        sections.append(f"## Task\n{task_text}")
    if stats:
        sections.append("## Action counts (computed from the stored actions)\n" + stats)
    if timeline:
        sections.append("## Timeline of the entities the question names (every step whose action names "
                        "them: action => start of its observation; [acts on it] marks actions whose direct "
                        "object is the entity)\n" + timeline)
    if digest:
        sections.append("## Trajectory outline (every step: action => start of its observation; "
                        "full text of the relevant steps follows)\n" + digest)
    body = "\n\n".join(hit["content"] for hit in kept) if kept else "(no matching trajectory steps)"
    sections.append(f"## Retrieved trajectory steps (in trajectory order)\n{body}")
    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Harness method
# ---------------------------------------------------------------------------

class TamEpisodeMemory:
    """One episode's TAM store plus the task text shown above the retrieved steps."""

    def __init__(self, store: TamStoreProcess, task: str):
        self.store = store
        self.task = task
        self.stats: dict[str, Any] = {}
        self.step_index: dict[int, list[int]] = {}
        self.position_step: dict[int, int] = {}
        self.digest = ""
        self.entities: dict[str, list[dict[str, Any]]] = {}
        self.inventory: list[dict[str, Any]] = []
        self.outline: list[dict[str, Any]] = []

    def close(self) -> None:
        self.store.close()


class TAMMethod(BaseMethod):
    """AMA-Bench two-stage method backed by total-agent-memory."""

    def __init__(self, config_path: str | None = None, client: Any = None, embedding_engine: Any = None):
        config = self._load_config(config_path) if config_path else {}
        self.settings = TamSettings.from_config(config)
        self._slots = threading.Semaphore(self.settings.max_live_workers)

    def memory_construction(self, traj_text: str, task: str = "") -> TamEpisodeMemory:
        fragments = build_fragments(traj_text, self.settings.fragment_max_chars)
        memory = TamEpisodeMemory(TamStoreProcess(self.settings.worker_settings(), self._slots, prefix="ama-tam-"),
                                  task)
        try:
            memory.stats = memory.store.add(fragments)
            if fragments and (self.settings.step_anchors or self.settings.quote_anchors or self.settings.digest_chars
                              or self.settings.timeline_chars or self.settings.action_stats):
                outline = memory.store.outline()
                for entry in outline:
                    memory.step_index.setdefault(entry["meta"]["step"], []).append(entry["position"])
                    memory.position_step[entry["position"]] = entry["meta"]["step"]
                if self.settings.digest_chars:
                    memory.digest = build_digest(outline, self.settings.digest_chars)
                if self.settings.action_stats:
                    memory.outline = outline
                if self.settings.timeline_chars:
                    memory.entities = entity_index(outline)
                    memory.inventory = inventory_steps(outline, self.settings.inventory_verbs)
        except BaseException:
            memory.close()
            raise
        return memory

    def memory_retrieve(self, memory: TamEpisodeMemory, question: str) -> str:
        if not isinstance(memory, TamEpisodeMemory):
            raise TypeError("memory must be a TamEpisodeMemory built by TAMMethod.memory_construction")
        if not (memory.stats.get("fragments") and question.strip()):
            return assemble_context(memory.task, [], self.settings, memory.digest)
        timeline = self._timeline(memory, question)
        stats = self._stats(memory, question)
        anchored: list[dict[str, Any]] = []
        quoted: list[int] = []
        if self.settings.quote_anchors:
            spans = quoted_spans(question)
            for matches in (memory.store.grep(spans, self.settings.quote_max_matches) if spans else []):
                quoted.extend(memory.position_step[position] for position in matches)
        if self.settings.step_anchors or quoted:
            question_for_steps = question if self.settings.step_anchors else ""
            positions = anchor_positions(question_for_steps, memory.step_index, self.settings,
                                         self.settings.fragment_max_chars, extra_steps=quoted)
            anchored = memory.store.fetch(positions)
        budget = (self.settings.max_context_chars - len(timeline) - len(stats)
                  - sum(len(hit["content"]) + HIT_SEPARATOR_CHARS for hit in anchored))
        if self.settings.fill_budget:
            hits = memory.store.search(question, limit=self.settings.fill_pool, radius=self.settings.context_radius,
                                       fill_chars=max(1, budget), fill_overhead=HIT_SEPARATOR_CHARS)
        else:
            hits = memory.store.search(question)
        seen = {hit["position"] for hit in anchored}
        ranked = anchored + [hit for hit in hits if hit["position"] not in seen]
        return assemble_context(memory.task, ranked, self.settings, memory.digest, timeline, stats)

    def _stats(self, memory: TamEpisodeMemory, question: str) -> str:
        if not (self.settings.action_stats and memory.outline and COUNT_WORDS.search(question)):
            return ""
        named = step_references(question)
        return action_counts(memory.outline, max(named) if named else None)

    def _timeline(self, memory: TamEpisodeMemory, question: str) -> str:
        if not self.settings.timeline_chars:
            return ""
        groups = [(entity, memory.entities[entity]) for entity in question_entities(question, memory.entities)]
        if memory.inventory and INVENTORY_WORDS.search(question):
            groups.append(("inventory changes (actions " + "/".join(self.settings.inventory_verbs) + ")",
                           memory.inventory))
        return build_timeline(groups, self.settings.timeline_chars)

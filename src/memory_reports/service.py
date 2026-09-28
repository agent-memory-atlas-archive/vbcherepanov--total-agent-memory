"""Builds a Report from one or more memory stores. Deterministic: same data, clock and request give the same report."""
import json
import logging
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from memory_core.telemetry import counters, op_timer
from memory_reports import periods
from memory_reports.contracts import (
    Contributor,
    Counted,
    DayActivity,
    ErrorPattern,
    Listing,
    Metric,
    OpenItem,
    Report,
    ReportItem,
    ReportRequest,
    SourceRef,
    Summary,
    TimelineEntry,
)
from memory_reports.repository import (
    ErrorRow,
    HistoryRow,
    KnowledgeRow,
    ObservationRow,
    ReportRepository,
    RuleRow,
    SummaryRow,
)

LOGGER = logging.getLogger(__name__)
TITLE_CHARS = 160
TEXT_CHARS = 400
TIMELINE_ENTRIES = 5
SOURCE_REFS = 25
RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
OPEN_ORDER = {"next_step": 0, "open_question": 1, "pitfall": 2}
TITLE_PREFIXES = ("DECISION:", "SOLUTION:", "LESSON:", "FACT:", "CONVENTION:")
RECORD_NODE_TYPES = frozenset(("fact", "solution", "decision", "lesson", "convention", "episode", "rule", "procedure",
                               "skill", "blindspot", "competency", "preference", "prohibition", "event"))
SYSTEM_TAG_PREFIXES = ("file:", "pattern:", "scope:", "team:", "user:", "report:")
SYSTEM_TAGS = frozenset(("structured", "learn_error", "auto", "auto-session"))
HISTORY_FIELDS = {"insert": "saves", "update": "updates", "delete": "deletes", "confirm": "confirms"}


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Source:
    """A store to report on. `label` names a team workspace (e.g. team:sales); None for the local store."""
    label: str | None
    repository: ReportRepository


@dataclass
class Collected:
    knowledge: list[tuple[str | None, KnowledgeRow]] = field(default_factory=list)
    replaced: set[tuple[str | None, int]] = field(default_factory=set)
    superseded: list[tuple[str | None, int, int]] = field(default_factory=list)
    confirmed: list[tuple[str | None, int]] = field(default_factory=list)
    errors: list[tuple[str | None, ErrorRow]] = field(default_factory=list)
    rules: list[tuple[str | None, RuleRow]] = field(default_factory=list)
    summaries: list[tuple[str | None, SummaryRow]] = field(default_factory=list)
    observations: list[tuple[str | None, ObservationRow]] = field(default_factory=list)
    sessions: set[tuple[str | None, str]] = field(default_factory=set)
    history: list[tuple[str | None, HistoryRow]] = field(default_factory=list)
    authors: dict[tuple[str | None, int], str] = field(default_factory=dict)
    pattern_history: list[tuple[str | None, str, datetime]] = field(default_factory=list)


def clip(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit - 1].rstrip() + "…"


def title_of(content: str) -> str:
    line = next((part.strip() for part in content.splitlines() if part.strip()), "")
    for prefix in TITLE_PREFIXES:
        if line.upper().startswith(prefix):
            line = line[len(prefix):].strip()
            break
    return clip(line, TITLE_CHARS) or "(empty)"


def decision_why(row: KnowledgeRow) -> str:
    context = row.context.strip()
    if context.startswith("{"):
        try:
            payload = json.loads(context)
        except ValueError:
            payload = None
        if isinstance(payload, dict) and payload.get("rationale"):
            selected = f"Selected {payload['selected']}. " if payload.get("selected") else ""
            return clip(selected + str(payload["rationale"]), TEXT_CHARS)
    return clip(context, TEXT_CHARS)


def error_parts(row: ErrorRow) -> tuple[str, str]:
    """(root cause, pattern key); learn_error stores 'root_cause: X | pattern: Y' in context and pattern:Y in tags."""
    root = row.context
    pattern = next((tag.split(":", 1)[1] for tag in row.tags if tag.startswith("pattern:")), "")
    if row.context.startswith("root_cause:"):
        head = row.context[len("root_cause:"):]
        root = head.split("| pattern:", 1)[0]
    return clip(root, TEXT_CHARS), pattern or "category:" + row.category


def pattern_key(category: str, tags: Sequence[str]) -> str:
    return next((tag.split(":", 1)[1] for tag in tags if tag.startswith("pattern:")), "") or "category:" + category


def change(current: int, previous: int | None) -> tuple[int | None, float | None]:
    if previous is None:
        return None, None
    delta = current - previous
    return delta, (round(delta * 100.0 / previous, 1) if previous else None)


class ReportService:
    def __init__(self, clock: Callable[[], datetime] = utc_now, environ: Mapping[str, str] | None = None):
        self.clock, self.environ = clock, environ

    def build(self, request: ReportRequest, sources: Sequence[Source], scope: str | None = None) -> Report:
        started = time.monotonic()
        try:
            with op_timer("report_build_ms"):
                report = self._build(request, sources, scope)
        except Exception:
            counters.bump("report_failed")
            raise
        counters.bump("report_built")
        LOGGER.info(json.dumps({"event": "report_built", "period": request.period, "project": request.project,
                                "scope": scope, "records": report.summary.records, "empty": report.empty,
                                "skipped_timestamps": sum(s.repository.skipped for s in sources),
                                "duration_ms": round((time.monotonic() - started) * 1000, 1)}))
        return report

    def _build(self, request: ReportRequest, sources: Sequence[Source], scope: str | None) -> Report:
        now = self.clock().astimezone(UTC)
        zone = periods.resolve_zone(request.tz, self.environ)
        first = min(filter(None, (s.repository.first_activity(request.project) for s in sources)), default=None)
        span = periods.resolve(request, zone, now, first)
        earlier = periods.previous(span)
        current = self._collect(request.project, span, sources)
        before = self._collect(request.project, earlier, sources) if earlier is not None else None
        view = _View(span, current, request.limit)
        summary = view.summary()
        opens = view.open_items()
        changes = self._changes(view, summary, opens, _View(earlier, before, request.limit) if before else None)
        return Report(
            project=request.project, scope=scope, window=periods.to_window(span, now),
            previous=periods.to_window(earlier, now) if earlier is not None else None,
            generated_at=now.astimezone(zone.info).isoformat(timespec="seconds"),
            empty=view.empty(), summary=summary, changes=changes,
            decisions=view.listing(view.items(("decision",), rank_first=True)),
            solutions=view.listing(view.items(("solution",))),
            errors=view.listing(view.errors()), lessons=view.listing(view.lessons()),
            error_patterns=view.patterns(), open_items=view.listing(opens),
            files=view.listing(view.files()), entities=view.listing(view.entities(sources, request.project)),
            tags=view.listing(view.tags()), timeline=view.timeline(), contributors=view.contributors())

    @staticmethod
    def _collect(project: str | None, span: periods.Span, sources: Sequence[Source]) -> Collected:
        data = Collected()
        for source in sources:
            repo, label = source.repository, source.label
            rows = repo.knowledge(project, span.start, span.end)
            data.knowledge.extend((label, row) for row in rows)
            for new, olds in repo.predecessors([row.id for row in rows]).items():
                if any(status == "superseded" for _, status in olds):
                    data.replaced.add((label, new))
            data.superseded.extend((label, old, new) for old, new in repo.superseded(project, span.start, span.end))
            data.confirmed.extend((label, rid) for rid in repo.confirmed(project, span.start, span.end))
            data.errors.extend((label, row) for row in repo.errors(project, span.start, span.end))
            data.rules.extend((label, row) for row in repo.rules(project, span.start, span.end))
            data.summaries.extend((label, row) for row in repo.summaries(project, span.start, span.end))
            data.observations.extend((label, row) for row in repo.observations(project, span.start, span.end))
            data.sessions |= {(label, sid) for sid in repo.sessions(project, span.start, span.end)}
            data.history.extend((label, row) for row in repo.history(span.start, span.end))
            data.authors.update({(label, rid): name for rid, name in repo.authors([row.id for row in rows]).items()})
            data.pattern_history.extend((label, pattern_key(category, tags), at)
                                        for category, tags, at in repo.error_history(project, span.end))
        return data

    @staticmethod
    def _changes(view: "_View", summary: Summary, opens: list[OpenItem], before: "_View | None") -> list[Metric]:
        old = before.summary() if before else None
        old_open = len(before.open_items()) if before else None
        rows = [("records", "Records written", summary.records, old.records if old else None),
                ("new", "New records", summary.new, old.new if old else None),
                ("updated", "Updated (replaced a record)", summary.updated, old.updated if old else None),
                ("superseded", "Superseded", summary.superseded, old.superseded if old else None),
                ("confirmed", "Re-confirmed", summary.confirmed, old.confirmed if old else None),
                ("decisions", "Decisions", view.count_type("decision"), before.count_type("decision") if before else None),
                ("solutions", "Solutions", view.count_type("solution"), before.count_type("solution") if before else None),
                ("lessons", "Lessons and rules", view.lesson_count(), before.lesson_count() if before else None),
                ("errors", "Errors", summary.errors, old.errors if old else None),
                ("sessions", "Sessions", summary.sessions, old.sessions if old else None),
                ("active_days", "Active days", summary.active_days, old.active_days if old else None),
                ("open_items", "Open items", len(opens), old_open)]
        return [Metric(key=key, label=label, current=value, previous=prior, delta=change(value, prior)[0],
                       change_pct=change(value, prior)[1]) for key, label, value, prior in rows]


class _View:
    """Sections over one collected window; all orderings are total so output is deterministic."""

    def __init__(self, span: periods.Span, data: Collected, limit: int):
        self.span, self.data, self.limit = span, data, limit

    def at(self, moment: datetime) -> str:
        return moment.astimezone(self.span.zone.info).isoformat(timespec="seconds")

    def listing(self, items: list) -> Listing:
        return Listing(total=len(items), items=items[:self.limit])

    @staticmethod
    def ref(kind: str, label: str | None, identifier: int | str) -> SourceRef:
        return SourceRef(kind=kind, id=identifier, workspace=label)

    def count_type(self, kind: str) -> int:
        return sum(1 for _, row in self.data.knowledge if row.type == kind)

    def lesson_count(self) -> int:
        return self.count_type("lesson") + len(self.data.rules)

    def summary(self) -> Summary:
        data = self.data
        by_type = Counter(row.type for _, row in data.knowledge)
        updated = sum(1 for label, row in data.knowledge if (label, row.id) in data.replaced)
        sessions = {(label, row.session_id) for label, row in data.knowledge if row.session_id}
        sessions |= {(label, row.session_id) for label, row in data.errors if row.session_id}
        sessions |= {(label, row.session_id) for label, row in data.summaries if row.session_id}
        sessions |= {(label, row.session_id) for label, row in data.observations if row.session_id}
        sessions |= data.sessions
        return Summary(records=len(data.knowledge), records_by_type=dict(sorted(by_type.items())),
                       new=len(data.knowledge) - updated, updated=updated, confirmed=len(data.confirmed),
                       superseded=len(data.superseded), errors=len(data.errors),
                       session_summaries=len(data.summaries), sessions=len(sessions), active_days=len(self._days()))

    def _days(self) -> set[str]:
        moments = [row.created_at for _, row in self.data.knowledge] + [row.created_at for _, row in self.data.errors]
        moments += [row.ended_at for _, row in self.data.summaries] + [row.created_at for _, row in self.data.observations]
        return {self.span.local_date(moment).isoformat() for moment in moments}

    def empty(self) -> bool:
        data = self.data
        return not (data.knowledge or data.errors or data.summaries or data.observations or data.confirmed
                    or data.superseded or data.rules or data.history)

    def _item(self, label: str | None, row: KnowledgeRow) -> ReportItem:
        why = decision_why(row) if row.type == "decision" else ""
        detail = "" if row.type == "decision" else clip(row.context, TEXT_CHARS)
        return ReportItem(title=title_of(row.content), type=row.type, at=self.at(row.created_at), status=row.status,
                          why=why, detail=detail, importance=row.importance,
                          author=self.data.authors.get((label, row.id)), workspace=label,
                          sources=[self.ref("knowledge", label, row.id)])

    def items(self, kinds: tuple[str, ...], rank_first: bool = False) -> list[ReportItem]:
        rows = [(label, row) for label, row in self.data.knowledge if row.type in kinds]
        rows.sort(key=lambda pair: ((RANK.get(pair[1].importance or "", len(RANK)) if rank_first else 0),
                                    pair[1].created_at, pair[0] or "", pair[1].id))
        return [self._item(label, row) for label, row in rows]

    def errors(self) -> list[ReportItem]:
        rows = sorted(self.data.errors, key=lambda pair: (RANK.get(pair[1].severity, len(RANK)), pair[1].created_at,
                                                          pair[0] or "", pair[1].id))
        result = []
        for label, row in rows:
            root, _pattern = error_parts(row)
            result.append(ReportItem(title=clip(row.description, TITLE_CHARS) or "(no description)", type="error",
                                     at=self.at(row.created_at), status=row.status, why=root,
                                     detail=clip(row.fix, TEXT_CHARS), severity=row.severity, workspace=label,
                                     sources=[self.ref("error", label, row.id)]))
        return result

    def lessons(self) -> list[ReportItem]:
        entries = [(row.created_at, label or "", 0, row.id, self._item(label, row))
                   for label, row in self.data.knowledge if row.type == "lesson"]
        entries += [(row.created_at, label or "", 1, row.id,
                     ReportItem(title=title_of(row.content), type="rule", at=self.at(row.created_at), status="active",
                                detail=clip(row.context, TEXT_CHARS), workspace=label,
                                sources=[self.ref("rule", label, row.id)]))
                    for label, row in self.data.rules]
        return [entry[-1] for entry in sorted(entries, key=lambda entry: entry[:4])]

    def patterns(self) -> list[ErrorPattern]:
        grouped: dict[tuple[str | None, str], list[ErrorRow]] = defaultdict(list)
        for label, row in self.data.errors:
            grouped[(label, error_parts(row)[1])].append(row)
        history: dict[tuple[str | None, str], list[datetime]] = defaultdict(list)
        for label, key, at in self.data.pattern_history:
            history[(label, key)].append(at)
        result = []
        for (label, key), rows in grouped.items():
            seen = history.get((label, key)) or [row.created_at for row in rows]
            total = max(len(seen), len(rows))
            name = key if label is None else f"{key} ({label})"
            result.append(ErrorPattern(pattern=name, count=len(rows), total=total,
                                       recurring=len(rows) > 1 or total > len(rows),
                                       first_seen=self.at(min(seen)), last_seen=self.at(max(r.created_at for r in rows)),
                                       sources=[self.ref("error", label, row.id) for row in rows][:SOURCE_REFS]))
        result.sort(key=lambda p: (not p.recurring, -p.count, -p.total, p.pattern))
        return result[:self.limit]

    def open_items(self) -> list[OpenItem]:
        found: dict[tuple[str, str], dict] = {}
        for label, row in sorted(self.data.summaries, key=lambda pair: (pair[1].ended_at, pair[0] or "", pair[1].id)):
            for kind, texts in (("next_step", row.next_steps), ("open_question", row.open_questions),
                                ("pitfall", row.pitfalls)):
                for text in texts:
                    key = (kind, " ".join(text.casefold().split()))
                    entry = found.setdefault(key, {"count": 0, "sources": []})
                    entry.update(text=clip(text, TEXT_CHARS), at=row.ended_at, picked_up=row.consumed)
                    entry["count"] += 1
                    entry["sources"].append(self.ref("session_summary", label, row.id))
        ordered = sorted(found.items(), key=lambda pair: (OPEN_ORDER[pair[0][0]], -pair[1]["at"].timestamp(), pair[0][1]))
        return [OpenItem(kind=kind, text=entry["text"], at=self.at(entry["at"]), occurrences=entry["count"],
                         picked_up=entry["picked_up"], sources=entry["sources"][-SOURCE_REFS:])
                for (kind, _), entry in ordered]

    @staticmethod
    def _counted(groups: Mapping[tuple[str, str], list[SourceRef]]) -> list[Counted]:
        items = [Counted(name=name, kind=kind, count=len(refs), sources=refs[:SOURCE_REFS])
                 for (kind, name), refs in groups.items()]
        return sorted(items, key=lambda item: (-item.count, item.kind, item.name))

    def files(self) -> list[Counted]:
        groups: dict[tuple[str, str], list[SourceRef]] = defaultdict(list)

        def add(path: str, ref: SourceRef) -> None:
            name = path.strip().replace("\\", "/")
            if name and ref not in groups[("file", name)]:
                groups[("file", name)].append(ref)

        for label, row in self.data.observations:
            for path in row.files:
                add(path, self.ref("observation", label, row.id))
        for label, row in self.data.knowledge:
            for tag in row.tags:
                if tag.startswith("file:"):
                    add(tag[len("file:"):], self.ref("knowledge", label, row.id))
        for label, row in self.data.errors:
            for tag in row.tags:
                if tag.startswith("file:"):
                    add(tag[len("file:"):], self.ref("error", label, row.id))
        return self._counted(groups)

    def entities(self, sources: Sequence[Source], project: str | None) -> list[Counted]:
        groups: dict[tuple[str, str], list[SourceRef]] = defaultdict(list)
        for source in sources:
            ids = [row.id for label, row in self.data.knowledge if label == source.label]
            for entity in source.repository.entities(ids):
                name = entity.name.strip()
                if (entity.type in RECORD_NODE_TYPES or not name or name in SYSTEM_TAGS
                        or name.startswith(SYSTEM_TAG_PREFIXES) or (entity.type == "project" and name == project)):
                    continue
                ref = self.ref("knowledge", source.label, entity.knowledge_id)
                bucket = groups[(entity.type or "entity", name)]
                if ref not in bucket:
                    bucket.append(ref)
        return self._counted(groups)

    def tags(self) -> list[Counted]:
        groups: dict[tuple[str, str], list[SourceRef]] = defaultdict(list)
        for label, row in self.data.knowledge:
            for tag in dict.fromkeys(row.tags):
                if tag and tag not in SYSTEM_TAGS and not tag.startswith(SYSTEM_TAG_PREFIXES):
                    groups[("tag", tag)].append(self.ref("knowledge", label, row.id))
        return self._counted(groups)

    def timeline(self) -> list[DayActivity]:
        days: dict[str, dict] = defaultdict(lambda: {"by_type": Counter(), "errors": 0, "sessions": set(),
                                                    "summaries": 0, "entries": []})
        for label, row in self.data.knowledge:
            day = days[self.span.local_date(row.created_at).isoformat()]
            day["by_type"][row.type] += 1
            day["sessions"].add((label, row.session_id))
            day["entries"].append((row.created_at, label or "", 0, row.id, TimelineEntry(
                title=title_of(row.content), type=row.type, at=self.at(row.created_at),
                sources=[self.ref("knowledge", label, row.id)])))
        for label, row in self.data.errors:
            day = days[self.span.local_date(row.created_at).isoformat()]
            day["errors"] += 1
            day["sessions"].add((label, row.session_id))
            day["entries"].append((row.created_at, label or "", 1, row.id, TimelineEntry(
                title=clip(row.description, TITLE_CHARS) or "(no description)", type="error",
                at=self.at(row.created_at), sources=[self.ref("error", label, row.id)])))
        for label, row in self.data.summaries:
            day = days[self.span.local_date(row.ended_at).isoformat()]
            day["summaries"] += 1
            day["sessions"].add((label, row.session_id))
        for label, row in self.data.observations:
            days[self.span.local_date(row.created_at).isoformat()]["sessions"].add((label, row.session_id))
        result = []
        for key in sorted(days):
            day = days[key]
            entries = [entry[-1] for entry in sorted(day["entries"], key=lambda entry: entry[:4])]
            result.append(DayActivity(date=key, weekday=datetime.fromisoformat(key).strftime("%a"),
                                      records=sum(day["by_type"].values()), by_type=dict(sorted(day["by_type"].items())),
                                      errors=day["errors"], sessions=len({s for s in day["sessions"] if s[1]}),
                                      summaries=day["summaries"], entries=entries[:TIMELINE_ENTRIES],
                                      more=max(0, len(entries) - TIMELINE_ENTRIES)))
        return result

    def contributors(self) -> list[Contributor]:
        people: dict[str, dict] = {}
        for _, row in self.data.history:
            person = people.setdefault(row.user_id, {"display_name": row.display_name, "saves": 0, "updates": 0,
                                                     "deletes": 0, "confirms": 0})
            field_name = HISTORY_FIELDS.get(row.operation)
            if field_name:
                person[field_name] += 1
        result = [Contributor(user_id=user, **stats) for user, stats in people.items()]
        return sorted(result, key=lambda c: (-(c.saves + c.updates + c.deletes + c.confirms), c.user_id))

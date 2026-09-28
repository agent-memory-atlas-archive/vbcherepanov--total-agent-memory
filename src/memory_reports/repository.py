"""Read-only SQL over a memory.db (a local store or a team workspace). Tables that an older store lacks read as empty.

On the team server a workspace may live in PostgreSQL: the connection is then the tam_db compatibility
connection (same sqlite3 surface; sqlite_master, PRAGMA table_info and julianday are provided there), so
this module stays free of any PostgreSQL import and the personal install keeps using plain sqlite3.
"""
import json
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from memory_reports.periods import utc_text

IN_CHUNK = 500
TIME_COLUMNS = {"knowledge": "created_at", "errors": "created_at", "session_summaries": "ended_at",
                "observations": "created_at"}


def parse_instant(value: object) -> datetime | None:
    """Stored timestamps come as ...Z, ...+00:00 or zone-less UTC; None when unparseable."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace(" ", "T", 1))
    except ValueError:
        return None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def json_list(raw: object) -> list:
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        value = json.loads(raw)
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def _chunks(values: Sequence, size: int = IN_CHUNK) -> Iterable[Sequence]:
    for index in range(0, len(values), size):
        yield values[index:index + size]


@dataclass(frozen=True)
class KnowledgeRow:
    id: int
    session_id: str
    type: str
    content: str
    context: str
    project: str
    tags: tuple[str, ...]
    status: str
    importance: str | None
    created_at: datetime


@dataclass(frozen=True)
class ErrorRow:
    id: int
    session_id: str
    category: str
    severity: str
    description: str
    context: str
    fix: str
    project: str
    tags: tuple[str, ...]
    status: str
    created_at: datetime


@dataclass(frozen=True)
class RuleRow:
    id: int
    content: str
    context: str
    category: str
    created_at: datetime


@dataclass(frozen=True)
class SummaryRow:
    id: str
    session_id: str
    summary: str
    next_steps: tuple[str, ...]
    pitfalls: tuple[str, ...]
    open_questions: tuple[str, ...]
    consumed: bool
    ended_at: datetime


@dataclass(frozen=True)
class ObservationRow:
    id: int
    session_id: str
    files: tuple[str, ...]
    created_at: datetime


@dataclass(frozen=True)
class EntityRow:
    knowledge_id: int
    node_id: str
    name: str
    type: str


@dataclass(frozen=True)
class HistoryRow:
    record_id: int
    operation: str
    user_id: str
    display_name: str
    at: datetime


def _texts(raw: object) -> tuple[str, ...]:
    return tuple(str(item).strip() for item in json_list(raw) if str(item).strip())


class ReportRepository:
    def __init__(self, db: sqlite3.Connection):
        """``db``: a sqlite3.Connection or an object with the same surface (tam_db CompatConnection)."""
        self.db = db
        self.tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.skipped = 0

    def _columns(self, table: str) -> set[str]:
        return {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}

    @staticmethod
    def _window(column: str) -> str:
        return f"julianday({column}) >= julianday(?) AND julianday({column}) < julianday(?)"

    def _rows(self, sql: str, args: Sequence) -> list[dict]:
        cursor = self.db.execute(sql, args)
        names = [d[0] for d in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    def _moment(self, value: object) -> datetime | None:
        moment = parse_instant(value)
        if moment is None:
            self.skipped += 1
        return moment

    @staticmethod
    def _project(project: str | None, column: str = "project") -> tuple[str, list]:
        return (f" AND {column}=?", [project]) if project is not None else ("", [])

    def first_activity(self, project: str | None) -> datetime | None:
        found = []
        for table, column in TIME_COLUMNS.items():
            if table not in self.tables:
                continue
            clause, args = self._project(project)
            row = self.db.execute(f"SELECT {column} FROM {table} WHERE julianday({column}) IS NOT NULL{clause} "
                                  f"ORDER BY julianday({column}) LIMIT 1", args).fetchone()
            moment = parse_instant(row[0]) if row else None
            if moment is not None:
                found.append(moment)
        return min(found, default=None)

    def knowledge(self, project: str | None, start: datetime, end: datetime) -> list[KnowledgeRow]:
        if "knowledge" not in self.tables:
            return []
        importance = "importance" if "importance" in self._columns("knowledge") else "NULL AS importance"
        clause, args = self._project(project)
        rows = self._rows(f"SELECT id,session_id,type,content,context,project,tags,status,{importance},created_at "
                          f"FROM knowledge WHERE {self._window('created_at')}{clause} ORDER BY id",
                          [utc_text(start), utc_text(end), *args])
        result = []
        for row in rows:
            moment = self._moment(row["created_at"])
            if moment is not None:
                result.append(KnowledgeRow(row["id"], row["session_id"] or "", row["type"] or "fact",
                                           row["content"] or "", row["context"] or "", row["project"] or "general",
                                           tuple(str(t) for t in json_list(row["tags"])), row["status"] or "active",
                                           row["importance"], moment))
        return result

    def predecessors(self, ids: Sequence[int]) -> dict[int, list[tuple[int, str]]]:
        """For each id, the records it replaced (status superseded or consolidated into it)."""
        result: dict[int, list[tuple[int, str]]] = {}
        if "knowledge" not in self.tables:
            return result
        for chunk in _chunks(list(ids)):
            marks = ",".join("?" * len(chunk))
            for old, new, status in self.db.execute(
                    f"SELECT id,superseded_by,status FROM knowledge WHERE superseded_by IN ({marks}) ORDER BY id", chunk):
                result.setdefault(int(new), []).append((int(old), status or ""))
        return result

    def superseded(self, project: str | None, start: datetime, end: datetime) -> list[tuple[int, int]]:
        """(old, new) pairs where the replacing record was written inside the window."""
        if "knowledge" not in self.tables:
            return []
        clause, args = self._project(project, "o.project")
        return [(int(old), int(new)) for old, new in self.db.execute(
            "SELECT o.id,o.superseded_by FROM knowledge o JOIN knowledge n ON n.id=o.superseded_by "
            f"WHERE o.status IN ('superseded','consolidated') AND {self._window('n.created_at')}{clause} ORDER BY o.id",
            [utc_text(start), utc_text(end), *args])]

    def confirmed(self, project: str | None, start: datetime, end: datetime) -> list[int]:
        """Records written earlier and confirmed again (same statement saved) inside the window."""
        if "knowledge" not in self.tables or "last_confirmed" not in self._columns("knowledge"):
            return []
        clause, args = self._project(project)
        return [int(row[0]) for row in self.db.execute(
            f"SELECT id FROM knowledge WHERE {self._window('last_confirmed')} "
            f"AND julianday(created_at) < julianday(?){clause} ORDER BY id",
            [utc_text(start), utc_text(end), utc_text(start), *args])]

    def errors(self, project: str | None, start: datetime, end: datetime) -> list[ErrorRow]:
        if "errors" not in self.tables:
            return []
        clause, args = self._project(project)
        rows = self._rows("SELECT id,session_id,category,severity,description,context,fix,project,tags,status,created_at "
                          f"FROM errors WHERE {self._window('created_at')}{clause} ORDER BY id",
                          [utc_text(start), utc_text(end), *args])
        result = []
        for row in rows:
            moment = self._moment(row["created_at"])
            if moment is not None:
                result.append(ErrorRow(row["id"], row["session_id"] or "", row["category"] or "bug",
                                       row["severity"] or "medium", row["description"] or "", row["context"] or "",
                                       row["fix"] or "", row["project"] or "general",
                                       tuple(str(t) for t in json_list(row["tags"])), row["status"] or "open", moment))
        return result

    def error_history(self, project: str | None, end: datetime) -> list[tuple[str, tuple[str, ...], datetime]]:
        """(category, tags, created_at) of every error before `end`, for all-time pattern totals."""
        if "errors" not in self.tables:
            return []
        clause, args = self._project(project)
        result = []
        for category, tags, created in self.db.execute(
                f"SELECT category,tags,created_at FROM errors WHERE julianday(created_at) < julianday(?){clause}",
                [utc_text(end), *args]):
            moment = parse_instant(created)
            if moment is not None:
                result.append((category or "bug", tuple(str(t) for t in json_list(tags)), moment))
        return result

    def rules(self, project: str | None, start: datetime, end: datetime) -> list[RuleRow]:
        if "rules" not in self.tables:
            return []
        clause, args = self._project(project)
        rows = self._rows(f"SELECT id,content,context,category,created_at FROM rules WHERE {self._window('created_at')}"
                          f"{clause} ORDER BY id", [utc_text(start), utc_text(end), *args])
        result = []
        for row in rows:
            moment = self._moment(row["created_at"])
            if moment is not None:
                result.append(RuleRow(row["id"], row["content"] or "", row["context"] or "", row["category"] or "",
                                      moment))
        return result

    def summaries(self, project: str | None, start: datetime, end: datetime) -> list[SummaryRow]:
        if "session_summaries" not in self.tables:
            return []
        clause, args = self._project(project)
        rows = self._rows("SELECT id,session_id,summary,next_steps,pitfalls,open_questions,consumed,ended_at "
                          f"FROM session_summaries WHERE {self._window('ended_at')}{clause} ORDER BY id",
                          [utc_text(start), utc_text(end), *args])
        result = []
        for row in rows:
            moment = self._moment(row["ended_at"])
            if moment is not None:
                result.append(SummaryRow(str(row["id"]), row["session_id"] or "", row["summary"] or "",
                                         _texts(row["next_steps"]), _texts(row["pitfalls"]),
                                         _texts(row["open_questions"]), bool(row["consumed"]), moment))
        return result

    def observations(self, project: str | None, start: datetime, end: datetime) -> list[ObservationRow]:
        if "observations" not in self.tables:
            return []
        clause, args = self._project(project)
        rows = self._rows(f"SELECT id,session_id,files_affected,created_at FROM observations "
                          f"WHERE {self._window('created_at')}{clause} ORDER BY id",
                          [utc_text(start), utc_text(end), *args])
        result = []
        for row in rows:
            moment = self._moment(row["created_at"])
            if moment is not None:
                result.append(ObservationRow(row["id"], row["session_id"] or "", _texts(row["files_affected"]), moment))
        return result

    def sessions(self, project: str | None, start: datetime, end: datetime) -> set[str]:
        if "sessions" not in self.tables:
            return set()
        clause, args = self._project(project)
        return {str(row[0]) for row in self.db.execute(
            f"SELECT id FROM sessions WHERE {self._window('started_at')}{clause}",
            [utc_text(start), utc_text(end), *args])}

    def entities(self, knowledge_ids: Sequence[int]) -> list[EntityRow]:
        if not {"knowledge_nodes", "graph_nodes"} <= self.tables:
            return []
        result = []
        for chunk in _chunks(list(knowledge_ids)):
            marks = ",".join("?" * len(chunk))
            result.extend(EntityRow(int(kid), str(node), name or "", kind or "") for kid, node, name, kind in
                          self.db.execute("SELECT kn.knowledge_id,g.id,g.name,g.type FROM knowledge_nodes kn "
                                          "JOIN graph_nodes g ON g.id=kn.node_id "
                                          f"WHERE kn.knowledge_id IN ({marks}) AND COALESCE(g.status,'active')='active' "
                                          "ORDER BY kn.knowledge_id,g.id", chunk))
        return result

    def history(self, start: datetime, end: datetime) -> list[HistoryRow]:
        """Team workspaces only: who saved, changed or deleted records inside the window."""
        if "tam_history" not in self.tables:
            return []
        result = []
        for record_id, operation, actor, at in self.db.execute(
                f"SELECT record_id,operation,actor,at FROM tam_history WHERE {self._window('at')} ORDER BY sequence",
                [utc_text(start), utc_text(end)]):
            moment = self._moment(at)
            try:
                who = json.loads(actor) if actor else {}
            except ValueError:
                who = {}
            if moment is not None and isinstance(who, dict):
                result.append(HistoryRow(int(record_id), operation or "", str(who.get("user_id") or "unknown"),
                                         str(who.get("display_name") or who.get("user_id") or "unknown"), moment))
        return result

    def authors(self, ids: Sequence[int]) -> dict[int, str]:
        """Team workspaces only: display name of each record's creator."""
        if "tam_authorship" not in self.tables:
            return {}
        result = {}
        for chunk in _chunks(list(ids)):
            marks = ",".join("?" * len(chunk))
            for record_id, created_by in self.db.execute(
                    f"SELECT record_id,created_by FROM tam_authorship WHERE record_id IN ({marks})", chunk):
                try:
                    who = json.loads(created_by)
                except ValueError:
                    continue
                if isinstance(who, dict):
                    result[int(record_id)] = str(who.get("display_name") or who.get("user_id") or "")
        return result

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tam_db.contracts import CompatConnection
from team_memory.contracts import Unavailable
from team_memory.database import SwitchableControlPlane

SAVE_OPERATIONS = ("insert", "confirm")
CHANGE_OPERATIONS = ("update", "delete")
EXCERPT_CHARS = 160
READ_TIMEOUT_SECONDS = 5


def day_keys(days: int, today: datetime | None = None) -> list[str]:
    end = (today or datetime.now(UTC)).date()
    return [(end - timedelta(days=offset)).isoformat() for offset in range(days - 1, -1, -1)]


def since_iso(days: int, today: datetime | None = None) -> str:
    return day_keys(days, today)[0] + "T00:00:00"


def excerpt(state: str | None) -> str:
    if not state:
        return ""
    content = str(json.loads(state).get("content") or "")
    return content if len(content) <= EXCERPT_CHARS else content[:EXCERPT_CHARS - 1].rstrip() + "…"


class WorkspaceReader:
    """Read-only statistics over workspace memory; never loads the memory runtime.

    With a control plane the workspace is read through it (memory.db or the workspace's
    PostgreSQL schema, as that schema's role); without one, memory.db files under ``root``.
    """

    def __init__(self, root: Path, plane: SwitchableControlPlane | None = None):
        self.root = root
        self.plane = plane

    @contextmanager
    def _connection(self, key: str) -> Iterator[CompatConnection | None]:
        if self.plane is not None:
            with self.plane.workspace_reader(key) as db:
                yield db
            return
        path = self.root / "workspaces" / key / "memory.db"
        if not path.is_file():
            yield None
            return
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=READ_TIMEOUT_SECONDS)) as db:
            yield db

    @contextmanager
    def _open(self, key: str) -> Iterator[CompatConnection | None]:
        try:
            with self._connection(key) as db:
                if db is None:
                    yield None
                    return
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                yield db if {"tam_history", "knowledge"} <= tables else None
        except sqlite3.Error as exc:
            raise Unavailable("Workspace statistics are temporarily unavailable") from exc

    def active_records(self, key: str) -> int:
        with self._open(key) as db:
            return 0 if db is None else db.execute("SELECT COUNT(*) FROM knowledge WHERE status='active'").fetchone()[0]

    def activity(self, key: str) -> dict[str, dict]:
        with self._open(key) as db:
            rows = [] if db is None else db.execute(
                "SELECT json_extract(actor,'$.user_id'),operation,COUNT(*),MAX(at) FROM tam_history GROUP BY 1,2").fetchall()
        result: dict[str, dict] = {}
        for user_id, operation, count, last in rows:
            stats = result.setdefault(user_id, {"saves": 0, "changes": 0, "last_activity": None})
            if operation in SAVE_OPERATIONS:
                stats["saves"] += count
            elif operation in CHANGE_OPERATIONS:
                stats["changes"] += count
            stats["last_activity"] = max(filter(None, (stats["last_activity"], last)), default=None)
        return result

    def daily_saves(self, key: str, days: int, user_id: str | None = None) -> dict[str, dict[str, int]]:
        query = ("SELECT json_extract(actor,'$.user_id'),substr(at,1,10),COUNT(*) FROM tam_history "
                 "WHERE at>=? AND operation IN ('insert','confirm')")
        args: list = [since_iso(days)]
        if user_id is not None:
            query += " AND json_extract(actor,'$.user_id')=?"
            args.append(user_id)
        with self._open(key) as db:
            rows = [] if db is None else db.execute(query + " GROUP BY 1,2", args).fetchall()
        result: dict[str, dict[str, int]] = {}
        for actor, day, count in rows:
            result.setdefault(actor, {})[day] = count
        return result

    def recent(self, key: str, limit: int, user_id: str | None = None,
               operations: tuple[str, ...] = ("insert", "update", "delete", "confirm")) -> list[dict]:
        marks = ",".join("?" * len(operations))
        query = (f"SELECT record_id,operation,at,actor,after_state,before_state FROM tam_history "
                 f"WHERE operation IN ({marks})")
        args: list = list(operations)
        if user_id is not None:
            query += " AND json_extract(actor,'$.user_id')=?"
            args.append(user_id)
        with self._open(key) as db:
            rows = [] if db is None else db.execute(query + " ORDER BY sequence DESC LIMIT ?", [*args, limit]).fetchall()
        return [{"record_id": record_id, "operation": operation, "at": at,
                 "actor": json.loads(actor).get("display_name", ""), "excerpt": excerpt(after or before)}
                for record_id, operation, at, actor, after, before in rows]


def series(per_day: dict[str, int], days: int) -> list[int]:
    return [per_day.get(day, 0) for day in day_keys(days)]


def older_than(value: str | None, days: int) -> bool:
    if not value:
        return True
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment < datetime.now(UTC) - timedelta(days=days)

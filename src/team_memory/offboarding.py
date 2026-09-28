"""Export or purge the personal area of a disabled user (`tam-team user-export`, `tam-team user-purge`).

Personal memory is private to its owner, so both commands refuse an active user: run `user-disable` first.
Onboarding notes waiting in the learning database for the user's personal area (`personal_outbox`) are
personal content too: export includes them and purge deletes them. Export reads the workspace read-only
and is safe while the server runs; a disabled user cannot write to it any more. Purge deletes the
workspace (its memory.db directory, or on PostgreSQL its schema and role in one transaction) and any
SQLite archive copy of it left by a migration, so, like `backup`, it needs the server stopped and detects
a running server or a live workspace process through their leases. Team and shared records the user
wrote live in other workspaces and keep their author. Backups taken earlier (and PostgreSQL WAL archives
or dumps) still contain the personal area.
"""
import json
import os
import shutil
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path

from tam_db.contracts import Backend, CompatConnection, ControlKind
from team_memory.contracts import Conflict, DomainError, Forbidden
from team_memory.database import archived_workspaces
from team_memory.lifecycle import ServerLease
from team_memory.registry import Registry
from version import VERSION

JSON_COLUMNS = ("created_by", "updated_by", "actor", "before_state", "after_state")
LEARNING_DB = "learning.db"


def personal_key(registry: Registry, user_id: str) -> str:
    return "personal_" + registry.digest(user_id)


def personal_workspace(registry: Registry, user_id: str) -> Path:
    return registry.root / "workspaces" / personal_key(registry, user_id)


def _require_disabled(registry: Registry, user_id: str) -> None:
    with registry.connect() as db:
        row = db.execute("SELECT active FROM users WHERE id=?", (user_id,)).fetchone()
    if row is None:
        raise DomainError("Unknown user")
    if row["active"]:
        raise Forbidden("Personal memory is private; run user-disable first")


@contextmanager
def _personal(registry: Registry, user_id: str) -> Iterator[CompatConnection]:
    with registry.plane.workspace_reader(personal_key(registry, user_id)) as db:
        if db is None:
            raise Conflict("No personal memory is stored for this user")
        yield db


def _rows(db: CompatConnection, query: str) -> list[dict]:
    db.row_factory = sqlite3.Row
    out = []
    for row in db.execute(query):
        item = dict(row)
        for column in JSON_COLUMNS:
            if isinstance(item.get(column), str):
                item[column] = json.loads(item[column])
        out.append(item)
    return out


def _learning_stored(registry: Registry) -> bool:
    # On SQLite, opening learning.db would create it; nothing is stored when it does not exist.
    return registry.plane.backend is Backend.POSTGRES or (registry.root / LEARNING_DB).is_file()


def _has_outbox(db: CompatConnection) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='personal_outbox'").fetchone() is not None


def _outbox(registry: Registry, user_id: str) -> list[dict]:
    if not _learning_stored(registry):
        return []
    with registry.plane.connect(ControlKind.LEARNING) as db:
        if not _has_outbox(db):
            return []
        return [dict(row) for row in db.execute("SELECT * FROM personal_outbox WHERE user_id=? ORDER BY id", (user_id,))]


def _delete_outbox(registry: Registry, user_id: str) -> int:
    if not _learning_stored(registry):
        return 0
    with registry.plane.connect(ControlKind.LEARNING, write=True) as db:
        return db.execute("DELETE FROM personal_outbox WHERE user_id=?", (user_id,)).rowcount if _has_outbox(db) else 0


def _workspace_lease(registry: Registry, user_id: str) -> AbstractContextManager:
    """No live worker may serve the workspace while it is purged: a file lease on SQLite, an
    advisory lock on PostgreSQL (the workspace has no directory there)."""
    if registry.plane.backend is Backend.SQLITE:
        return ServerLease(personal_workspace(registry, user_id))
    from team_memory.pg_provision import workspace_lease

    current = registry.plane.current()
    return workspace_lease(current.url, personal_key(registry, user_id), current.settings,
                           connect_overrides=current.connect_kwargs())


def _archived_copies(registry: Registry, key: str) -> list[Path]:
    """SQLite copies of the workspace kept by a migration archive."""
    return [path for path in archived_workspaces(registry.root, key) if path.exists()]


def _json(value):
    if isinstance(value, bytes):
        return {"hex": value.hex()}
    raise TypeError(f"Unsupported value in export: {type(value).__name__}")


def export_personal(registry: Registry, user_id: str, out: Path) -> dict[str, int]:
    """Write every personal record (all statuses, with authorship) and its full history as JSONL (mode 0600)."""
    _require_disabled(registry, user_id)
    with _personal(registry, user_id) as db:
        records = _rows(db, "SELECT k.*, a.created_by, a.updated_by, a.revision FROM knowledge k "
                            "LEFT JOIN tam_authorship a ON a.record_id=k.id ORDER BY k.id")
        history = _rows(db, "SELECT * FROM tam_history ORDER BY sequence")
    outbox = _outbox(registry, user_id)
    header = {"type": "export", "user_id": user_id, "package_version": VERSION,
              "exported_at": datetime.now(UTC).isoformat(), "records": len(records), "history": len(history),
              "onboarding_notes": len(outbox)}
    descriptor = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            for line in (header, *({"type": "record", "record": r} for r in records),
                         *({"type": "history", "event": h} for h in history),
                         *({"type": "onboarding_note", "note": n} for n in outbox)):
                handle.write(json.dumps(line, ensure_ascii=False, default=_json) + "\n")
    except BaseException:
        out.unlink(missing_ok=True)
        raise
    with registry.connect() as db:
        registry._event(db, "personal_exported", user_id)
    return {"records": len(records), "history": len(history), "onboarding_notes": len(outbox)}


def _servers_stopped(registry: Registry) -> AbstractContextManager:
    """No server may run: the file lease of this data directory and, on PostgreSQL, the advisory
    server lease, which also catches a server on another host sharing the database."""
    stack = ExitStack()
    stack.enter_context(ServerLease(registry.root))
    if registry.plane.backend is Backend.POSTGRES:
        from team_memory.pg_provision import server_lease

        current = registry.plane.current()
        try:
            stack.enter_context(server_lease(current.url, current.settings, connect_overrides=current.connect_kwargs()))
        except BaseException:
            stack.close()
            raise
    return stack


def purge_personal(registry: Registry, user_id: str, confirm: str,
                   on_dropped: Callable[[str], None] | None = None) -> dict[str, int]:
    """Delete the personal workspace of a disabled user. Needs the server stopped.

    ``on_dropped(key)`` runs right after the workspace is dropped, so a process that keeps state
    about provisioned workspaces (WorkerPool.forget) re-provisions it for a re-created user."""
    if confirm != user_id:
        raise DomainError("--confirm must repeat the user ID")
    _require_disabled(registry, user_id)
    key = personal_key(registry, user_id)
    with _servers_stopped(registry):
        if not registry.plane.workspaces.exists(key):
            raise Conflict("No personal memory is stored for this user")
        with _workspace_lease(registry, user_id):
            with _personal(registry, user_id) as db:
                records = db.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]
            notes = _delete_outbox(registry, user_id)
            archived = _archived_copies(registry, key)
        registry.plane.workspaces.drop(key)
        if on_dropped is not None:
            on_dropped(key)
        # On PostgreSQL the directory only holds the worker's lease file; nothing of the user stays.
        leftover = personal_workspace(registry, user_id)
        if leftover.is_dir():
            shutil.rmtree(leftover)
        for path in archived:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        with registry.connect() as db:
            # set_active(False) already did this; repeated so a purge never leaves a way back in.
            db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM invites WHERE user_id=? AND used_at IS NULL", (user_id,))
            db.execute("UPDATE tokens SET revoked=1 WHERE user_id=? AND revoked=0", (user_id,))
            registry._event(db, "personal_purged", user_id,
                            f"records={records} onboarding_notes={notes} archived_copies={len(archived)}")
    return {"records": records, "onboarding_notes": notes}

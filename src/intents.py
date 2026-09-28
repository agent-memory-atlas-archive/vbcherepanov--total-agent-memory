"""User intents module — capture and query user prompts.

Backing table: `intents` (see migrations/013_intents.sql).

The write path is fed by `hooks/user-prompt-submit.sh`, which runs on every
UserPromptSubmit event. Reads are exposed via MCP tools `list_intents` /
`search_intents` and can also be used from the dashboard.

Dedup policy: if the exact same prompt (sha256) is submitted in the same
session within DEDUP_WINDOW_SECONDS, the duplicate is silently dropped —
covers accidental double-enter, retries and /resume replays.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from secret_redaction import redact_secrets

# 5-minute dedup window (per-session same-prompt duplicates get collapsed).
DEDUP_WINDOW_SECONDS = 5 * 60


def _utc_now_iso() -> str:
    """Return current UTC time as ISO-8601 string (seconds precision, with Z)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(text: str) -> str:
    """Deterministic sha256 hex digest of a string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a writable sqlite3 connection to `db_path` with Row factory."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_table(db: sqlite3.Connection) -> None:
    """Create `intents` table inline when the DB was not migrated yet.

    Production DBs get the table via `migrations/013_intents.sql`, but in
    tests we may open a fresh tmp DB; this keeps the module self-sufficient.
    """
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS intents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            project TEXT,
            prompt TEXT NOT NULL,
            created_at TEXT NOT NULL,
            turn_index INTEGER,
            prompt_hash TEXT NOT NULL
        )
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_intents_session ON intents(session_id)")
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_intents_project ON intents(project, created_at)"
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_intents_hash ON intents(prompt_hash)")


def save_intent(
    db_path: str | Path,
    prompt: str,
    session_id: str | None,
    project: str | None,
) -> int:
    """Insert one user prompt, returning the new row id or 0 on dedup/empty.

    - Empty / whitespace-only prompts are a no-op (returns 0).
    - If the same prompt hash was written for the same session_id within
      DEDUP_WINDOW_SECONDS, returns the existing row id (no duplicate write).
    - turn_index is auto-assigned as (max existing turn_index in session) + 1,
      or 0 if this is the first entry for the session.
    """
    if not prompt or not prompt.strip():
        return 0
    prompt, _ = redact_secrets(prompt)

    phash = _sha256(prompt)
    now = _utc_now_iso()

    db = _connect(db_path)
    try:
        _ensure_table(db)

        # Dedup: same hash, same session, within the window → skip.
        if session_id:
            cutoff = (
                datetime.now(timezone.utc) - timedelta(seconds=DEDUP_WINDOW_SECONDS)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            row = db.execute(
                "SELECT id FROM intents "
                "WHERE prompt_hash = ? AND session_id = ? AND created_at >= ? "
                "ORDER BY id DESC LIMIT 1",
                (phash, session_id, cutoff),
            ).fetchone()
            if row:
                return int(row["id"])

            # Next turn index for this session
            max_turn = db.execute(
                "SELECT MAX(turn_index) FROM intents WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
            turn_index = 0 if max_turn is None else int(max_turn) + 1
        else:
            turn_index = 0

        cur = db.execute(
            "INSERT INTO intents "
            "(session_id, project, prompt, created_at, turn_index, prompt_hash) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, project, prompt, now, turn_index, phash),
        )
        db.commit()
        return int(cur.lastrowid or 0)
    finally:
        db.close()


def list_intents(
    db_path: str | Path,
    project: str | None = None,
    session_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return recent intents filtered by project and/or session, newest first."""
    limit = max(1, min(500, int(limit)))
    conds: list[str] = []
    params: list[Any] = []
    if project:
        conds.append("project = ?")
        params.append(project)
    if session_id:
        conds.append("session_id = ?")
        params.append(session_id)
    where = (" WHERE " + " AND ".join(conds)) if conds else ""

    db = _connect(db_path)
    try:
        _ensure_table(db)
        rows = db.execute(
            "SELECT id, session_id, project, prompt, created_at, turn_index, prompt_hash "
            f"FROM intents{where} "
            "ORDER BY id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()


def search_intents(
    db_path: str | Path,
    query: str,
    project: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """LIKE-search over prompt text. Returns newest match first.

    We intentionally use LIKE instead of FTS5 here because this table is
    write-heavy on every user turn and is fine with simple substring match;
    the typical query set is "what did I ask about X" over recent intents.
    """
    limit = max(1, min(500, int(limit)))
    q = (query or "").strip()
    if not q:
        return []

    conds: list[str] = ["prompt LIKE ?"]
    params: list[Any] = [f"%{q}%"]
    if project:
        conds.append("project = ?")
        params.append(project)

    db = _connect(db_path)
    try:
        _ensure_table(db)
        rows = db.execute(
            "SELECT id, session_id, project, prompt, created_at, turn_index, prompt_hash "
            "FROM intents WHERE " + " AND ".join(conds) + " "
            "ORDER BY id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()

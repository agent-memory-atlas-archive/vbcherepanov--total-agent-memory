"""User registry, retention purge and the deletion journal.

Each AML user_id maps to `users/<ns>/` where ns = sha256(user_id)[:32]. The
registry records when each user was last written; the purge deletes a user's
whole directory once it is older than the retention window and appends one
JSON line per deletion to `deletion-journal.jsonl` for the post-run report.
The journal holds ids, counts and timestamps only — never memory content.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

LOGGER = logging.getLogger("aml_adapter.registry")
SECONDS_PER_DAY = 86400
NS_LENGTH = 32
JOURNAL_NAME = "deletion-journal.jsonl"


def user_namespace(user_id: str) -> str:
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:NS_LENGTH]


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class UserRecord:
    ns: str
    user_id: str
    created_at: float
    last_write_at: float


class Registry:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.users_dir = data_dir / "users"
        self.journal_path = data_dir / JOURNAL_NAME
        self.users_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(data_dir / "registry.db"), check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                ns TEXT PRIMARY KEY, user_id TEXT NOT NULL UNIQUE,
                created_at REAL NOT NULL, last_write_at REAL NOT NULL)""")
        self.db.commit()

    def record_write(self, user_id: str) -> str:
        ns = user_namespace(user_id)
        now = time.time()
        with self.lock:
            self.db.execute(
                "INSERT INTO users(ns, user_id, created_at, last_write_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(ns) DO UPDATE SET last_write_at=excluded.last_write_at", (ns, user_id, now, now))
            self.db.commit()
        return ns

    def lookup(self, user_id: str) -> str | None:
        ns = user_namespace(user_id)
        with self.lock:
            row = self.db.execute("SELECT 1 FROM users WHERE ns=?", (ns,)).fetchone()
        return ns if row is not None else None

    def users(self) -> list[UserRecord]:
        with self.lock:
            rows = self.db.execute("SELECT ns, user_id, created_at, last_write_at FROM users ORDER BY ns").fetchall()
        return [UserRecord(*row) for row in rows]

    def expired(self, retention_days: float, now: float | None = None) -> list[UserRecord]:
        cutoff = (time.time() if now is None else now) - retention_days * SECONDS_PER_DAY
        return [user for user in self.users() if user.last_write_at < cutoff]

    def delete(self, user: UserRecord, reason: str) -> dict | None:
        """Remove every byte stored for `user`, then journal it.

        The caller must hold the pool's exclusive lock for the user. Returns
        None (and deletes nothing) when the user was written after `user` was
        read — a fresh Add must never be purged by a stale decision.
        """
        with self.lock:
            row = self.db.execute("SELECT last_write_at FROM users WHERE ns=?", (user.ns,)).fetchone()
            if row is None or row[0] != user.last_write_at:
                return None
            self.db.execute("DELETE FROM users WHERE ns=?", (user.ns,))
            self.db.commit()
        user_dir = self.users_dir / user.ns
        fragments, requests = _count_rows(user_dir / "memory.db")
        size = sum(path.stat().st_size for path in user_dir.rglob("*") if path.is_file()) if user_dir.exists() else 0
        if user_dir.exists():
            shutil.rmtree(user_dir)
        entry = {
            "event": "aml_user_data_deleted", "reason": reason, "user_ns": user.ns, "user_id": user.user_id,
            "created_at": _iso(user.created_at), "last_write_at": _iso(user.last_write_at),
            "deleted_at": _iso(time.time()), "fragments": fragments, "requests": requests, "bytes": size,
        }
        with self.lock, self.journal_path.open("a", encoding="utf-8") as journal:
            journal.write(json.dumps(entry, ensure_ascii=False) + "\n")
            journal.flush()
            os.fsync(journal.fileno())
        LOGGER.info(json.dumps({k: v for k, v in entry.items() if k != "user_id"}))
        return entry

    def close(self) -> None:
        with self.lock:
            self.db.close()


def _count_rows(db_path: Path) -> tuple[int, int]:
    if not db_path.exists():
        return 0, 0
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        fragments = connection.execute("SELECT COUNT(*) FROM aml_fragments").fetchone()[0]
        requests = connection.execute("SELECT COUNT(*) FROM aml_requests").fetchone()[0]
        return int(fragments), int(requests)
    except sqlite3.OperationalError:
        # A store that never finished its first Add has no AML tables yet.
        return 0, 0
    finally:
        connection.close()

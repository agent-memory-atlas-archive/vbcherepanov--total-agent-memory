from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager, closing
from datetime import datetime, timezone
from typing import Self

from memory_core.telemetry import counters, op_timer
from tam_db.contracts import Backend


class LeaseLost(RuntimeError):
    pass


class LeaseUnsupported(RuntimeError):
    pass


def assert_owned(db: sqlite3.Connection, task_id: int, token: str) -> None:
    if (
        db.execute(
            "SELECT 1 FROM enrichment_queue WHERE id=? AND lease_token=? "
            "AND status='processing'",
            (task_id, token),
        ).fetchone()
        is None
    ):
        counters.bump("enrichment_lease_lost")
        raise LeaseLost(f"Enrichment lease lost for task {task_id}")


class LeaseHeartbeat(AbstractContextManager):
    def __init__(
        self,
        db: sqlite3.Connection,
        leases: Sequence[tuple[int, str]],
        interval: float,
        logger: logging.Logger,
    ):
        self.leases = tuple(leases)
        self.interval = interval
        self.logger = logger
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        if getattr(db, "backend", None) is Backend.POSTGRES:
            # The heartbeat renews leases over its own sqlite3 connection to the
            # database file; async enrichment is refused on PostgreSQL
            # (enrichment_worker.reject_postgres), so no lease exists to renew here.
            raise LeaseUnsupported("Enrichment lease heartbeat requires a SQLite database")
        self.path = next(
            (row[2] for row in db.execute("PRAGMA database_list") if row[1] == "main"),
            "",
        )

    def __enter__(self) -> Self:
        if self.path and self.leases:
            self.thread = threading.Thread(
                target=self._run, daemon=True, name="enrichment-heartbeat"
            )
            self.thread.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.stop.set()
        if self.thread:
            self.thread.join()

    def _run(self) -> None:
        try:
            with closing(sqlite3.connect(self.path, timeout=self.interval)) as db:
                while not self.stop.wait(self.interval):
                    self.renew(db)
        except sqlite3.Error:
            counters.bump("enrichment_heartbeat_errors")
            self.logger.exception("Enrichment heartbeat failed")

    def renew(self, db: sqlite3.Connection) -> None:
        with op_timer("enrichment_heartbeat_ms"):
            now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            db.executemany(
                "UPDATE enrichment_queue SET heartbeat_at=? "
                "WHERE id=? AND lease_token=? AND status='processing'",
                [(now, task_id, token) for task_id, token in self.leases],
            )
            db.commit()


def enqueue_rebuilds(db: sqlite3.Connection, limit: int, now: Callable[[], str]) -> int:
    with op_timer("atomic_fact_rebuild_enqueue_ms"):
        db.execute("SAVEPOINT enqueue_atomic_rebuilds")
        try:
            rows = db.execute(
                "SELECT knowledge_id FROM atomic_fact_rebuild ORDER BY knowledge_id LIMIT ?",
                (limit,),
            ).fetchall()
            for row in rows:
                db.execute(
                    "INSERT INTO enrichment_queue(knowledge_id,session_id,project,ktype,content_snapshot,"
                    "tags_snapshot,importance,skip_quality,enqueued_at,atomic_only) "
                    "SELECT id,session_id,project,type,content,COALESCE(tags,'[]'),"
                    "COALESCE(importance,'medium'),1,?,1 FROM knowledge WHERE id=? AND status='active'",
                    (now(), row[0]),
                )
                db.execute(
                    "DELETE FROM atomic_fact_rebuild WHERE knowledge_id=?", (row[0],)
                )
            db.execute("RELEASE enqueue_atomic_rebuilds")
        except sqlite3.Error:
            db.execute("ROLLBACK TO enqueue_atomic_rebuilds")
            db.execute("RELEASE enqueue_atomic_rebuilds")
            raise
        db.commit()
        counters.bump("atomic_fact_rebuild_enqueued", len(rows))
        return len(rows)

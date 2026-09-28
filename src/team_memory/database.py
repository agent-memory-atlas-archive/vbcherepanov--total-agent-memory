"""Switchable control plane of the team server (plan 4.4): identity and learning on SQLite or PostgreSQL.

Every repository (Registry, Accounts, SettingsStore, SetupService, LearningRepository) and every
gateway reader (insights, reports, offboarding export) reaches the database through one
``SwitchableControlPlane``. ``connect`` holds a read lock for the length of one transaction and
``activate`` takes the write lock, so no call straddles a backend switch.

SQLite keeps today's files and semantics (``identity.db`` deferred transactions unless a writer
asks for BEGIN IMMEDIATE, ``learning.db`` always BEGIN IMMEDIATE). PostgreSQL runs every
control-plane transaction SERIALIZABLE over the tam_db compatibility connection, so repository
SQL stays unchanged; ``transaction`` and the ``serializable`` decorator retry on 40001.
"""
import functools
import json
import logging
import random
import shutil
import sqlite3
import threading
import time
import uuid
import weakref
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Protocol, TypeVar

from tam_db.contracts import (
    COMPAT_SCHEMA,
    CONTROL_SCHEMAS,
    EXTENSIONS_SCHEMA,
    ActiveDatabase,
    Backend,
    CompatConnection,
    ControlKind,
    DatabaseSettings,
    PgSerializationFailure,
    StoreDatabase,
    WorkspaceProvisioner,
    WorkspaceTarget,
)
from team_memory.contracts import Conflict, Unavailable
from team_memory.database_contracts import ARCHIVE_DIR, ARCHIVE_PREFIX

LOGGER = logging.getLogger(__name__)
IDENTITY_DB = "identity.db"
LEARNING_DB = "learning.db"
WORKSPACES_DIR = "workspaces"
WORKSPACE_DB = "memory.db"
SQLITE_TIMEOUT_SECONDS = 10
READ_TIMEOUT_SECONDS = 5
META_INSTANCE_KEY = "instance_id"
RETRY_BASE_SECONDS = 0.01
RETRY_JITTER_SECONDS = 0.02
POOL_ACQUIRE_TIMEOUT_SECONDS = 10
CONTROL_APPLICATION = "tam-control"
SERIALIZABLE_OPTION = "-c default_transaction_isolation=serializable"
UTC_OPTION = "-c TimeZone=UTC"

T = TypeVar("T")


class ReadWriteLock:
    """Many concurrent readers or one writer; writers go first once waiting.

    Re-entrant for readers of the same thread (a repository call nested in another must not
    deadlock behind a waiting writer); a writer inside a read section is a programming error.
    """

    def __init__(self):
        self._condition = threading.Condition()
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0
        self._local = threading.local()

    def _depth(self) -> int:
        return getattr(self._local, "depth", 0)

    @contextmanager
    def read(self) -> Iterator[None]:
        depth = self._depth()
        if depth == 0:
            with self._condition:
                while self._writer or self._waiting_writers:
                    self._condition.wait()
                self._readers += 1
        self._local.depth = depth + 1
        try:
            yield
        finally:
            self._local.depth -= 1
            if self._local.depth == 0:
                with self._condition:
                    self._readers -= 1
                    if not self._readers:
                        self._condition.notify_all()

    @contextmanager
    def write(self) -> Iterator[None]:
        if self._depth():
            raise RuntimeError("activate() inside a control-plane transaction would deadlock")
        with self._condition:
            self._waiting_writers += 1
            try:
                while self._writer or self._readers:
                    self._condition.wait()
            finally:
                self._waiting_writers -= 1
            self._writer = True
        try:
            yield
        finally:
            with self._condition:
                self._writer = False
                self._condition.notify_all()


def sqlite_instance_id(root: Path, *, create: bool) -> str | None:
    """Installation id kept in identity.db's meta table (plan 4.3); created on demand."""
    path = root / IDENTITY_DB
    if not create and not path.is_file():
        return None
    if create:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(sqlite3.connect(path, timeout=SQLITE_TIMEOUT_SECONDS, isolation_level=None)) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                db.execute("INSERT OR IGNORE INTO meta(key,value) VALUES (?,?)", (META_INSTANCE_KEY, str(uuid.uuid4())))
                value = db.execute("SELECT value FROM meta WHERE key=?", (META_INSTANCE_KEY,)).fetchone()[0]
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        path.chmod(0o600)
        return value
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=SQLITE_TIMEOUT_SECONDS)) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone() is None:
            return None
        row = db.execute("SELECT value FROM meta WHERE key=?", (META_INSTANCE_KEY,)).fetchone()
    return None if row is None else row[0]


def archived_workspaces(root: Path, key: str) -> list[Path]:
    """Every SQLite copy of workspace ``key`` kept by a migration archive (archive/sqlite-*/workspaces/<key>)."""
    base = root / ARCHIVE_DIR
    if not base.is_dir():
        return []
    return sorted(path for path in base.glob(f"{ARCHIVE_PREFIX}*/{WORKSPACES_DIR}/{key}")
                  if path.is_dir() and not path.is_symlink())


def sqlite_has_data(root: Path) -> bool:
    """Whether the root holds SQLite control or workspace data worth migrating (users, teams or memory)."""
    if any((root / WORKSPACES_DIR).glob(f"*/{WORKSPACE_DB}")):
        return True
    path = root / IDENTITY_DB
    if not path.is_file():
        return False
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=SQLITE_TIMEOUT_SECONDS)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return any(db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() for table in ("users", "teams")
                   if table in tables)


class ControlBackend(Protocol):
    workspaces: WorkspaceProvisioner

    def connect(self, kind: ControlKind, write: bool) -> Any: ...

    def workspace_reader(self, key: str) -> Any: ...

    def close(self) -> None: ...


class SqliteWorkspaces:
    """WorkspaceProvisioner for SQLite: a workspace is ``<root>/workspaces/<key>/`` with its memory.db."""

    def __init__(self, root: Path, instance_id: str):
        self.root, self.instance_id = root, instance_id

    def path(self, key: str) -> Path:
        WorkspaceTarget.for_key(key, self.instance_id)
        return self.root / WORKSPACES_DIR / key

    def ensure(self, key: str, *, migrate: bool = True) -> WorkspaceTarget:
        """The directory; its memory.db is created and migrated by the worker's Store (``migrate``
        exists for parity with PostgreSQL)."""
        self.path(key).mkdir(parents=True, exist_ok=True, mode=0o700)
        return WorkspaceTarget.for_key(key, self.instance_id)

    def exists(self, key: str) -> bool:
        return self.path(key).exists()

    def drop(self, key: str) -> None:
        path = self.path(key)
        if path.exists():
            shutil.rmtree(path)

    def store_database(self, key: str) -> StoreDatabase:
        return StoreDatabase.sqlite()


class SqliteControlBackend:
    def __init__(self, root: Path, instance_id: str):
        self.root = root
        self.workspaces = SqliteWorkspaces(root, instance_id)

    @contextmanager
    def connect(self, kind: ControlKind, write: bool) -> Iterator[sqlite3.Connection]:
        if kind is ControlKind.LEARNING:
            with self._learning() as db:
                yield db
            return
        db = sqlite3.connect(self.root / IDENTITY_DB, timeout=SQLITE_TIMEOUT_SECONDS)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                if write:
                    # Takes the write lock before the first read, so read-then-write checks
                    # (at least one superadmin remains) cannot interleave.
                    db.execute("BEGIN IMMEDIATE")
                yield db
        finally:
            db.close()

    @contextmanager
    def _learning(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.root / LEARNING_DB, timeout=SQLITE_TIMEOUT_SECONDS, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")
        finally:
            db.close()

    @contextmanager
    def workspace_reader(self, key: str) -> Iterator[sqlite3.Connection | None]:
        path = self.workspaces.path(key) / WORKSPACE_DB
        if not path.is_file():
            yield None
            return
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=READ_TIMEOUT_SECONDS)) as db:
            yield db

    def close(self) -> None:
        return None


class PostgresControlBackend:
    """Control plane on PostgreSQL: a psycopg pool of admin sessions, SERIALIZABLE by default.

    Each checkout sets the search_path of the requested kind and opens the transaction in one
    round trip; the tam_db compatibility wrapper (one per session and kind, so its catalog cache
    stays per schema) runs the repositories' SQLite-dialect SQL unchanged."""

    def __init__(self, target: ActiveDatabase, master_key: bytes):
        from psycopg_pool import ConnectionPool

        from tam_db.pg_connection import configure_adapters
        from team_memory.pg_provision import PgWorkspaceProvisioner, session_options

        settings = target.settings
        self.settings = settings
        # WEB-origin DSNs carry connect options (empty passfile, no client certificates, password
        # auth only); they apply to every connection: this pool, provisioning, role sessions, readers.
        self.workspaces = PgWorkspaceProvisioner(target.url, target.instance_id, master_key, settings,
                                                 connect_overrides=target.connect_kwargs())
        password = self.workspaces.dsn.password
        self._secrets = (password.get_secret_value(),) if password is not None else ()
        self._wrappers: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
        self.pool = ConnectionPool(
            target.url, min_size=settings.pool_min_size, max_size=settings.pool_max_size, open=True,
            timeout=POOL_ACQUIRE_TIMEOUT_SECONDS, name=CONTROL_APPLICATION, configure=configure_adapters,
            kwargs={"autocommit": True, "connect_timeout": settings.connect_timeout_seconds,
                    "application_name": CONTROL_APPLICATION,
                    "options": " ".join((session_options(settings), SERIALIZABLE_OPTION, UTC_OPTION)),
                    **target.connect_kwargs()})

    @contextmanager
    def _session(self) -> Iterator[Any]:
        import psycopg
        from psycopg_pool import PoolTimeout

        from tam_db.pg_connection import map_error

        try:
            with self.pool.connection() as raw:
                yield raw
        except PoolTimeout as exc:
            LOGGER.error(json.dumps({"event": "control_plane_pool_timeout"}))
            raise Unavailable("The database is not reachable; try again shortly") from exc
        except psycopg.Error as exc:
            raise map_error(exc, self._secrets) from exc

    def _wrapper(self, raw, kind: ControlKind) -> CompatConnection:
        from tam_db.pg_connection import PgConnection

        per_kind = self._wrappers.setdefault(raw, {})
        if kind not in per_kind:
            per_kind[kind] = PgConnection(raw, secrets=self._secrets)
        return per_kind[kind]

    @contextmanager
    def connect(self, kind: ControlKind, write: bool) -> Iterator[CompatConnection]:
        from psycopg import sql

        path = sql.SQL(", ").join(sql.Identifier(name) for name in (CONTROL_SCHEMAS[kind], COMPAT_SCHEMA,
                                                                   EXTENSIONS_SCHEMA))
        with self._session() as raw:
            db = self._wrapper(raw, kind)
            raw.execute(sql.SQL("SET search_path TO {}; BEGIN").format(path))
            db.row_factory = sqlite3.Row
            try:
                yield db
            except BaseException:
                db.rollback()
                raise
            db.commit()

    @contextmanager
    def workspace_reader(self, key: str) -> Iterator[CompatConnection | None]:
        """Read-only session of one workspace as its own role (plan 1.3 gateway readers).

        A dedicated connection per reader: a company report opens one per department at once,
        which must not exhaust the control pool, and the role sees nothing but its schema."""
        from tam_db import pg_connection

        if not self.workspaces.exists(key):
            yield None
            return
        db = pg_connection.connect(self.workspaces.store_database(key))
        try:
            db.execute("SET default_transaction_read_only = on")
            yield db
        finally:
            db.close()

    def close(self) -> None:
        self.pool.close()


def _backend(root: Path, target: ActiveDatabase, master_key: Callable[[], bytes]) -> ControlBackend:
    if target.backend is Backend.POSTGRES:
        return PostgresControlBackend(target, master_key())
    return SqliteControlBackend(root, target.instance_id)


class SwitchableControlPlane:
    """ControlPlane (tam_db.contracts) over SQLite or PostgreSQL, switchable at run time."""

    def __init__(self, root: Path, target: ActiveDatabase, master_key: Callable[[], bytes]):
        self.root = root.resolve()
        self._master_key = master_key
        self._lock = ReadWriteLock()
        self._target = target
        self._backend = _backend(self.root, target, master_key)
        self._maintenance: Callable[[], bool] = _never
        # Closes the PostgreSQL pool when the plane is dropped without close() (CLI, tests).
        self._finalizer = weakref.finalize(self, self._backend.close)

    @classmethod
    def sqlite(cls, root: Path) -> "SwitchableControlPlane":
        root = root.resolve()
        instance_id = sqlite_instance_id(root, create=True)
        return cls(root, ActiveDatabase(backend=Backend.SQLITE, instance_id=instance_id, generation=0),
                   master_key=_no_master_key)

    def current(self) -> ActiveDatabase:
        return self._target

    def watch_maintenance(self, probe: Callable[[], bool]) -> None:
        """Install the process-wide maintenance probe (the migration gate); repositories that
        would write on a read (session touch) consult ``maintenance_active``."""
        self._maintenance = probe

    def maintenance_active(self) -> bool:
        return self._maintenance()

    @property
    def backend(self) -> Backend:
        return self._target.backend

    @property
    def settings(self) -> DatabaseSettings:
        return self._target.settings

    @contextmanager
    def connect(self, kind: ControlKind, *, write: bool = False) -> Iterator[CompatConnection]:
        """One transaction (commit on success, rollback on error). ``write`` asks SQLite for
        BEGIN IMMEDIATE; PostgreSQL transactions are always SERIALIZABLE."""
        with self._lock.read(), self._backend.connect(kind, write) as db:
            yield db

    def transaction(self, kind: ControlKind, work: Callable[[CompatConnection], T], *, write: bool = True) -> T:
        return retrying(self.settings.serializable_attempts, lambda: self._once(kind, work, write))

    def _once(self, kind: ControlKind, work: Callable[[CompatConnection], T], write: bool) -> T:
        with self.connect(kind, write=write) as db:
            return work(db)

    @contextmanager
    def workspace_reader(self, key: str) -> Iterator[CompatConnection | None]:
        """Read-only connection to one workspace's memory, or None when it holds nothing yet."""
        with self._lock.read(), self._backend.workspace_reader(key) as db:
            yield db

    @property
    def workspaces(self) -> WorkspaceProvisioner:
        return self._backend.workspaces

    def activate(self, target: ActiveDatabase) -> None:
        if target.instance_id != self._target.instance_id:
            raise Conflict("The new database belongs to another installation")
        backend = _backend(self.root, target, self._master_key)
        with self._lock.write():
            release, self._backend, self._target = self._finalizer, backend, target
            self._finalizer = weakref.finalize(self, backend.close)
        release()
        LOGGER.info(json.dumps({"event": "control_plane_activated", "backend": target.backend.value,
                                "generation": target.generation}))

    def close(self) -> None:
        with self._lock.write():
            self._finalizer()


def _never() -> bool:
    return False


def _no_master_key() -> bytes:
    raise Conflict("A SQLite control plane has no PostgreSQL roles to derive passwords for")


def retrying(attempts: int, call: Callable[[], T]) -> T:
    """Run ``call`` again after a serialization failure (SQLSTATE 40001/40P01), at most ``attempts`` times."""
    attempt = 1
    while True:
        try:
            return call()
        except PgSerializationFailure as exc:
            if attempt >= attempts:
                LOGGER.warning(json.dumps({"event": "serialization_retries_exhausted", "attempts": attempts}))
                raise Conflict("The change collided with a concurrent one; retry the request") from exc
        time.sleep(RETRY_BASE_SECONDS * attempt + random.uniform(0, RETRY_JITTER_SECONDS))
        attempt += 1


def serializable(method: Callable[..., T]) -> Callable[..., T]:
    """Retry a repository method whose body is exactly one control-plane transaction.

    Only for methods without effects outside that transaction: the whole method runs again.
    The instance exposes its plane as ``self.plane``.
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        return retrying(self.plane.settings.serializable_attempts, lambda: method(self, *args, **kwargs))

    return wrapper

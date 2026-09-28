"""Backend-neutral contracts shared by the SQLite and PostgreSQL team-server paths.

Standard library only: this module is imported by code that must keep working when the
``postgres`` extra is not installed, so nothing here may import psycopg.

Isolation model: one PostgreSQL database per organization, one schema (``schema_for``)
plus one LOGIN role (``role_for``) per workspace, control plane in ``tam_control`` and
``tam_learning``, SQLite compatibility functions in ``tam_compat``, extensions in
``extensions``.
"""

import hashlib
import re
import sqlite3
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from enum import StrEnum
from types import TracebackType
from typing import Any, Literal, Protocol, Self, TypeVar, runtime_checkable

WORKSPACE_SCHEMA_PREFIX = "ws_"
WORKSPACE_DIGEST_CHARS = 48
WORKSPACE_SCHEMA_PATTERN = re.compile(r"^ws_[0-9a-f]{48}$")
WORKSPACE_ROLE_PREFIX = "wsr_"
WORKSPACE_ROLE_PATTERN = re.compile(r"^wsr_[0-9a-f]{48}$")
MAX_WORKSPACE_KEY_CHARS = 128
MAX_IDENTIFIER_BYTES = 63

CONTROL_SCHEMA = "tam_control"
LEARNING_SCHEMA = "tam_learning"
COMPAT_SCHEMA = "tam_compat"
EXTENSIONS_SCHEMA = "extensions"
REQUIRED_EXTENSIONS = ("vector",)
MIN_SERVER_VERSION_NUM = 170000

STATEMENT_TIMEOUT_ENV = "TAM_TEAM_PG_STATEMENT_TIMEOUT_MS"
IDLE_IN_TRANSACTION_TIMEOUT_ENV = "TAM_TEAM_PG_IDLE_IN_TRANSACTION_TIMEOUT_MS"
LOCK_TIMEOUT_ENV = "TAM_TEAM_PG_LOCK_TIMEOUT_MS"
CONNECT_TIMEOUT_ENV = "TAM_TEAM_PG_CONNECT_TIMEOUT_SECONDS"
WORKSPACE_CONNECTION_LIMIT_ENV = "TAM_TEAM_PG_WORKSPACE_CONNECTION_LIMIT"
POOL_MIN_SIZE_ENV = "TAM_TEAM_PG_POOL_MIN_SIZE"
POOL_MAX_SIZE_ENV = "TAM_TEAM_PG_POOL_MAX_SIZE"
SERIALIZABLE_ATTEMPTS_ENV = "TAM_TEAM_PG_SERIALIZABLE_ATTEMPTS"

DEFAULT_STATEMENT_TIMEOUT_MS = 30_000
DEFAULT_IDLE_IN_TRANSACTION_TIMEOUT_MS = 60_000
DEFAULT_LOCK_TIMEOUT_MS = 10_000
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5
DEFAULT_WORKSPACE_CONNECTION_LIMIT = 4
DEFAULT_POOL_MIN_SIZE = 1
DEFAULT_POOL_MAX_SIZE = 8
DEFAULT_SERIALIZABLE_ATTEMPTS = 3

# Extra libpq keywords a target may carry (team_memory.database_contracts builds them with
# DatabaseDsn.connect_overrides for DSNs typed into the web UI). Nothing else is allowed:
# these are passed as keyword arguments to every psycopg.connect for the target.
CONNECT_OPTION_KEYS = frozenset({"passfile", "sslcertmode", "gssencmode", "require_auth"})
ConnectOptions = tuple[tuple[str, str], ...]

T = TypeVar("T")


class Backend(StrEnum):
    SQLITE = "sqlite"
    POSTGRES = "postgres"


class ControlKind(StrEnum):
    """Control-plane database: identity.db / tam_control and learning.db / tam_learning."""

    IDENTITY = "identity"
    LEARNING = "learning"


CONTROL_SCHEMAS: Mapping[ControlKind, str] = {
    ControlKind.IDENTITY: CONTROL_SCHEMA,
    ControlKind.LEARNING: LEARNING_SCHEMA,
}


def _validate_key(key: str) -> None:
    if not isinstance(key, str) or not key or len(key) > MAX_WORKSPACE_KEY_CHARS:
        raise ValueError(f"workspace key must be 1–{MAX_WORKSPACE_KEY_CHARS} characters")
    if not key.isprintable() or key != key.strip():
        raise ValueError("workspace key must be printable without surrounding whitespace")


def schema_for(key: str) -> str:
    """Schema (and LOGIN role) name of a workspace: ``ws_`` + 48 hex chars of sha256(key).

    Workspace keys (``personal_<sha256>`` is 73 chars) exceed PostgreSQL's 63-byte
    identifier limit, so they are hashed; ``tam_control.workspace_schemas`` keeps the
    reverse map.
    """
    _validate_key(key)
    return WORKSPACE_SCHEMA_PREFIX + hashlib.sha256(key.encode("utf-8")).hexdigest()[:WORKSPACE_DIGEST_CHARS]


def role_for(key: str, instance_id: str) -> str:
    """LOGIN role of a workspace: ``wsr_`` + 48 hex chars of sha256(instance_id NUL key).

    Schemas live inside the organization's database, but roles are global to the
    PostgreSQL cluster. Salting with the installation's instance_id keeps two TAM
    installations on one cluster (both have a "shared" workspace) from sharing a role.
    """
    _validate_key(key)
    canonical = str(uuid.UUID(instance_id))
    if canonical != instance_id:
        raise ValueError("instance_id must be a canonical lower-case UUID")
    material = f"{instance_id}\0{key}".encode()
    return WORKSPACE_ROLE_PREFIX + hashlib.sha256(material).hexdigest()[:WORKSPACE_DIGEST_CHARS]


def is_workspace_schema(name: str) -> bool:
    return bool(WORKSPACE_SCHEMA_PATTERN.fullmatch(name))


def is_workspace_role(name: str) -> bool:
    return bool(WORKSPACE_ROLE_PATTERN.fullmatch(name))


def _positive(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def as_connect_options(options: ConnectOptions | Mapping[str, str] | None) -> ConnectOptions:
    """Normalize libpq keyword overrides to the stored form: a sorted tuple of pairs.

    Stored as a tuple rather than a mapping because it must be immutable, hashable and
    picklable (MappingProxyType cannot be pickled to spawned workers).
    """
    if options is None:
        return ()
    if isinstance(options, Mapping):
        return tuple(sorted(options.items()))
    return options


def _validate_connect_options(options: ConnectOptions, backend: Backend) -> None:
    if not isinstance(options, tuple):
        raise TypeError("connect_options must be a tuple of (keyword, value) pairs")
    if options and backend is not Backend.POSTGRES:
        raise ValueError("connect_options apply to PostgreSQL targets only")
    seen: set[str] = set()
    for pair in options:
        if not (isinstance(pair, tuple) and len(pair) == 2 and all(isinstance(item, str) for item in pair)):
            raise TypeError("connect_options must be a tuple of (keyword, value) string pairs")
        key, value = pair
        if key not in CONNECT_OPTION_KEYS:
            allowed = ", ".join(sorted(CONNECT_OPTION_KEYS))
            raise ValueError(f"connect option {key!r} is not allowed; allowed: {allowed}")
        if key in seen:
            raise ValueError(f"connect option {key!r} is repeated")
        if not value or not value.isprintable():
            raise ValueError(f"connect option {key!r} needs a printable, non-empty value")
        seen.add(key)


@dataclass(frozen=True, slots=True)
class DatabaseSettings:
    """Session and pool limits applied to every PostgreSQL connection TAM opens."""

    statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS
    idle_in_transaction_timeout_ms: int = DEFAULT_IDLE_IN_TRANSACTION_TIMEOUT_MS
    lock_timeout_ms: int = DEFAULT_LOCK_TIMEOUT_MS
    connect_timeout_seconds: int = DEFAULT_CONNECT_TIMEOUT_SECONDS
    workspace_connection_limit: int = DEFAULT_WORKSPACE_CONNECTION_LIMIT
    pool_min_size: int = DEFAULT_POOL_MIN_SIZE
    pool_max_size: int = DEFAULT_POOL_MAX_SIZE
    serializable_attempts: int = DEFAULT_SERIALIZABLE_ATTEMPTS

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            _positive(name, getattr(self, name))
        if self.pool_min_size > self.pool_max_size:
            raise ValueError("pool_min_size must not exceed pool_max_size")

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> Self:
        names = {
            "statement_timeout_ms": STATEMENT_TIMEOUT_ENV,
            "idle_in_transaction_timeout_ms": IDLE_IN_TRANSACTION_TIMEOUT_ENV,
            "lock_timeout_ms": LOCK_TIMEOUT_ENV,
            "connect_timeout_seconds": CONNECT_TIMEOUT_ENV,
            "workspace_connection_limit": WORKSPACE_CONNECTION_LIMIT_ENV,
            "pool_min_size": POOL_MIN_SIZE_ENV,
            "pool_max_size": POOL_MAX_SIZE_ENV,
            "serializable_attempts": SERIALIZABLE_ATTEMPTS_ENV,
        }
        values: dict[str, int] = {}
        for attribute, variable in names.items():
            raw = environ.get(variable, "").strip()
            if not raw:
                continue
            try:
                values[attribute] = int(raw)
            except ValueError as exc:
                raise ValueError(f"{variable} must be a positive integer") from exc
        return cls(**values)


@dataclass(frozen=True, slots=True)
class WorkspaceTarget:
    """A provisioned workspace: its key, schema (``schema_for``) and LOGIN role (``role_for``)."""

    key: str
    schema: str
    role: str

    def __post_init__(self) -> None:
        if self.schema != schema_for(self.key):
            raise ValueError("workspace schema must equal schema_for(key)")
        if not is_workspace_role(self.role):
            raise ValueError("workspace role must come from role_for(key, instance_id)")

    @classmethod
    def for_key(cls, key: str, instance_id: str) -> Self:
        return cls(key=key, schema=schema_for(key), role=role_for(key, instance_id))


@dataclass(frozen=True, slots=True)
class StoreDatabase:
    """What a team worker's Store connects to.

    SQLite: ``url`` and ``schema`` are None and Store opens ``<workspace>/memory.db`` as
    today. PostgreSQL: ``url`` is a libpq URI that authenticates as the workspace role
    (it carries the role password, so it is excluded from repr) and ``schema`` is that
    workspace's schema. Picklable: WorkerPool passes it to spawned worker processes.
    """

    backend: Backend
    url: str | None = field(default=None, repr=False)
    schema: str | None = None
    settings: DatabaseSettings = field(default_factory=DatabaseSettings)
    connect_options: ConnectOptions = field(default=(), repr=False)

    def __post_init__(self) -> None:
        _validate_connect_options(self.connect_options, self.backend)
        if self.backend is Backend.SQLITE:
            if self.url is not None or self.schema is not None:
                raise ValueError("a SQLite store database takes no url or schema")
            return
        if self.backend is not Backend.POSTGRES:
            raise ValueError("backend must be a Backend member")
        if not self.url:
            raise ValueError("a PostgreSQL store database requires a url")
        if self.schema is None or not is_workspace_schema(self.schema):
            raise ValueError("a PostgreSQL store database requires a workspace schema")

    @classmethod
    def sqlite(cls) -> Self:
        return cls(backend=Backend.SQLITE)

    @classmethod
    def postgres(cls, url: str, schema: str, settings: DatabaseSettings | None = None,
                 connect_options: ConnectOptions | Mapping[str, str] | None = None) -> Self:
        return cls(backend=Backend.POSTGRES, url=url, schema=schema, settings=settings or DatabaseSettings(),
                   connect_options=as_connect_options(connect_options))

    def connect_kwargs(self) -> dict[str, str]:
        """Keyword arguments for every psycopg.connect made for this target."""
        return dict(self.connect_options)


@dataclass(frozen=True, slots=True)
class ActiveDatabase:
    """The control plane's current target, after the DSN has been decrypted.

    ``url`` is the full libpq URI for PostgreSQL (excluded from repr) and None for
    SQLite. ``instance_id`` is the installation's UUID (meta table of identity.db or
    ``tam_control.meta``); ``generation`` increases with every activation (0 = never
    configured, SQLite default). ``connect_options`` are extra libpq keywords for every
    connection to the target (non-empty for a DSN typed into the web UI).
    """

    backend: Backend
    instance_id: str
    generation: int
    url: str | None = field(default=None, repr=False)
    settings: DatabaseSettings = field(default_factory=DatabaseSettings)
    connect_options: ConnectOptions = field(default=(), repr=False)

    def __post_init__(self) -> None:
        _validate_connect_options(self.connect_options, self.backend)
        if (self.backend is Backend.POSTGRES) != bool(self.url):
            raise ValueError("url is required for PostgreSQL and forbidden for SQLite")
        if str(uuid.UUID(self.instance_id)) != self.instance_id:
            raise ValueError("instance_id must be a canonical lower-case UUID")
        if isinstance(self.generation, bool) or not isinstance(self.generation, int) or self.generation < 0:
            raise ValueError("generation must be a non-negative integer")

    def connect_kwargs(self) -> dict[str, str]:
        """Keyword arguments for every psycopg.connect made for this target."""
        return dict(self.connect_options)


# Errors. Each PostgreSQL error subclasses the sqlite3 exception the SQLite path raises
# in the same situation, so existing ``except sqlite3.IntegrityError`` / ``except
# sqlite3.OperationalError`` sites keep working unchanged on PostgreSQL.

class PgDatabaseError(sqlite3.DatabaseError):
    """Base of every error raised by the PostgreSQL compatibility layer.

    ``message`` must never contain a DSN or password; driver text is redacted before it
    reaches this class.
    """

    def __init__(self, message: str, *, sqlstate: str | None = None):
        super().__init__(message)
        self.sqlstate = sqlstate


class PgIntegrityError(PgDatabaseError, sqlite3.IntegrityError):
    """Constraint violation (SQLSTATE class 23) or a trigger's RAISE EXCEPTION (P0001).

    SQLite reports ``RAISE(ABORT, ...)`` in a trigger as IntegrityError, so the PL/pgSQL
    ports of those triggers must surface the same class.
    """


class PgOperationalError(PgDatabaseError, sqlite3.OperationalError):
    """Connection, syntax, missing object, lock and timeout errors."""


class PgSerializationFailure(PgOperationalError):
    """SQLSTATE 40001 / 40P01: the transaction may be retried from the start."""


class UntranslatableSQL(PgDatabaseError, sqlite3.NotSupportedError):
    """SQLite SQL the translator refuses on purpose (FTS5 MATCH, bm25(), FTS rowid, ...).

    Raised before execution so a call site that still needs an explicit PostgreSQL
    branch fails loudly in tests instead of returning wrong results.
    """

    def __init__(self, reason: str, sql: str):
        super().__init__(f"untranslatable SQL: {reason}")
        self.reason = reason
        self.sql = sql


SERIALIZATION_SQLSTATES = frozenset({"40001", "40P01"})
INTEGRITY_SQLSTATES = frozenset({"P0001"})
INTEGRITY_SQLSTATE_CLASSES = frozenset({"23"})
DATABASE_SQLSTATE_CLASSES = frozenset({"XX", "58"})


def error_class_for_sqlstate(sqlstate: str | None) -> type[PgDatabaseError]:
    """The compatibility exception class for a PostgreSQL SQLSTATE (single mapping table)."""
    if not sqlstate:
        return PgOperationalError
    if sqlstate in SERIALIZATION_SQLSTATES:
        return PgSerializationFailure
    if sqlstate in INTEGRITY_SQLSTATES or sqlstate[:2] in INTEGRITY_SQLSTATE_CLASSES:
        return PgIntegrityError
    if sqlstate[:2] in DATABASE_SQLSTATE_CLASSES:
        return PgDatabaseError
    return PgOperationalError


# Connection surface. These Protocols are exactly the part of sqlite3.Connection /
# sqlite3.Cursor that team-reachable code uses; sqlite3 objects satisfy them as-is and the
# psycopg-backed connection (tam_db.pg_connection) implements them.

SQLParameters = Sequence[object] | Mapping[str, object]
RowFactory = Callable[[Any, tuple[Any, ...]], Any]
ColumnDescription = tuple[str, None, None, None, None, None, None]


@runtime_checkable
class CompatRow(Protocol):
    """sqlite3.Row behaviour: tuple access by index, access by column name, keys()."""

    def keys(self) -> list[str]: ...

    def __getitem__(self, key: int | str, /) -> Any: ...

    def __len__(self) -> int: ...

    def __iter__(self) -> Iterator[Any]: ...


@runtime_checkable
class CompatCursor(Protocol):
    @property
    def lastrowid(self) -> int | None: ...

    @property
    def rowcount(self) -> int: ...

    @property
    def description(self) -> tuple[ColumnDescription, ...] | None: ...

    def execute(self, sql: str, parameters: SQLParameters = (), /) -> "CompatCursor": ...

    def executemany(self, sql: str, seq_of_parameters: Iterable[SQLParameters], /) -> "CompatCursor": ...

    def fetchone(self) -> Any: ...

    def fetchmany(self, size: int = 1) -> list[Any]: ...

    def fetchall(self) -> list[Any]: ...

    def close(self) -> None: ...

    def __iter__(self) -> Iterator[Any]: ...


@runtime_checkable
class CompatConnection(Protocol):
    """sqlite3 legacy-transaction semantics: DML opens a transaction implicitly,
    ``commit``/``rollback`` end it, ``with conn:`` commits on success and rolls back on
    error without closing. ``total_changes`` counts rows changed since connect."""

    row_factory: RowFactory | None

    @property
    def in_transaction(self) -> bool: ...

    @property
    def total_changes(self) -> int: ...

    def execute(self, sql: str, parameters: SQLParameters = (), /) -> CompatCursor: ...

    def executemany(self, sql: str, seq_of_parameters: Iterable[SQLParameters], /) -> CompatCursor: ...

    def executescript(self, sql_script: str, /) -> CompatCursor: ...

    def cursor(self) -> CompatCursor: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...

    def __enter__(self) -> Self: ...

    def __exit__(self, exc_type: type[BaseException] | None, exc_value: BaseException | None,
                 traceback: TracebackType | None, /) -> Literal[False] | None: ...


class WorkspaceProvisioner(Protocol):
    """Creates and removes workspace schemas and roles (PostgreSQL) or directories (SQLite)."""

    def ensure(self, key: str) -> WorkspaceTarget:
        """Idempotent: schema, role, grants and workspace migrations exist afterwards."""
        ...

    def exists(self, key: str) -> bool: ...

    def drop(self, key: str) -> None:
        """Idempotent: DROP SCHEMA ... CASCADE, DROP ROLE and the reverse-map row, in one transaction."""
        ...

    def store_database(self, key: str) -> StoreDatabase:
        """Connection target for a worker serving ``key``; call ``ensure`` first."""
        ...


class ControlPlane(Protocol):
    """Switchable control-plane access (identity and learning).

    ``connect`` and ``transaction`` hold a read lock for their duration; ``activate``
    takes the write lock, so no repository call straddles a backend switch.
    """

    def current(self) -> ActiveDatabase: ...

    def connect(self, kind: ControlKind) -> AbstractContextManager[CompatConnection]:
        """One transaction: commit on normal exit, rollback on error, connection released.

        On PostgreSQL the transaction is SERIALIZABLE; a conflict raises
        PgSerializationFailure without retrying (the block cannot be replayed).
        """
        ...

    def transaction(self, kind: ControlKind, work: Callable[[CompatConnection], T]) -> T:
        """Run ``work`` in one transaction, retrying it from the start on
        PgSerializationFailure up to ``DatabaseSettings.serializable_attempts`` times.
        ``work`` must have no side effects outside the database."""
        ...

    def activate(self, target: ActiveDatabase) -> None:
        """Switch every later ``connect``/``transaction`` to ``target``."""
        ...

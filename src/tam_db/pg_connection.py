"""A psycopg 3 connection with the sqlite3.Connection behaviour team-server code relies on.

The team server's Store, audit layer and control-plane repositories were written against
sqlite3. ``PgConnection`` gives them the same surface (``tam_db.contracts.CompatConnection``)
on PostgreSQL:

  - SQL is translated by ``tam_db.translate`` (placeholders, conflict clauses, types, ...);
    PRAGMA and ``SELECT last_insert_rowid()`` are answered locally (``tam_db.pragma``).
  - sqlite3 legacy transactions: the psycopg connection runs in autocommit mode and a
    ``BEGIN`` is issued before INSERT/UPDATE/DELETE/REPLACE when no transaction is open
    (unless ``isolation_level`` is None); ``commit``/``rollback``/``with conn:`` end it;
    explicit BEGIN/COMMIT/ROLLBACK/SAVEPOINT statements behave as in SQLite.
  - Statement-level atomicity: in SQLite a failed statement inside a transaction undoes
    only itself. PostgreSQL aborts the whole transaction, so every statement executed
    inside a transaction is wrapped in a savepoint, pipelined with the statement (one
    round trip). A serialization failure rolls the whole transaction back instead.
  - Rows: ``row_factory = sqlite3.Row`` yields ``PgRow`` (tuple + access by name +
    ``keys()``); other factories are called with (cursor, tuple) as in sqlite3.
  - ``lastrowid`` via an automatic ``RETURNING <integer primary key>``;
    ``total_changes`` sums DML row counts; ``rowcount`` is -1 for queries.
  - Values: numeric -> int/float, boolean -> 0/1, json/jsonb -> str (as SQLite returns
    them); Python bool is sent as an integer, datetime/date as SQLite's default text.
  - Errors: psycopg errors become the ``tam_db.contracts`` classes (subclasses of the
    sqlite3 ones) with the password and any connection URI redacted.

``execute_native`` runs PostgreSQL SQL (psycopg ``%s`` placeholders) untranslated with
the same transaction, row and error semantics, for explicit PostgreSQL branches.
"""

import json
import logging
import re
import sqlite3
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from datetime import date, datetime
from functools import lru_cache
from types import TracebackType
from typing import Any, Literal, Self

import psycopg
from psycopg import sql as pgsql
from psycopg.adapt import Dumper, Loader
from psycopg.conninfo import conninfo_to_dict
from psycopg.pq import TransactionStatus
from psycopg.types.numeric import IntDumper

from tam_db.contracts import (
    COMPAT_SCHEMA,
    EXTENSIONS_SCHEMA,
    SERIALIZATION_SQLSTATES,
    Backend,
    ColumnDescription,
    DatabaseSettings,
    PgDatabaseError,
    PgOperationalError,
    SQLParameters,
    StoreDatabase,
    error_class_for_sqlstate,
)
from tam_db.pragma import PgCatalog, execute_pragma
from tam_db.translate import StatementKind, Translation, split_statements, translate

LOGGER = logging.getLogger(__name__)

APPLICATION_NAME = "tam-team-server"
SESSION_TIME_ZONE = "UTC"
STATEMENT_SAVEPOINT = "tam_statement"
RENDERED_CACHE_SIZE = 1024
ROW_CLASS_CACHE_SIZE = 1024
LAST_INSERT_ROWID_COLUMN = "last_insert_rowid()"
ISOLATION_LEVELS = frozenset({"", "DEFERRED", "IMMEDIATE", "EXCLUSIVE"})
REDACTED = "[redacted]"
# Keywords open_raw sets itself; connect_options may not override them.
RESERVED_CONNECT_OPTIONS = frozenset({"autocommit", "connect_timeout", "application_name"})
_URI = re.compile(r"postgres(?:ql)?://\S+", re.IGNORECASE)
_PASSWORD_ASSIGNMENT = re.compile(r"password\s*=\s*\S+", re.IGNORECASE)
_ACTIVE_STATUSES = frozenset({TransactionStatus.INTRANS, TransactionStatus.INERROR})


# ── values ──

class _NumericLoader(Loader):
    """numeric -> int when integral text, else float (SQLite has no decimal type)."""

    def load(self, data: bytes | bytearray | memoryview) -> int | float:
        text = bytes(data).decode("ascii")
        if text in ("NaN", "Infinity", "-Infinity"):
            return float(text)
        if "." in text or "e" in text or "E" in text:
            return float(text)
        return int(text)


class _BoolLoader(Loader):
    def load(self, data: bytes | bytearray | memoryview) -> int:
        return 1 if bytes(data) == b"t" else 0


class _TextLoader(Loader):
    def load(self, data: bytes | bytearray | memoryview) -> str:
        return bytes(data).decode("utf-8")


class _SqliteTextDumper(Dumper):
    """datetime/date as sqlite3's default adapters wrote them, typed "unknown" like str."""

    oid = 0

    def dump(self, obj: date) -> bytes:
        text = obj.isoformat(" ") if isinstance(obj, datetime) else obj.isoformat()
        return text.encode("utf-8")


class TextParameter(str):
    """A str sent with type text (not "unknown"), for arguments PostgreSQL cannot type."""


class _NullText:
    """NULL sent with type text."""


NULL_TEXT = _NullText()


class _TextParameterDumper(Dumper):
    oid = psycopg.adapters.types["text"].oid

    def dump(self, obj: str) -> bytes:
        return obj.encode("utf-8")


class _NullTextDumper(Dumper):
    oid = psycopg.adapters.types["text"].oid

    def dump(self, obj: _NullText) -> None:
        return None


def typed_text(parameters: Sequence[object] | Mapping[str, object],
               positions: Sequence[int | str]) -> Sequence[object] | Mapping[str, object]:
    """Mark str / None parameters at ``positions`` as text-typed."""
    def convert(value: object) -> object:
        if value is None:
            return NULL_TEXT
        if isinstance(value, str):
            return TextParameter(value)
        return value

    if isinstance(parameters, Mapping):
        return {key: convert(value) if key in positions else value for key, value in parameters.items()}
    values = list(parameters)
    for position in positions:
        values[position] = convert(values[position])
    return tuple(values)


def configure_adapters(raw: psycopg.Connection) -> None:
    adapters = raw.adapters
    adapters.register_loader("numeric", _NumericLoader)
    adapters.register_loader("bool", _BoolLoader)
    for name in ("json", "jsonb"):
        adapters.register_loader(name, _TextLoader)
    adapters.register_dumper(bool, IntDumper)
    adapters.register_dumper(datetime, _SqliteTextDumper)
    adapters.register_dumper(date, _SqliteTextDumper)
    adapters.register_dumper(TextParameter, _TextParameterDumper)
    adapters.register_dumper(_NullText, _NullTextDumper)


def configure_session(raw: psycopg.Connection, schema: str, settings: DatabaseSettings) -> None:
    """Adapters plus session settings: search_path, time zone, timeouts (one round trip)."""
    configure_adapters(raw)
    statements = [
        pgsql.SQL("SET search_path TO {}, {}, {}").format(
            pgsql.Identifier(schema), pgsql.Identifier(COMPAT_SCHEMA), pgsql.Identifier(EXTENSIONS_SCHEMA)),
        pgsql.SQL("SET TimeZone TO {}").format(pgsql.Literal(SESSION_TIME_ZONE)),
        pgsql.SQL("SET statement_timeout TO {}").format(pgsql.Literal(settings.statement_timeout_ms)),
        pgsql.SQL("SET lock_timeout TO {}").format(pgsql.Literal(settings.lock_timeout_ms)),
        pgsql.SQL("SET idle_in_transaction_session_timeout TO {}").format(
            pgsql.Literal(settings.idle_in_transaction_timeout_ms)),
    ]
    raw.execute(pgsql.SQL("; ").join(statements))


# ── errors ──

def redact(text: str, secrets: Iterable[str] = ()) -> str:
    text = _URI.sub(REDACTED, text)
    text = _PASSWORD_ASSIGNMENT.sub("password=" + REDACTED, text)
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    return text


def map_error(error: psycopg.Error, secrets: Iterable[str] = ()) -> PgDatabaseError:
    sqlstate = getattr(error, "sqlstate", None)
    diag = getattr(error, "diag", None)
    primary = diag.message_primary if diag is not None and diag.message_primary else str(error)
    message = redact(primary.strip() or type(error).__name__, secrets)
    return error_class_for_sqlstate(sqlstate)(message, sqlstate=sqlstate)


# ── rows ──

@lru_cache(maxsize=ROW_CLASS_CACHE_SIZE)
def _row_class(keys: tuple[str, ...]) -> type["PgRow"]:
    index: dict[str, int] = {}
    for position, key in enumerate(keys):
        index.setdefault(key.lower(), position)
    return type("PgRow", (PgRow,), {"__slots__": (), "_keys": keys, "_index": index})


class PgRow(tuple):
    """sqlite3.Row equivalent: a tuple that also answers ``row["column"]`` and ``keys()``."""

    __slots__ = ()
    _keys: tuple[str, ...] = ()
    _index: Mapping[str, int] = {}

    @staticmethod
    def build(keys: tuple[str, ...], values: Sequence[Any]) -> "PgRow":
        return _row_class(keys)(values)

    def keys(self) -> list[str]:
        return list(self._keys)

    def __getitem__(self, key):
        if isinstance(key, str):
            position = self._index.get(key.lower())
            if position is None:
                raise IndexError("No item with that key")
            return tuple.__getitem__(self, position)
        return tuple.__getitem__(self, key)

    def __repr__(self) -> str:
        return f"<PgRow {dict(zip(self._keys, self, strict=True))!r}>"


# ── cursor ──

class PgCursor:
    """sqlite3.Cursor surface over results the connection has already fetched."""

    def __init__(self, connection: "PgConnection"):
        self.connection = connection
        self.row_factory = connection.row_factory
        self.arraysize = 1
        self._rows: list[tuple[Any, ...]] = []
        self._position = 0
        self._columns: tuple[str, ...] | None = None
        self._rowcount = -1
        self._lastrowid: int | None = None
        self._closed = False

    @property
    def lastrowid(self) -> int | None:
        return self._lastrowid

    @property
    def rowcount(self) -> int:
        return self._rowcount

    @property
    def description(self) -> tuple[ColumnDescription, ...] | None:
        if self._columns is None:
            return None
        return tuple((name, None, None, None, None, None, None) for name in self._columns)

    def execute(self, sql: str, parameters: SQLParameters = (), /) -> Self:
        self._check_open()
        self.connection._execute(self, sql, parameters)
        return self

    def executemany(self, sql: str, seq_of_parameters: Iterable[SQLParameters], /) -> Self:
        self._check_open()
        self.connection._executemany(self, sql, seq_of_parameters)
        return self

    def executescript(self, sql_script: str, /) -> Self:
        self._check_open()
        self.connection._executescript(self, sql_script)
        return self

    def fetchone(self) -> Any:
        if self._position >= len(self._rows):
            return None
        row = self._rows[self._position]
        self._position += 1
        return self._convert(row)

    def fetchmany(self, size: int | None = None) -> list[Any]:
        count = self.arraysize if size is None else size
        rows = self._rows[self._position:self._position + count]
        self._position += len(rows)
        return [self._convert(row) for row in rows]

    def fetchall(self) -> list[Any]:
        rows = self._rows[self._position:]
        self._position = len(self._rows)
        return [self._convert(row) for row in rows]

    def close(self) -> None:
        self._rows = []
        self._position = 0
        self._closed = True

    def __iter__(self) -> Iterator[Any]:
        return self

    def __next__(self) -> Any:
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row

    def _check_open(self) -> None:
        if self._closed:
            raise sqlite3.ProgrammingError("Cannot operate on a closed cursor.")
        self.connection._check_open()

    def _reset(self) -> None:
        self._rows = []
        self._position = 0
        self._columns = None
        self._rowcount = -1

    def _set_result(self, columns: tuple[str, ...] | None, rows: list[tuple[Any, ...]], rowcount: int) -> None:
        self._columns = columns
        self._rows = rows
        self._position = 0
        self._rowcount = rowcount

    def _convert(self, row: tuple[Any, ...]) -> Any:
        factory = self.row_factory
        if factory is None:
            return row
        if factory is sqlite3.Row or factory is PgRow:
            return PgRow.build(self._columns or (), row)
        return factory(self, row)


# ── connection ──

class PgConnection:
    """sqlite3.Connection semantics over one psycopg connection (see module docstring).

    ``raw`` must be a psycopg connection already configured with ``configure_session``;
    it is switched to autocommit. ``isolation_level`` follows sqlite3: "" (default),
    DEFERRED/IMMEDIATE/EXCLUSIVE open implicit transactions before DML, None disables them.
    ``statement_savepoints=False`` drops per-statement atomicity inside transactions (a
    failed statement then aborts the whole transaction, which is rolled back).
    """

    def __init__(self, raw: psycopg.Connection, *, isolation_level: str | None = "",
                 statement_savepoints: bool = True, secrets: Iterable[str] = ()):
        raw.autocommit = True
        self._raw = raw
        self.row_factory: Callable[[Any, tuple[Any, ...]], Any] | None = None
        self._isolation_level = _validated_isolation_level(isolation_level)
        self._statement_savepoints = statement_savepoints
        self._secrets = tuple(secret for secret in secrets if secret)
        self._total_changes = 0
        self._last_insert_rowid = 0
        self._catalog = PgCatalog(self._catalog_query)
        self._rendered: OrderedDict[str, tuple[str, str | None]] = OrderedDict()
        self._savepoint_transaction: str | None = None
        self._closed = False

    # sqlite3 attributes

    @property
    def raw(self) -> psycopg.Connection:
        return self._raw

    @property
    def backend(self) -> Backend:
        return Backend.POSTGRES

    @property
    def in_transaction(self) -> bool:
        return self._raw.info.transaction_status in _ACTIVE_STATUSES

    @property
    def total_changes(self) -> int:
        return self._total_changes

    @property
    def isolation_level(self) -> str | None:
        return self._isolation_level

    @isolation_level.setter
    def isolation_level(self, value: str | None) -> None:
        level = _validated_isolation_level(value)
        if level is None:
            self.commit()
        self._isolation_level = level

    @property
    def closed(self) -> bool:
        return self._closed

    # public API

    def cursor(self) -> PgCursor:
        self._check_open()
        return PgCursor(self)

    def execute(self, sql: str, parameters: SQLParameters = (), /) -> PgCursor:
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql: str, seq_of_parameters: Iterable[SQLParameters], /) -> PgCursor:
        return self.cursor().executemany(sql, seq_of_parameters)

    def executescript(self, sql_script: str, /) -> PgCursor:
        return self.cursor().executescript(sql_script)

    def execute_native(self, sql: str | pgsql.Composable, parameters: SQLParameters | None = None) -> PgCursor:
        """Run PostgreSQL SQL untranslated (psycopg placeholders), sqlite3 semantics otherwise.

        An implicit transaction is opened for INSERT/UPDATE/DELETE/MERGE as for translated
        DML; the statement is savepoint-wrapped inside a transaction.
        """
        cursor = self.cursor()
        text = sql if isinstance(sql, str) else sql.as_string(self._raw)
        dml = text.lstrip().split(None, 1)[0].upper() in ("INSERT", "UPDATE", "DELETE", "MERGE") if text.strip() \
            else False
        self._run(cursor, text, parameters, dml=dml, rowid=None, appended=False, many=False)
        return cursor

    def commit(self) -> None:
        self._check_open()
        if self.in_transaction:
            self._control("COMMIT")
        self._savepoint_transaction = None

    def rollback(self) -> None:
        self._check_open()
        if self.in_transaction:
            self._control("ROLLBACK")
        self._savepoint_transaction = None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Like sqlite3: an open transaction is discarded, not committed.
        self._raw.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc_value: BaseException | None,
                 traceback: TracebackType | None, /) -> Literal[False]:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False

    # execution

    def _check_open(self) -> None:
        if self._closed:
            raise sqlite3.ProgrammingError("Cannot operate on a closed database.")

    def _catalog_query(self, sql: str, parameters: Sequence[object]) -> list[tuple[Any, ...]]:
        try:
            return self._raw.execute(sql, parameters).fetchall()
        except psycopg.Error as error:
            raise map_error(error, self._secrets) from error

    def _control(self, statement: str) -> None:
        try:
            self._raw.execute(statement)
        except psycopg.Error as error:
            raise map_error(error, self._secrets) from error

    def _execute(self, cursor: PgCursor, sql: str, parameters: SQLParameters) -> None:
        cursor._reset()
        translation = translate(sql)
        if not self._local(cursor, translation):
            text, rowid = self._rendered_text(translation)
            self._run(cursor, text, self._bind(translation, parameters), dml=self._implicit(translation),
                      rowid=rowid, appended=not translation.has_returning, many=False)
            if translation.kind is StatementKind.DDL:
                self._invalidate_catalog()
        # sqlite3 copies the connection's last insert rowid after every execute().
        cursor._lastrowid = self._last_insert_rowid

    def _executemany(self, cursor: PgCursor, sql: str, seq_of_parameters: Iterable[SQLParameters]) -> None:
        cursor._reset()
        translation = translate(sql)
        if not translation.is_dml:
            raise sqlite3.ProgrammingError("executemany() can only execute DML statements.")
        text, rowid = self._rendered_text(translation)
        if rowid is not None and not translation.has_returning:
            text = translation.render(self._catalog) if translation.needs_catalog else translation.sql
        batch = [self._bind(translation, parameters) for parameters in seq_of_parameters]
        if not batch:
            cursor._set_result(None, [], 0)
            return
        self._run(cursor, text, batch, dml=self._implicit(translation), rowid=None, appended=False, many=True)

    def _executescript(self, cursor: PgCursor, script: str) -> None:
        cursor._reset()
        # sqlite3.executescript commits a pending transaction first and then runs the
        # script without implicit transactions.
        self.commit()
        for statement in split_statements(script):
            translation = translate(statement)
            if self._local(PgCursor(self), translation):
                continue
            text, rowid = self._rendered_text(translation)
            self._run(PgCursor(self), text, translation.bind(()), dml=False, rowid=rowid,
                      appended=not translation.has_returning, many=False)
            if translation.kind is StatementKind.DDL:
                self._invalidate_catalog()

    @staticmethod
    def _bind(translation: Translation, parameters: SQLParameters) -> Sequence[object] | Mapping[str, object]:
        bound = translation.bind(parameters)
        if translation.text_parameters:
            return typed_text(bound, translation.text_parameters)
        return bound

    def _implicit(self, translation: Translation) -> bool:
        return translation.is_dml and self._isolation_level is not None

    def _invalidate_catalog(self) -> None:
        self._catalog.invalidate()
        self._rendered.clear()

    def _rendered_text(self, translation: Translation) -> tuple[str, str | None]:
        """Final SQL plus the INSERT target's integer key (for lastrowid).

        An INSERT without its own RETURNING gets ``RETURNING <key>`` appended; the rows it
        returns are consumed by the connection, never shown to the caller.
        """
        cached = self._rendered.get(translation.source)
        if cached is not None:
            self._rendered.move_to_end(translation.source)
            return cached
        text = translation.render(self._catalog) if translation.needs_catalog else translation.sql
        rowid = None
        if translation.kind is StatementKind.INSERT and translation.insert_table:
            shape = self._catalog.table(translation.insert_table)
            if shape is not None and shape.rowid_column is not None:
                rowid = shape.rowid_column
                if not translation.has_returning:
                    text = f'{text} RETURNING "{rowid}"'
        if translation.needs_catalog or translation.kind is StatementKind.INSERT:
            self._rendered[translation.source] = (text, rowid)
            if len(self._rendered) > RENDERED_CACHE_SIZE:
                self._rendered.popitem(last=False)
        return text, rowid

    def _local(self, cursor: PgCursor, translation: Translation) -> bool:
        """Statements answered without (or with special handling of) a server round trip."""
        kind = translation.kind
        if kind is StatementKind.EMPTY:
            return True
        if kind is StatementKind.PRAGMA:
            result = execute_pragma(translation.pragma, self._catalog, translation.source)
            cursor._set_result(result.columns or None, list(result.rows), -1)
            return True
        if kind is StatementKind.LAST_INSERT_ROWID:
            cursor._set_result((LAST_INSERT_ROWID_COLUMN,), [(self._last_insert_rowid,)], -1)
            return True
        if kind is StatementKind.BEGIN:
            if self.in_transaction:
                raise PgOperationalError("cannot start a transaction within a transaction")
            self._control(translation.sql)
            return True
        if kind in (StatementKind.COMMIT, StatementKind.ROLLBACK):
            if not self.in_transaction:
                verb = "commit" if kind is StatementKind.COMMIT else "rollback"
                raise PgOperationalError(f"cannot {verb} - no transaction is active")
            self._control(kind.value.upper())
            self._savepoint_transaction = None
            return True
        if kind is StatementKind.SAVEPOINT:
            if not self.in_transaction:
                # SQLite: a SAVEPOINT outside a transaction starts one that its RELEASE commits.
                self._control("BEGIN")
                self._savepoint_transaction = _savepoint_name(translation.sql)
            self._control(translation.sql)
            return True
        if kind is StatementKind.RELEASE:
            self._control(translation.sql)
            if self._savepoint_transaction == _savepoint_name(translation.sql):
                self._control("COMMIT")
                self._savepoint_transaction = None
            return True
        if kind is StatementKind.ROLLBACK_TO:
            self._control(translation.sql)
            return True
        return False

    def _run(self, cursor: PgCursor, text: str, parameters: Any, *, dml: bool, rowid: str | None,
             appended: bool, many: bool) -> None:
        """Execute one statement (or a DML batch) with sqlite3 transaction semantics."""
        opened = False
        if dml and not self.in_transaction:
            opened = True
        wrap = self.in_transaction and self._statement_savepoints
        pg_cursor = self._raw.cursor()
        try:
            if opened or wrap:
                with self._raw.pipeline():
                    if opened:
                        self._raw.execute("BEGIN")
                    else:
                        self._raw.execute(f"SAVEPOINT {STATEMENT_SAVEPOINT}")
                    self._send(pg_cursor, text, parameters, many)
                    if wrap:
                        self._raw.execute(f"RELEASE SAVEPOINT {STATEMENT_SAVEPOINT}")
            else:
                self._send(pg_cursor, text, parameters, many)
        except psycopg.Error as error:
            mapped = map_error(error, self._secrets)
            self._recover(error, opened=opened, wrapped=wrap)
            raise mapped from error
        self._collect(cursor, pg_cursor, rowid=rowid, appended=appended, many=many)

    @staticmethod
    def _send(pg_cursor: psycopg.Cursor, text: str, parameters: Any, many: bool) -> None:
        if many:
            pg_cursor.executemany(text, parameters)
        else:
            pg_cursor.execute(text, parameters)

    def _recover(self, error: psycopg.Error, *, opened: bool, wrapped: bool) -> None:
        """Restore SQLite's state after a failed statement: only the statement is undone."""
        if self._raw.closed or self._raw.broken:
            return
        status = self._raw.info.transaction_status
        if status not in _ACTIVE_STATUSES:
            return
        serialization = getattr(error, "sqlstate", None) in SERIALIZATION_SQLSTATES
        try:
            if opened or serialization or not wrapped:
                self._raw.execute("ROLLBACK")
                self._savepoint_transaction = None
            else:
                self._raw.execute(f"ROLLBACK TO SAVEPOINT {STATEMENT_SAVEPOINT}; "
                                  f"RELEASE SAVEPOINT {STATEMENT_SAVEPOINT}")
        except psycopg.Error as recovery:
            LOGGER.warning(json.dumps({"event": "pg_statement_recovery_failed",
                                       "sqlstate": getattr(recovery, "sqlstate", None),
                                       "error": redact(str(recovery), self._secrets)}))
            raise map_error(recovery, self._secrets) from recovery

    def _collect(self, cursor: PgCursor, pg_cursor: psycopg.Cursor, *, rowid: str | None, appended: bool,
                 many: bool) -> None:
        rowcount = pg_cursor.rowcount
        if many:
            changed = max(rowcount, 0)
            self._total_changes += changed
            cursor._set_result(None, [], changed)
            return
        description = pg_cursor.description
        rows = pg_cursor.fetchall() if description is not None else []
        status = (pg_cursor.statusmessage or "").split(" ", 1)[0]
        is_change = status in ("INSERT", "UPDATE", "DELETE", "MERGE")
        if is_change:
            self._total_changes += max(rowcount, 0)
        columns = tuple(column.name for column in description) if description is not None else None
        if rowid is not None and rows and status == "INSERT" and columns is not None and rowid in columns:
            self._last_insert_rowid = rows[-1][columns.index(rowid)]
        if rowid is not None and appended:
            cursor._set_result(None, [], rowcount)
            return
        cursor._set_result(columns, rows, rowcount if is_change else -1)


def _validated_isolation_level(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value.upper() not in ISOLATION_LEVELS:
        raise ValueError("isolation_level must be None, '', DEFERRED, IMMEDIATE or EXCLUSIVE")
    return value.upper()


def _savepoint_name(statement: str) -> str:
    return statement.rsplit(None, 1)[-1].strip('"').lower()


# ── connecting ──

def _password(url: str) -> str:
    try:
        return conninfo_to_dict(url).get("password") or ""
    except psycopg.ProgrammingError:
        return ""


def _connection_options(connect_options: Mapping[str, str]) -> dict[str, str]:
    reserved = RESERVED_CONNECT_OPTIONS.intersection(connect_options)
    if reserved:
        raise ValueError(f"connect_options may not set {', '.join(sorted(reserved))}")
    return dict(connect_options)


def open_raw(url: str, *, schema: str, settings: DatabaseSettings, application_name: str = APPLICATION_NAME,
             connect_options: Mapping[str, str] | None = None) -> psycopg.Connection:
    """A configured autocommit psycopg connection (errors mapped and redacted).

    ``connect_options`` are extra libpq keywords (passfile, sslcertmode, require_auth, ...);
    their values never appear in error messages.
    """
    options = _connection_options(connect_options or {})
    secrets = (_password(url), *options.values())
    try:
        raw = psycopg.connect(url, autocommit=True, connect_timeout=settings.connect_timeout_seconds,
                              application_name=application_name, **options)
    except psycopg.Error as error:
        raise map_error(error, secrets) from None
    try:
        configure_session(raw, schema, settings)
    except psycopg.Error as error:
        raw.close()
        raise map_error(error, secrets) from None
    return raw


def connect_url(url: str, *, schema: str, settings: DatabaseSettings | None = None,
                factory: type[PgConnection] = PgConnection, isolation_level: str | None = "",
                statement_savepoints: bool = True, application_name: str = APPLICATION_NAME,
                connect_options: Mapping[str, str] | None = None) -> PgConnection:
    """Connect to ``url`` with ``schema`` first in the search_path (then tam_compat, extensions)."""
    settings = settings or DatabaseSettings()
    raw = open_raw(url, schema=schema, settings=settings, application_name=application_name,
                   connect_options=connect_options)
    return factory(raw, isolation_level=isolation_level, statement_savepoints=statement_savepoints,
                   secrets=(_password(url), *(connect_options or {}).values()))


def connect(database: StoreDatabase, *, factory: type[PgConnection] = PgConnection,
            isolation_level: str | None = "", statement_savepoints: bool = True) -> PgConnection:
    """Open the compatibility connection of a PostgreSQL ``StoreDatabase`` (a workspace)."""
    if database.backend is not Backend.POSTGRES or database.url is None or database.schema is None:
        raise ValueError("pg_connection.connect needs a PostgreSQL StoreDatabase")
    return connect_url(database.url, schema=database.schema, settings=database.settings, factory=factory,
                       isolation_level=isolation_level, statement_savepoints=statement_savepoints,
                       connect_options=database.connect_kwargs())

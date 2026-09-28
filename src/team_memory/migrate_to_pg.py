"""Copy the team server's SQLite databases into PostgreSQL and verify the copy (plan 5.3-5.4).

One source database (identity.db, learning.db or a workspace memory.db) is copied into one
PostgreSQL schema inside a single transaction: user triggers are disabled for the copy (their
effects - audit rows, change logs, guards - are already part of the copied data), rows go in
through COPY in SQLite column order, identity columns are advanced past the copied ids and past
SQLite's AUTOINCREMENT high-water mark, and derived vector columns are filled from the float32
BLOBs. Either the whole database arrives or nothing does.

Verification is a per-table row count plus a SHA-256 over canonical rows sorted by their primary
key, computed on both sides with the same value conversion, so it compares what PostgreSQL holds
with what the SQLite snapshot held. Derived vector columns are compared as float32 bytes.

This module imports psycopg and is loaded only on the PostgreSQL migration path.
"""

import hashlib
import json
import math
import re
import sqlite3
import struct
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import psycopg
from psycopg import sql

from tam_db.contracts import COMPAT_SCHEMA, EXTENSIONS_SCHEMA
from team_memory.contracts import Conflict
from team_memory.database_contracts import DatabaseEstimate, DatabaseKind, TableEstimate

IDENTITY_DB = "identity.db"
LEARNING_DB = "learning.db"
WORKSPACES_DIR = "workspaces"
MEMORY_DB = "memory.db"
WORKSPACE_KEY_PATTERN = re.compile(r"^(shared|(?:personal|team)_[a-f0-9]{64})$")
SQLITE_INTERNAL_PREFIX = "sqlite_"
FTS_SHADOW_SUFFIXES = ("_data", "_idx", "_content", "_docsize", "_config")
# SQLite-only bookkeeping, skipped when the PostgreSQL schema has no counterpart: the L2 embedding
# cache (disabled on PostgreSQL workers, plan 1.6) and the SQLite migration ledgers of memory.db and
# learning.db (PostgreSQL schemas keep their own ledgers).
SQLITE_ONLY_TABLES = frozenset({"embedding_cache", "migrations", "schema_migrations"})
# PostgreSQL-only columns filled from a SQLite column: (table, column) -> float32 BLOB source column.
VECTOR_COLUMNS: Mapping[tuple[str, str], str] = {("embeddings", "embedding"): "float32_vector"}
# Orphan rows (a declared SQLite foreign key without its parent) go to this workspace table instead of
# their own (lead decision); the control schemas have none, so orphans there block a plan.
QUARANTINE_TABLE = "migration_quarantine"
AUDIT_TABLES = frozenset({"tam_history", "tam_authorship"})
QUARANTINE_SAMPLE_PKS = 5
QUARANTINE_MAX_REASONS = 8
FETCH_BATCH_ROWS = 2000
SQLITE_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
FLOAT32_BYTES = 4
FLOAT32_DIGITS = 9
PROGRESS_EVERY_ROWS = 5000

INTEGER_TYPES = frozenset({"bigint", "integer", "smallint"})
FLOAT_TYPES = frozenset({"double precision"})
FLOAT32_TYPES = frozenset({"real"})
TEXT_TYPES = frozenset({"text", "json", "uuid", "name", "citext"})
TEXT_TYPE_PREFIXES = ("character varying", "character(")
NUMERIC_PREFIX = "numeric"
VECTOR_PREFIX = "vector"
JSONB_TYPE = "jsonb"
BYTEA_TYPE = "bytea"
BOOLEAN_TYPE = "boolean"


class MigrationError(Conflict):
    """The copy or its verification cannot proceed; the message names the database, table and column."""


class MigrationCancelled(Conflict):
    """Raised at the next checkpoint after a cancel request; the running transaction rolls back."""


class CopyObserver(Protocol):
    """Progress sink of a copy. ``checkpoint`` raises MigrationCancelled when a cancel was requested."""

    def table_started(self, database: str, table: str, rows: int) -> None: ...

    def rows_copied(self, rows: int) -> None: ...

    def checkpoint(self) -> None: ...


@dataclass(frozen=True, slots=True)
class SourceDatabase:
    kind: DatabaseKind
    name: str
    path: Path


@dataclass(frozen=True, slots=True)
class TableDigest:
    """``rows`` copied into the table (hashed) plus ``quarantined`` rows kept in migration_quarantine."""

    table: str
    rows: int
    sha256: str
    quarantined: int = 0


@dataclass(frozen=True, slots=True)
class DatabaseDigest:
    database: str
    tables: tuple[TableDigest, ...]

    def table(self, name: str) -> TableDigest | None:
        return next((table for table in self.tables if table.table == name), None)


@dataclass(frozen=True, slots=True)
class Mismatch:
    table: str
    reason: str


def discover_sources(root: Path) -> list[SourceDatabase]:
    """identity.db (required), learning.db (optional) and every workspace memory.db, in copy order."""
    root = root.resolve()
    identity = root / IDENTITY_DB
    if not identity.is_file():
        raise MigrationError("The server identity database does not exist")
    sources = [SourceDatabase(DatabaseKind.IDENTITY, "identity", identity)]
    learning = root / LEARNING_DB
    if learning.is_file():
        sources.append(SourceDatabase(DatabaseKind.LEARNING, "learning", learning))
    workspaces = root / WORKSPACES_DIR
    if workspaces.is_dir():
        for path in sorted(workspaces.glob(f"*/{MEMORY_DB}")):
            key = path.parent.name
            if not WORKSPACE_KEY_PATTERN.fullmatch(key):
                raise MigrationError(f"Unexpected workspace directory {key!r}")
            if path.is_symlink() or root not in path.resolve().parents:
                raise MigrationError("Workspace database paths must remain inside server data")
            sources.append(SourceDatabase(DatabaseKind.WORKSPACE, key, path))
    return sources


def open_source(path: Path) -> sqlite3.Connection:
    """Read-only connection holding one read transaction, so every table comes from one snapshot."""
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, isolation_level=None)
    connection.execute("BEGIN")
    return connection


def _quote_sqlite(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def source_tables(db: sqlite3.Connection) -> list[str]:
    """Ordinary tables: no sqlite_* internals, FTS5 virtual tables or their shadow tables."""
    rows = db.execute("SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    virtual = {name for name, ddl in rows if (ddl or "").lstrip().upper().startswith("CREATE VIRTUAL TABLE")}
    shadow = {name + suffix for name in virtual for suffix in FTS_SHADOW_SUFFIXES}
    return [name for name, _ in rows
            if not name.startswith(SQLITE_INTERNAL_PREFIX) and name not in virtual and name not in shadow]


def source_columns(db: sqlite3.Connection, table: str) -> list[tuple[str, int]]:
    """(column, primary-key position) in declaration order; position 0 means not in the key."""
    return [(row[1], row[5]) for row in db.execute(f"PRAGMA table_info({_quote_sqlite(table)})")]


def autoincrement_floor(db: sqlite3.Connection) -> dict[str, int]:
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'").fetchone()
    if exists is None:
        return {}
    return {name: int(seq) for name, seq in db.execute("SELECT name, seq FROM sqlite_sequence") if seq is not None}


def foreign_key_violations(path: Path) -> list[str]:
    """Tables whose rows break a declared foreign key; PostgreSQL would reject them, so they block a plan."""
    with closing(open_source(path)) as db:
        return sorted({row[0] for row in db.execute("PRAGMA foreign_key_check")})


def bundled_sqlite_migrations(directory: Path = SQLITE_MIGRATIONS_DIR) -> frozenset[str]:
    """Versions of the SQLite store migrations this build ships ("001" for 001_v5_schema.sql)."""
    return frozenset(path.stem.split("_", 1)[0] for path in directory.glob("*.sql"))


def unknown_migrations(source: SourceDatabase, known: frozenset[str]) -> list[str]:
    """SQLite migration versions of ``source`` this build does not know: the file comes from a newer TAM."""
    with closing(open_source(source.path)) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='migrations'").fetchone() is None:
            return []
        return sorted({str(row[0]) for row in db.execute("SELECT version FROM migrations")} - known)


def estimate(source: SourceDatabase) -> DatabaseEstimate:
    """Rows and on-disk bytes per table (dbstat pages; the file size split by rows without it)."""
    with closing(open_source(source.path)) as db:
        tables = source_tables(db)
        counts = {table: db.execute(f"SELECT COUNT(*) FROM {_quote_sqlite(table)}").fetchone()[0] for table in tables}
        try:
            pages = dict(db.execute("SELECT name, SUM(pgsize) FROM dbstat GROUP BY name").fetchall())
        except sqlite3.OperationalError:
            pages = {}
    if pages:
        sizes = {table: int(pages.get(table) or 0) for table in tables}
    else:
        total_rows = sum(counts.values()) or 1
        file_bytes = source.path.stat().st_size
        sizes = {table: file_bytes * counts[table] // total_rows for table in tables}
    return DatabaseEstimate(kind=source.kind, name=source.name,
                            tables=tuple(TableEstimate(name=t, rows=counts[t], bytes=sizes[t]) for t in tables))


# Orphans. SQLite workspaces run with foreign_keys off, so a row may reference a parent that is
# gone. Such rows are found on the SQLite side before the copy, to a fixpoint (a row whose parent is
# itself an orphan is an orphan too), and quarantined instead of copied.

RowIdentity = tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class ForeignKey:
    child: str
    columns: tuple[str, ...]
    parent: str
    parent_columns: tuple[str, ...]

    @property
    def reason(self) -> str:
        return (f"fk:{self.child}.{','.join(self.columns)}->"
                f"{self.parent}.{','.join(self.parent_columns) or '?'}")


@dataclass(frozen=True, slots=True)
class QuarantineSummary:
    database: str
    table: str
    rows: int
    reasons: tuple[str, ...]
    sample_pks: tuple[str, ...]

    @property
    def audit(self) -> bool:
        return self.table in AUDIT_TABLES


def _primary_key(db: sqlite3.Connection, table: str) -> list[str]:
    return [name for name, position in sorted((c for c in source_columns(db, table) if c[1]), key=lambda c: c[1])]


def _identity_columns(db: sqlite3.Connection, table: str) -> list[str]:
    """How a row is identified while it is copied: its primary key, else its rowid."""
    return _primary_key(db, table) or ["rowid"]


def _identity_select(columns: Sequence[str]) -> str:
    return ", ".join("rowid" if column == "rowid" else _quote_sqlite(column) for column in columns)


def foreign_keys(db: sqlite3.Connection, tables: Sequence[str]) -> list[ForeignKey]:
    keys = []
    for table in tables:
        groups: dict[int, list[tuple]] = {}
        for row in db.execute(f"PRAGMA foreign_key_list({_quote_sqlite(table)})"):
            groups.setdefault(row[0], []).append(row)
        for rows in groups.values():
            rows.sort(key=lambda row: row[1])
            parent = rows[0][2]
            referenced = tuple(row[4] for row in rows if row[4] is not None)
            if not referenced and parent in tables:
                referenced = tuple(_primary_key(db, parent))
            keys.append(ForeignKey(child=table, columns=tuple(row[3] for row in rows), parent=parent,
                                   parent_columns=referenced))
    return keys


def find_orphans(db: sqlite3.Connection) -> dict[str, dict[RowIdentity, str]]:
    """table -> {row identity: reason} for every row whose declared foreign key has no live parent."""
    tables = source_tables(db)
    keys = foreign_keys(db, tables)
    orphans: dict[str, dict[RowIdentity, str]] = {}
    changed = True
    while changed:
        changed = False
        for key in keys:
            found = orphans.setdefault(key.child, {})
            identity = _identity_columns(db, key.child)
            if key.parent in tables and len(key.parent_columns) == len(key.columns):
                parent_identity = _identity_columns(db, key.parent)
                dead = orphans.get(key.parent, {})
                live = {tuple(row[len(parent_identity):]) for row in db.execute(
                    f"SELECT {_identity_select(parent_identity)}, {_identity_select(key.parent_columns)} "
                    f"FROM {_quote_sqlite(key.parent)}") if tuple(row[:len(parent_identity)]) not in dead}
            else:
                live = set()
            for row in db.execute(f"SELECT {_identity_select(identity)}, {_identity_select(key.columns)} "
                                  f"FROM {_quote_sqlite(key.child)}"):
                ident, values = tuple(row[:len(identity)]), tuple(row[len(identity):])
                if ident in found or any(value is None for value in values) or values in live:
                    continue
                found[ident] = key.reason
                changed = True
    return {table: rows for table, rows in orphans.items() if rows}


def _jsonable(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"hex": value.hex()}
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    return value


def _pk_text(identity_columns: Sequence[str], identity: RowIdentity) -> str:
    return json.dumps({column: _jsonable(value) for column, value in zip(identity_columns, identity, strict=True)},
                      sort_keys=True, ensure_ascii=False)


def quarantine_summary(source: SourceDatabase) -> list[QuarantineSummary]:
    """What a copy of ``source`` would quarantine: per table the count, reasons and sample primary keys."""
    with closing(open_source(source.path)) as db:
        orphans = find_orphans(db)
        summaries = []
        for table in sorted(orphans):
            identity = _identity_columns(db, table)
            rows = orphans[table]
            samples = sorted(_pk_text(identity, ident) for ident in rows)[:QUARANTINE_SAMPLE_PKS]
            reasons = tuple(sorted(set(rows.values())))[:QUARANTINE_MAX_REASONS]
            summaries.append(QuarantineSummary(database=source.name, table=table, rows=len(rows), reasons=reasons,
                                               sample_pks=tuple(samples)))
    return summaries


def text_problems(source: SourceDatabase, limit: int = QUARANTINE_SAMPLE_PKS) -> list[str]:
    """Values the copy would refuse whatever the PostgreSQL column type (dry-run blockers): text with
    NUL characters, and non-UTF-8 BLOBs stored in a column declared as text."""
    problems: list[str] = []
    with closing(open_source(source.path)) as db:
        for table in source_tables(db):
            info = db.execute(f"PRAGMA table_info({_quote_sqlite(table)})").fetchall()
            columns = [row[1] for row in info]
            textual = {row[1] for row in info if "BLOB" not in (row[2] or "").upper()}
            identity = _identity_columns(db, table)
            for ident, row in _rows(db, table, columns, identity):
                for column, value in zip(columns, row, strict=True):
                    reason = None
                    if isinstance(value, str) and "\x00" in value:
                        reason = "text contains NUL characters, which PostgreSQL cannot store"
                    elif isinstance(value, bytes) and column in textual:
                        try:
                            value.decode("utf-8")
                        except UnicodeDecodeError:
                            reason = "a BLOB in a text column is not UTF-8"
                    if reason:
                        problems.append(f"{source.name}: {table}.{column} row {_pk_text(identity, ident)}: {reason}")
                        if len(problems) >= limit:
                            return problems
    return problems


# Value conversion. A codec turns a SQLite value into what PostgreSQL stores for a column type and
# gives both sides the same canonical bytes for hashing.

def _canonical(value: Any) -> bytes:
    if value is None:
        return b"N"
    if isinstance(value, bool):
        return b"b1" if value else b"b0"
    if isinstance(value, int):
        return b"I" + str(value).encode()
    if isinstance(value, float):
        return b"F" + value.hex().encode()
    if isinstance(value, Decimal):
        return b"D" + str(value.normalize()).encode()
    if isinstance(value, str):
        return b"T" + value.encode("utf-8")
    if isinstance(value, bytes | bytearray | memoryview):
        return b"B" + bytes(value)
    raise TypeError(f"no canonical form for {type(value).__name__}")


class ConversionError(ValueError):
    pass


def _to_int(value: Any) -> int | None:
    if value is None or isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        raise ConversionError("a fractional number cannot be stored in an integer column")
    if isinstance(value, str) and re.fullmatch(r"\s*[+-]?\d+\s*", value):
        return int(value)
    raise ConversionError(f"a {type(value).__name__} value cannot be stored in an integer column")


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError as exc:
            raise ConversionError("text that is not a number cannot be stored in a float column") from exc
    raise ConversionError(f"a {type(value).__name__} value cannot be stored in a float column")


def _to_float32(value: Any) -> float | None:
    number = _to_float(value)
    return None if number is None else struct.unpack("<f", struct.pack("<f", number))[0]


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ConversionError("the value is not a number") from exc


def _to_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ConversionError("a BLOB that is not UTF-8 cannot be stored in a text column") from exc
    elif not isinstance(value, str):
        value = str(value)
    if "\x00" in value:
        raise ConversionError("PostgreSQL text cannot contain NUL characters")
    return value


def _to_bytes(value: Any) -> bytes | None:
    if value is None or isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    if isinstance(value, int | float):
        return str(value).encode("ascii")
    raise ConversionError(f"a {type(value).__name__} value cannot be stored in a bytea column")


def _to_bool(value: Any) -> bool | None:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise ConversionError("only 0 and 1 can be stored in a boolean column")


def _to_jsonb(value: Any) -> str | None:
    text = _to_text(value)
    if text is None:
        return None
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise ConversionError("the value is not valid JSON") from exc
    if _json_has_nul(parsed):
        raise ConversionError("PostgreSQL jsonb cannot contain \\u0000 (NUL) characters")
    return text


def _json_has_nul(value: Any) -> bool:
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(_json_has_nul(key) or _json_has_nul(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_json_has_nul(item) for item in value)
    return False


def _canonical_json(value: Any) -> Any:
    if value is None:
        return None
    parsed = json.loads(value) if isinstance(value, str) else value
    return json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def float32_blob(value: Any) -> np.ndarray:
    if not isinstance(value, bytes) or not value or len(value) % FLOAT32_BYTES:
        raise ConversionError("the float32 vector BLOB is empty or not a multiple of 4 bytes")
    vector = np.frombuffer(value, dtype=np.float32)
    if not np.isfinite(vector).all():
        raise ConversionError("the float32 vector holds NaN or infinity, which pgvector rejects")
    return vector


def vector_literal(vector: np.ndarray) -> str:
    return "[" + ",".join(format(float(x), f".{FLOAT32_DIGITS}g") for x in vector) + "]"


def parse_vector(text: str) -> bytes:
    body = text.strip()
    if not (body.startswith("[") and body.endswith("]")):
        raise ConversionError("unexpected vector text form")
    return np.array([float(item) for item in body[1:-1].split(",")], dtype=np.float32).tobytes()


def _unchanged(value: Any) -> Any:
    return value


@dataclass(frozen=True, slots=True)
class Codec:
    to_target: Callable[[Any], Any]
    canonical_target: Callable[[Any], Any] = _unchanged
    select_cast: str | None = None


CODECS: Mapping[str, Codec] = {
    "integer": Codec(_to_int),
    "float": Codec(_to_float),
    "float32": Codec(_to_float32),
    "numeric": Codec(_to_decimal),
    "text": Codec(_to_text),
    "jsonb": Codec(_to_jsonb, _canonical_json, "text"),
    "bytea": Codec(_to_bytes, lambda value: None if value is None else bytes(value)),
    "boolean": Codec(_to_bool),
}


def codec_for(pg_type: str) -> Codec:
    if pg_type in INTEGER_TYPES:
        return CODECS["integer"]
    if pg_type in FLOAT_TYPES:
        return CODECS["float"]
    if pg_type in FLOAT32_TYPES:
        return CODECS["float32"]
    if pg_type.startswith(NUMERIC_PREFIX):
        return CODECS["numeric"]
    if pg_type in TEXT_TYPES or pg_type.startswith(TEXT_TYPE_PREFIXES):
        return CODECS["text"]
    if pg_type == JSONB_TYPE:
        return CODECS["jsonb"]
    if pg_type == BYTEA_TYPE:
        return CODECS["bytea"]
    if pg_type == BOOLEAN_TYPE:
        return CODECS["boolean"]
    raise MigrationError(f"column type {pg_type!r} has no SQLite conversion")


# Target catalog.

@dataclass(frozen=True, slots=True)
class TargetColumn:
    name: str
    pg_type: str
    identity: bool
    generated: bool
    serial: bool


@dataclass(frozen=True, slots=True)
class ColumnPlan:
    """One PostgreSQL column the copy writes: from a SQLite column, or a vector derived from a BLOB column."""

    name: str
    source: str
    pg_type: str
    vector: bool = False

    def transfer(self, value: Any) -> tuple[Any, bytes]:
        """The value to write and its canonical form, from one SQLite value."""
        if self.vector:
            if value is None:
                return None, _canonical(None)
            vector = float32_blob(value)
            return vector_literal(vector), _canonical(vector.tobytes())
        codec = codec_for(self.pg_type)
        stored = codec.to_target(value)
        return stored, _canonical(codec.canonical_target(stored))

    def canonical_source(self, value: Any) -> bytes:
        return self.transfer(value)[1]

    def canonical_target(self, value: Any) -> bytes:
        if self.vector:
            return _canonical(None if value is None else parse_vector(value))
        return _canonical(codec_for(self.pg_type).canonical_target(value))

    def select(self) -> sql.Composable:
        if self.vector:
            return sql.SQL("{}::text").format(sql.Identifier(self.name))
        cast = codec_for(self.pg_type).select_cast
        if cast is None:
            return sql.Identifier(self.name)
        return sql.SQL("{}::{}").format(sql.Identifier(self.name), sql.SQL(cast))


@dataclass(frozen=True, slots=True)
class TablePlan:
    name: str
    columns: tuple[ColumnPlan, ...]
    key: tuple[int, ...]
    sequences: tuple[str, ...] = ()
    always_identity: bool = False

    @property
    def source_columns(self) -> tuple[str, ...]:
        return tuple(column.source for column in self.columns)


def target_tables(target: psycopg.Connection, schema: str) -> dict[str, list[TargetColumn]]:
    rows = target.execute("""
        SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attidentity, a.attgenerated,
               coalesce(pg_get_expr(d.adbin, d.adrelid), '') LIKE 'nextval(%%'
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
        LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum
        WHERE n.nspname = %s AND c.relkind IN ('r', 'p')
        ORDER BY c.relname, a.attnum""", (schema,)).fetchall()
    tables: dict[str, list[TargetColumn]] = {}
    for table, column, pg_type, identity, generated, serial in rows:
        # format_type qualifies types outside search_path (extensions.vector); the schema is irrelevant here.
        tables.setdefault(table, []).append(TargetColumn(
            name=column, pg_type=pg_type.rpartition(".")[2], identity=identity in ("a", "d"), generated=bool(generated),
            serial=bool(serial)))
    return tables


def always_identity_tables(target: psycopg.Connection, schema: str) -> set[str]:
    rows = target.execute("""
        SELECT DISTINCT c.relname FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0
        WHERE n.nspname = %s AND a.attidentity = 'a'""", (schema,)).fetchall()
    return {row[0] for row in rows}


def plan_table(database: str, table: str, source: Sequence[tuple[str, int]], target: Sequence[TargetColumn],
               always_identity: bool) -> TablePlan:
    source_names = [name for name, _ in source]
    by_name = {column.name: column for column in target}
    missing = [name for name in source_names if name not in by_name]
    if missing:
        raise MigrationError(f"{database}: PostgreSQL table {table} lacks columns {', '.join(missing)}")
    generated = [name for name in source_names if by_name[name].generated]
    if generated:
        raise MigrationError(f"{database}: PostgreSQL column {table}.{generated[0]} is generated but holds "
                             "SQLite data")
    columns = [ColumnPlan(name=name, source=name, pg_type=by_name[name].pg_type) for name in source_names]
    for column in target:
        derived = VECTOR_COLUMNS.get((table, column.name))
        if column.name in source_names or column.generated:
            continue
        if derived is None:
            # A PostgreSQL-only column nobody fills would silently take its default for every copied row.
            raise MigrationError(f"{database}: PostgreSQL column {table}.{column.name} has no SQLite source")
        if derived not in source_names:
            raise MigrationError(f"{database}: {table}.{column.name} is derived from missing column {derived}")
        if not column.pg_type.startswith(VECTOR_PREFIX):
            raise MigrationError(f"{database}: {table}.{column.name} must be a vector column")
        columns.append(ColumnPlan(name=column.name, source=derived, pg_type=column.pg_type, vector=True))
    for plan in columns:
        if not plan.vector:
            codec_for(plan.pg_type)
    ordered_key = [name for name, position in sorted((c for c in source if c[1]), key=lambda c: c[1])]
    key = tuple(source_names.index(name) for name in ordered_key)
    sequences = tuple(c.name for c in target if (c.identity or c.serial) and c.name in source_names)
    return TablePlan(name=table, columns=tuple(columns), key=key, sequences=sequences,
                     always_identity=always_identity)


def _foreign_key_order(target: psycopg.Connection, schema: str, tables: Sequence[str]) -> list[str]:
    """Parents before children, following PostgreSQL's foreign keys between the copied tables."""
    edges = target.execute("""
        SELECT child.relname, parent.relname FROM pg_constraint k
        JOIN pg_class child ON child.oid = k.conrelid
        JOIN pg_class parent ON parent.oid = k.confrelid
        JOIN pg_namespace n ON n.oid = child.relnamespace
        WHERE k.contype = 'f' AND n.nspname = %s AND child.oid <> parent.oid""", (schema,)).fetchall()
    wanted = set(tables)
    parents: dict[str, set[str]] = {table: set() for table in tables}
    for child, parent in edges:
        if child in wanted and parent in wanted:
            parents[child].add(parent)
    ordered: list[str] = []
    remaining = dict(parents)
    while remaining:
        ready = sorted(table for table, needs in remaining.items() if not needs - set(ordered))
        if not ready:
            # A foreign-key cycle: copy the rest alphabetically and rely on deferrable constraints.
            ready = sorted(remaining)
        for table in ready:
            ordered.append(table)
            remaining.pop(table)
    return ordered


def _rows(db: sqlite3.Connection, table: str, columns: Sequence[str],
          identity: Sequence[str] = ()) -> Iterator[tuple]:
    """Rows of ``columns``; with ``identity`` each row is (identity tuple, row)."""
    selected = ", ".join(_quote_sqlite(column) for column in columns)
    if identity:
        selected = _identity_select(identity) + ", " + selected
    cursor = db.execute(f"SELECT {selected} FROM {_quote_sqlite(table)}")
    width = len(identity)
    while batch := cursor.fetchmany(FETCH_BATCH_ROWS):
        if width:
            yield from ((tuple(row[:width]), row[width:]) for row in batch)
        else:
            yield from batch


def _kept_rows(db: sqlite3.Connection, plan: TablePlan, orphans: Mapping[RowIdentity, str]) -> Iterator[tuple]:
    """The table's rows without its orphans."""
    if not orphans:
        yield from _rows(db, plan.name, plan.source_columns)
        return
    for ident, row in _rows(db, plan.name, plan.source_columns, _identity_columns(db, plan.name)):
        if ident not in orphans:
            yield row


class _DigestBuilder:
    """Rows keyed by their canonical primary key (the whole row without one), hashed in key order."""

    def __init__(self, key: tuple[int, ...]):
        self.key = key
        self.entries: list[tuple[bytes, bytes]] = []

    @staticmethod
    def _frame(parts: Iterable[bytes]) -> bytes:
        return b"".join(len(part).to_bytes(8, "big") + part for part in parts)

    @classmethod
    def order(cls, key: tuple[int, ...], canonical: Sequence[bytes]) -> bytes:
        if key:
            return cls._frame(canonical[i] for i in key)
        return hashlib.sha256(cls._frame(canonical)).digest()

    def add(self, canonical: Sequence[bytes]) -> None:
        row = hashlib.sha256(self._frame(canonical)).digest()
        self.entries.append((self.order(self.key, canonical) if self.key else row, row))

    def finish(self, table: str) -> TableDigest:
        self.entries.sort()
        total = hashlib.sha256()
        for _, row in self.entries:
            total.update(row)
        return TableDigest(table=table, rows=len(self.entries), sha256=total.hexdigest())


def _qualified(schema: str, table: str) -> sql.Composable:
    return sql.Identifier(schema, table)


def _table_plans(db: sqlite3.Connection, target: psycopg.Connection, schema: str,
                 database: str, ledgers: frozenset[str] = frozenset()) -> list[TablePlan]:
    catalog = target_tables(target, schema)
    always = always_identity_tables(target, schema)
    plans = []
    for table in source_tables(db):
        if table in ledgers:
            continue
        if table not in catalog:
            if table in SQLITE_ONLY_TABLES:
                continue
            raise MigrationError(f"{database}: PostgreSQL schema has no table {table}")
        plans.append(plan_table(database, table, source_columns(db, table), catalog[table], table in always))
    order = _foreign_key_order(target, schema, [plan.name for plan in plans])
    by_name = {plan.name: plan for plan in plans}
    return [by_name[name] for name in order]


def _row_key(plan: TablePlan, row: Sequence[Any]) -> str:
    """The primary key of a source row as JSON text, for error messages."""
    if not plan.key:
        return "(no primary key)"
    return json.dumps({plan.columns[index].name: _jsonable(row[index]) for index in plan.key},
                      sort_keys=True, ensure_ascii=False)


def _upsert(database: str, plan: TablePlan) -> sql.Composable:
    """ON CONFLICT clause of a seeded table: the SQLite row replaces a seeded row with the same key."""
    if not plan.key:
        raise MigrationError(f"{database}: seeded table {plan.name} needs a primary key")
    keys = [plan.columns[index].name for index in plan.key]
    others = [column.name for column in plan.columns if column.name not in keys]
    action = sql.SQL("DO NOTHING") if not others else sql.SQL("DO UPDATE SET {}").format(sql.SQL(", ").join(
        sql.SQL("{} = EXCLUDED.{}").format(sql.Identifier(name), sql.Identifier(name)) for name in others))
    return sql.SQL("ON CONFLICT ({}) {}").format(sql.SQL(", ").join(sql.Identifier(name) for name in keys), action)


def _quarantine(db: sqlite3.Connection, target: psycopg.Connection, schema: str, database: str, table: str,
                orphans: Mapping[RowIdentity, str], moment: str) -> None:
    identity = _identity_columns(db, table)
    columns = [name for name, _ in source_columns(db, table)]
    statement = sql.SQL("INSERT INTO {} (source_table, source_pk, row, reason, migrated_at) "
                        "VALUES (%s, %s, %s::jsonb, %s, %s)").format(_qualified(schema, QUARANTINE_TABLE))
    batch = []
    for ident, row in _rows(db, table, columns, identity):
        if ident in orphans:
            record = {column: _jsonable(value) for column, value in zip(columns, row, strict=True)}
            batch.append((table, _pk_text(identity, ident), json.dumps(record, ensure_ascii=False),
                          orphans[ident], moment))
    try:
        with target.cursor() as cursor:
            cursor.executemany(statement, batch)
    except psycopg.errors.UntranslatableCharacter as exc:
        raise MigrationError(f"{database}: {table}: an orphan row holds text PostgreSQL JSON cannot store") from exc


def _copy_table(db: sqlite3.Connection, target: psycopg.Connection, schema: str, database: str, plan: TablePlan,
                observer: CopyObserver, merge: bool, orphans: Mapping[RowIdentity, str]) -> TableDigest:
    total = db.execute(f"SELECT COUNT(*) FROM {_quote_sqlite(plan.name)}").fetchone()[0]
    observer.table_started(database, plan.name, total)
    names = sql.SQL(", ").join(sql.Identifier(column.name) for column in plan.columns)
    digest = _DigestBuilder(plan.key)
    pending = 0

    def converted(row: tuple) -> list[Any]:
        values, canonical = [], []
        for column, value in zip(plan.columns, row, strict=True):
            try:
                stored, form = column.transfer(value)
            except ConversionError as exc:
                raise MigrationError(f"{database}: {plan.name}.{column.name} row {_row_key(plan, row)}: {exc}") from exc
            values.append(stored)
            canonical.append(form)
        digest.add(canonical)
        return values

    rows = _kept_rows(db, plan, orphans)
    if plan.always_identity or merge:
        placeholders = sql.SQL(", ").join(sql.Placeholder() * len(plan.columns))
        statement = sql.SQL("INSERT INTO {} ({}) {} VALUES ({}) {}").format(
            _qualified(schema, plan.name), names,
            sql.SQL("OVERRIDING SYSTEM VALUE" if plan.always_identity else ""), placeholders,
            _upsert(database, plan) if merge else sql.SQL(""))
        batch: list[list[Any]] = []
        with target.cursor() as cursor:
            for row in rows:
                batch.append(converted(row))
                if len(batch) >= FETCH_BATCH_ROWS:
                    cursor.executemany(statement, batch)
                    observer.rows_copied(len(batch))
                    observer.checkpoint()
                    batch.clear()
            if batch:
                cursor.executemany(statement, batch)
                observer.rows_copied(len(batch))
    else:
        statement = sql.SQL("COPY {} ({}) FROM STDIN").format(_qualified(schema, plan.name), names)
        with target.cursor() as cursor, cursor.copy(statement) as copy:
            for row in rows:
                copy.write_row(converted(row))
                pending += 1
                if pending >= PROGRESS_EVERY_ROWS:
                    observer.rows_copied(pending)
                    observer.checkpoint()
                    pending = 0
        if pending:
            observer.rows_copied(pending)
    if orphans:
        observer.rows_copied(len(orphans))
    result = digest.finish(plan.name)
    return TableDigest(table=result.table, rows=result.rows, sha256=result.sha256, quarantined=len(orphans))


def _advance_sequences(target: psycopg.Connection, schema: str, plan: TablePlan, floors: Mapping[str, int]) -> None:
    table = _qualified(schema, plan.name)
    for column in plan.sequences:
        sequence = target.execute("SELECT pg_get_serial_sequence(%s, %s)",
                                  (table.as_string(target), column)).fetchone()[0]
        if sequence is None:
            continue
        highest = target.execute(sql.SQL("SELECT max({}) FROM {}").format(sql.Identifier(column), table)).fetchone()[0]
        # sqlite_sequence belongs to the table's single AUTOINCREMENT key: ids deleted in SQLite stay unused.
        floor = max(highest or 0, floors.get(plan.name, 0) if len(plan.sequences) == 1 else 0)
        if floor >= 1:
            target.execute("SELECT setval(%s, %s, true)", (sequence, floor))
        else:
            target.execute("SELECT setval(%s, 1, false)", (sequence,))


def _user_triggers(target: psycopg.Connection, schema: str, tables: Iterable[str]) -> list[tuple[str, str]]:
    """(table, trigger) of every enabled user trigger on ``tables``."""
    rows = target.execute("""
        SELECT c.relname, t.tgname FROM pg_trigger t
        JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s AND c.relname = ANY(%s) AND NOT t.tgisinternal AND t.tgenabled <> 'D'
        ORDER BY c.relname, t.tgname""", (schema, list(tables))).fetchall()
    return [(table, trigger) for table, trigger in rows]


def _set_triggers(target: psycopg.Connection, schema: str, triggers: Iterable[tuple[str, str]],
                  enabled: bool) -> None:
    action = sql.SQL("ENABLE" if enabled else "DISABLE")
    for table, trigger in triggers:
        target.execute(sql.SQL("ALTER TABLE {} {} TRIGGER {}").format(
            _qualified(schema, table), action, sql.Identifier(trigger)))


def _session(target: psycopg.Connection, schema: str) -> None:
    """Transaction-local settings: the copy may run longer than the role's statement timeout."""
    target.execute(sql.SQL("SET LOCAL search_path = {}, {}, {}").format(
        sql.Identifier(schema), sql.Identifier(COMPAT_SCHEMA), sql.Identifier(EXTENSIONS_SCHEMA)))
    target.execute("SET LOCAL statement_timeout = 0")
    target.execute("SET LOCAL idle_in_transaction_session_timeout = 0")
    # A row-level security policy on a workspace table must never run inside the migration: with
    # row_security off PostgreSQL raises an error instead of executing it.
    target.execute("SET LOCAL row_security = off")


def _check_ledger(db: sqlite3.Connection, target: psycopg.Connection, schema: str, database: str,
                  table: str) -> None:
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    if exists is None:
        return
    key = _primary_key(db, table)
    if not key or table not in target_tables(target, schema):
        raise MigrationError(f"{database}: ledger table {table} needs a primary key on both sides")
    columns = sql.SQL(", ").join(sql.Identifier(name) for name in key)
    known = {tuple(row) for row in target.execute(sql.SQL("SELECT {} FROM {}").format(
        columns, _qualified(schema, table)))}
    missing = sorted(str(values[0] if len(values) == 1 else values)
                     for values in (tuple(row) for row in db.execute(
                         f"SELECT {_identity_select(key)} FROM {_quote_sqlite(table)}")) if values not in known)
    if missing:
        raise MigrationError(f"{database}: the SQLite data comes from a newer TAM ({table} {', '.join(missing[:5])} "
                             "unknown to this build); upgrade the server first")


PostCopy = Callable[[psycopg.Connection, str], None]


def copy_database(source: SourceDatabase, target: psycopg.Connection, schema: str, observer: CopyObserver,
                  post_copy: Sequence[PostCopy] = (), merge: frozenset[str] = frozenset(),
                  replace: frozenset[str] = frozenset(),
                  ledgers: frozenset[str] = frozenset()) -> DatabaseDigest:
    """Copy one SQLite database into ``schema`` in one transaction; returns the source-side digests.

    ``target`` must be an autocommit connection whose role owns the schema's tables (disabling
    triggers needs ownership). User triggers are disabled for the copy, so PostgreSQL-only derived
    tables (the full-text side tables and their BM25 statistics) are rebuilt by ``post_copy``
    hooks from the copied rows. The target tables must
    be empty, except ``merge`` tables that provisioning seeds (``meta``): SQLite rows replace seeded
    rows with the same key, rows only PostgreSQL has stay, and verification compares the rows whose
    keys SQLite has. ``replace`` tables are seeded too, but SQLite is authoritative: the seeded rows
    are deleted and the SQLite rows copied. ``ledgers`` (the migration ledger) are not copied; every
    SQLite key must already exist in PostgreSQL, otherwise the source is newer than this build.
    Orphan rows (find_orphans) go to ``migration_quarantine`` instead of their table;
    a schema without that table refuses them. ``post_copy`` hooks rebuild PostgreSQL-only derived
    state inside the same transaction.
    """
    with closing(open_source(source.path)) as db:
        floors = autoincrement_floor(db)
        orphans = find_orphans(db)
        with target.transaction():
            _session(target, schema)
            target.execute("SET CONSTRAINTS ALL DEFERRED")
            for ledger in sorted(ledgers):
                _check_ledger(db, target, schema, source.name, ledger)
            plans = _table_plans(db, target, schema, source.name, ledgers)
            for plan in plans:
                if plan.name in replace:
                    target.execute(sql.SQL("DELETE FROM {}").format(_qualified(schema, plan.name)))
            occupied = [plan.name for plan in plans if plan.name not in merge and target.execute(
                sql.SQL("SELECT 1 FROM {} LIMIT 1").format(_qualified(schema, plan.name))).fetchone()]
            if occupied:
                raise MigrationError(f"{source.name}: PostgreSQL tables are not empty: {', '.join(occupied)}")
            if orphans and QUARANTINE_TABLE not in target_tables(target, schema):
                raise MigrationError(f"{source.name}: rows violate foreign keys in {', '.join(sorted(orphans))} "
                                     f"and the schema has no {QUARANTINE_TABLE} table")
            disabled = _user_triggers(target, schema, [plan.name for plan in plans])
            _set_triggers(target, schema, disabled, enabled=False)
            digests = []
            for plan in plans:
                observer.checkpoint()
                table_orphans = orphans.get(plan.name, {})
                digests.append(_copy_table(db, target, schema, source.name, plan, observer, plan.name in merge,
                                           table_orphans))
                if table_orphans:
                    _quarantine(db, target, schema, source.name, plan.name, table_orphans,
                                datetime.now(UTC).isoformat())
                _advance_sequences(target, schema, plan, floors)
            _set_triggers(target, schema, disabled, enabled=True)
            for hook in post_copy:
                hook(target, schema)
            observer.checkpoint()
    return DatabaseDigest(database=source.name, tables=tuple(sorted(digests, key=lambda digest: digest.table)))


def _source_rows(db: sqlite3.Connection, database: str, plan: TablePlan,
                 orphans: Mapping[RowIdentity, str]) -> Iterator[list[bytes]]:
    for row in _kept_rows(db, plan, orphans):
        canonical = []
        for column, value in zip(plan.columns, row, strict=True):
            try:
                canonical.append(column.canonical_source(value))
            except ConversionError as exc:
                raise MigrationError(f"{database}: {plan.name}.{column.name} row {_row_key(plan, row)}: "
                                     f"{exc}") from exc
        yield canonical


def source_digest(source: SourceDatabase, target: psycopg.Connection, schema: str,
                  ledgers: frozenset[str] = frozenset()) -> DatabaseDigest:
    """Digest of the SQLite side as the copy would write it (column types come from ``schema``)."""
    with closing(open_source(source.path)) as db:
        plans = _table_plans(db, target, schema, source.name, ledgers)
        orphans = find_orphans(db)
        digests = []
        for plan in plans:
            digest = _DigestBuilder(plan.key)
            table_orphans = orphans.get(plan.name, {})
            for canonical in _source_rows(db, source.name, plan, table_orphans):
                digest.add(canonical)
            result = digest.finish(plan.name)
            digests.append(TableDigest(table=result.table, rows=result.rows, sha256=result.sha256,
                                       quarantined=len(table_orphans)))
    return DatabaseDigest(database=source.name, tables=tuple(sorted(digests, key=lambda digest: digest.table)))


def target_digest(source: SourceDatabase, target: psycopg.Connection, schema: str,
                  merge: frozenset[str] = frozenset(), ledgers: frozenset[str] = frozenset()) -> DatabaseDigest:
    """Digest of what PostgreSQL holds for the tables of ``source``, read in one REPEATABLE READ snapshot.

    For ``merge`` tables only rows whose primary key SQLite also has are included.
    """
    with closing(open_source(source.path)) as db, target.transaction():
        target.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        _session(target, schema)
        plans = _table_plans(db, target, schema, source.name, ledgers)
        quarantined: dict[str, int] = {}
        if QUARANTINE_TABLE in target_tables(target, schema):
            quarantined = dict(target.execute(sql.SQL("SELECT source_table, count(*) FROM {} GROUP BY 1").format(
                _qualified(schema, QUARANTINE_TABLE))).fetchall())
        digests = []
        for plan in plans:
            wanted = None
            if plan.name in merge:
                wanted = {_DigestBuilder.order(plan.key, canonical)
                          for canonical in _source_rows(db, source.name, plan, {})}
            digest = _DigestBuilder(plan.key)
            selected = sql.SQL(", ").join(column.select() for column in plan.columns)
            with target.cursor(name="tam_verify") as cursor:
                cursor.itersize = FETCH_BATCH_ROWS
                cursor.execute(sql.SQL("SELECT {} FROM {}").format(selected, _qualified(schema, plan.name)))
                for row in cursor:
                    canonical = [column.canonical_target(value)
                                 for column, value in zip(plan.columns, row, strict=True)]
                    if wanted is None or _DigestBuilder.order(plan.key, canonical) in wanted:
                        digest.add(canonical)
            result = digest.finish(plan.name)
            digests.append(TableDigest(table=result.table, rows=result.rows, sha256=result.sha256,
                                       quarantined=quarantined.get(plan.name, 0)))
    return DatabaseDigest(database=source.name, tables=tuple(sorted(digests, key=lambda digest: digest.table)))


def compare(expected: DatabaseDigest, actual: DatabaseDigest) -> list[Mismatch]:
    mismatches = []
    for table in expected.tables:
        other = actual.table(table.table)
        if other is None:
            mismatches.append(Mismatch(table.table, "missing in PostgreSQL"))
        elif other.rows != table.rows:
            mismatches.append(Mismatch(table.table, f"{table.rows} rows in SQLite, {other.rows} in PostgreSQL"))
        elif other.quarantined != table.quarantined:
            mismatches.append(Mismatch(table.table, f"{table.quarantined} orphan rows in SQLite, "
                                                    f"{other.quarantined} quarantined in PostgreSQL"))
        elif other.sha256 != table.sha256:
            mismatches.append(Mismatch(table.table, "row contents differ"))
    return mismatches


def _copied_tables(source: SourceDatabase, target: psycopg.Connection, schema: str,
                   merge: frozenset[str]) -> list[str]:
    with closing(open_source(source.path)) as db:
        names = source_tables(db)
    catalog = target_tables(target, schema)
    return [name for name in names if name in catalog and name not in merge]


def truncate_copied_tables(source: SourceDatabase, target: psycopg.Connection, schema: str,
                           merge: frozenset[str] = frozenset()) -> None:
    """Empty the tables an earlier, unfinished job filled (resume of a control schema); seeded tables stay."""
    present = _copied_tables(source, target, schema, merge)
    if present:
        target.execute(sql.SQL("TRUNCATE {} RESTART IDENTITY").format(
            sql.SQL(", ").join(_qualified(schema, name) for name in present)))


def has_rows(source: SourceDatabase, target: psycopg.Connection, schema: str,
             merge: frozenset[str] = frozenset()) -> bool:
    return any(target.execute(sql.SQL("SELECT 1 FROM {} LIMIT 1").format(_qualified(schema, name))).fetchone()
               for name in _copied_tables(source, target, schema, merge))


@dataclass(frozen=True, slots=True)
class Throughput:
    """Copy speed behind the dry-run time estimate."""

    rows_per_second: float
    bytes_per_second: float
    seconds_per_database: float

    def seconds(self, estimates: Sequence[DatabaseEstimate]) -> int:
        rows = sum(item.rows for item in estimates)
        size = sum(item.bytes for item in estimates)
        work = max(rows / self.rows_per_second, size / self.bytes_per_second)
        return math.ceil(work + self.seconds_per_database * len(estimates))

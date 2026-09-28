"""SQLite PRAGMA emulation and the catalog lookups the compatibility connection needs.

``PgCatalog`` reads table shapes (columns, integer key, unique keys) from pg_catalog for
the translator (REPLACE conflict targets, rowid) and for ``PRAGMA table_info`` /
``index_list`` / ``index_info``. Storage pragmas that only tune SQLite (journal_mode,
synchronous, busy_timeout, cache_size, foreign_keys, ...) are accepted and answered with
fixed values; integrity checks report "ok" because PostgreSQL enforces constraints on
every write. ``data_version`` is constant per connection: SQLite changes it only when
another connection commits, and a PostgreSQL workspace has a single writer (the worker
holding the workspace lease), so a cache keyed on it stays correct.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tam_db.contracts import UntranslatableSQL
from tam_db.translate import PragmaCall, TableShape

INTEGER_TYPES = frozenset({"smallint", "integer", "bigint"})
RELATION_KINDS = ("r", "p", "v", "m", "f")
DATA_VERSION = 1
JOURNAL_MODE = "wal"

# Pragmas that tune SQLite storage: an assignment is accepted and ignored, a query returns
# the value SQLite would report for the settings Store applies.
STORAGE_PRAGMAS: Mapping[str, object] = {
    "journal_mode": JOURNAL_MODE,
    "synchronous": 1,
    "busy_timeout": 5000,
    "cache_size": -20000,
    "foreign_keys": 1,
    "temp_store": 0,
    "mmap_size": 0,
    "wal_autocheckpoint": 1000,
    "secure_delete": 0,
    "recursive_triggers": 0,
    "case_sensitive_like": 0,
    "automatic_index": 1,
    "optimize": None,
    "analysis_limit": 0,
}
# Assignments SQLite answers with the new value as a one-row result.
ECHOING_ASSIGNMENTS = frozenset({"journal_mode", "busy_timeout"})

TABLE_QUERY = """
SELECT c.oid, c.relname
FROM pg_catalog.pg_class c
WHERE c.oid = pg_catalog.to_regclass(%s) AND c.relkind = ANY(%s)
"""
COLUMNS_QUERY = """
SELECT a.attname, pg_catalog.format_type(a.atttypid, a.atttypmod), a.attnotnull,
       pg_catalog.pg_get_expr(d.adbin, d.adrelid), a.attgenerated <> '', a.attidentity <> ''
FROM pg_catalog.pg_attribute a
LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum
"""
KEYS_QUERY = """
SELECT i.indisprimary,
       ARRAY(SELECT a.attname FROM unnest(i.indkey) WITH ORDINALITY AS k(attnum, position)
             JOIN pg_catalog.pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
             ORDER BY k.position)
FROM pg_catalog.pg_index i
WHERE i.indrelid = %s AND i.indisunique AND i.indpred IS NULL AND i.indexprs IS NULL
ORDER BY NOT i.indisprimary, i.indexrelid
"""
INDEX_LIST_QUERY = """
SELECT ic.relname, i.indisunique,
       CASE WHEN i.indisprimary THEN 'pk' WHEN con.oid IS NOT NULL THEN 'u' ELSE 'c' END,
       i.indpred IS NOT NULL
FROM pg_catalog.pg_index i
JOIN pg_catalog.pg_class ic ON ic.oid = i.indexrelid
LEFT JOIN pg_catalog.pg_constraint con ON con.conindid = i.indexrelid AND con.contype = 'u'
WHERE i.indrelid = %s
ORDER BY ic.oid DESC
"""
INDEX_INFO_QUERY = """
SELECT k.position - 1, a.attnum - 1, a.attname
FROM pg_catalog.pg_index i
CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, position)
LEFT JOIN pg_catalog.pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
WHERE i.indexrelid = pg_catalog.to_regclass(%s)
ORDER BY k.position
"""


def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


@dataclass(frozen=True, slots=True)
class ColumnInfo:
    name: str
    type: str
    not_null: bool
    default: str | None
    generated: bool
    identity: bool


@dataclass(frozen=True, slots=True)
class TableDetails:
    oid: int
    shape: TableShape
    columns: tuple[ColumnInfo, ...]
    primary_key: tuple[str, ...]


class PgCatalog:
    """Table shapes of the connection's search_path, cached until ``invalidate``.

    ``execute`` runs one catalog query on the underlying psycopg connection and returns
    its rows; the compatibility connection supplies it so catalog reads share its session.
    """

    def __init__(self, execute):
        self.query = execute
        self._tables: dict[str, TableDetails] = {}

    def invalidate(self) -> None:
        self._tables.clear()

    def details(self, name: str) -> TableDetails | None:
        key = name.lower()
        cached = self._tables.get(key)
        if cached is not None:
            return cached
        rows = self.query(TABLE_QUERY, (quote_identifier(key), list(RELATION_KINDS)))
        if not rows:
            # Not cached: the table may be created later in this session.
            return None
        oid, relname = rows[0]
        columns = tuple(ColumnInfo(name=row[0], type=row[1], not_null=bool(row[2]), default=row[3],
                                   generated=bool(row[4]), identity=bool(row[5]))
                        for row in self.query(COLUMNS_QUERY, (oid,)))
        keys = self.query(KEYS_QUERY, (oid,))
        primary = next((tuple(row[1]) for row in keys if row[0]), ())
        types = {column.name: column.type for column in columns}
        rowid = primary[0] if len(primary) == 1 and types.get(primary[0]) in INTEGER_TYPES else None
        shape = TableShape(
            name=relname,
            columns=tuple(column.name for column in columns),
            rowid_column=rowid,
            unique_keys=tuple(tuple(row[1]) for row in keys),
            generated=frozenset(column.name for column in columns if column.generated),
            identity=frozenset(column.name for column in columns if column.identity),
            not_null=frozenset(column.name for column in columns if column.not_null),
        )
        details = TableDetails(oid=oid, shape=shape, columns=columns, primary_key=primary)
        self._tables[key] = details
        return details

    def table(self, name: str) -> TableShape | None:
        details = self.details(name)
        return None if details is None else details.shape


@dataclass(frozen=True, slots=True)
class PragmaResult:
    columns: tuple[str, ...]
    rows: list[tuple[Any, ...]]


def execute_pragma(call: PragmaCall, catalog: PgCatalog, source: str) -> PragmaResult:
    """Answer a PRAGMA the way SQLite would for an equivalent database."""
    name = call.name
    if name == "table_info":
        return _table_info(call, catalog, source)
    if name == "index_list":
        return _index_list(call, catalog, source)
    if name == "index_info":
        return _index_info(call, catalog, source)
    if name in ("quick_check", "integrity_check"):
        return PragmaResult((name,), [("ok",)])
    if name == "foreign_key_check":
        return PragmaResult(("table", "rowid", "parent", "fkid"), [])
    if name == "data_version":
        return PragmaResult((name,), [(DATA_VERSION,)])
    if name in STORAGE_PRAGMAS:
        if call.assignment:
            if name in ECHOING_ASSIGNMENTS:
                value = JOURNAL_MODE if name == "journal_mode" else _integer(call.argument, name, source)
                return PragmaResult((name,), [(value,)])
            return PragmaResult((), [])
        value = STORAGE_PRAGMAS[name]
        return PragmaResult((), []) if value is None else PragmaResult((name,), [(value,)])
    raise UntranslatableSQL(f"PRAGMA {name} has no PostgreSQL equivalent", source)


def _integer(argument: str | None, name: str, source: str) -> int:
    try:
        return int(argument or "")
    except ValueError as exc:
        raise UntranslatableSQL(f"PRAGMA {name} expects an integer", source) from exc


def _required_table(call: PragmaCall, catalog: PgCatalog) -> TableDetails | None:
    if not call.argument:
        return None
    return catalog.details(call.argument)


def _table_info(call: PragmaCall, catalog: PgCatalog, source: str) -> PragmaResult:
    columns = ("cid", "name", "type", "notnull", "dflt_value", "pk")
    details = _required_table(call, catalog)
    if details is None:
        return PragmaResult(columns, [])
    rows = []
    for cid, column in enumerate(details.columns):
        pk = details.primary_key.index(column.name) + 1 if column.name in details.primary_key else 0
        # SQLite reports only declared NOT NULL; PostgreSQL marks every key column NOT NULL.
        not_null = int(column.not_null and not pk)
        rows.append((cid, column.name, column.type.upper(), not_null, column.default, pk))
    return PragmaResult(columns, rows)


def _index_list(call: PragmaCall, catalog: PgCatalog, source: str) -> PragmaResult:
    columns = ("seq", "name", "unique", "origin", "partial")
    details = _required_table(call, catalog)
    if details is None:
        return PragmaResult(columns, [])
    rows = catalog.query(INDEX_LIST_QUERY, (details.oid,))
    return PragmaResult(columns, [(seq, row[0], int(row[1]), row[2], int(row[3])) for seq, row in enumerate(rows)])


def _index_info(call: PragmaCall, catalog: PgCatalog, source: str) -> PragmaResult:
    columns = ("seqno", "cid", "name")
    if not call.argument:
        return PragmaResult(columns, [])
    rows = catalog.query(INDEX_INFO_QUERY, (quote_identifier(call.argument.lower()),))
    return PragmaResult(columns, [(row[0], row[1] if row[2] is not None else -2, row[2]) for row in rows])

"""SQLite -> PostgreSQL statement translation for the team server's compatibility connection.

The translator works on a token stream (a lexer splits the statement into literals,
identifiers, parameters and operators; rules only ever look at tokens), so string
literals, quoted identifiers and comments are never rewritten by accident.

What it does, in one pass per statement (results are cached, ``TRANSLATION_CACHE_SIZE``):
  - parameters: ``?`` / ``?NNN`` -> ``%s`` (with the parameter order), ``:name`` ->
    ``%(name)s``; every literal ``%`` is doubled for psycopg;
  - ``INSERT OR IGNORE`` -> ``ON CONFLICT DO NOTHING``; ``INSERT OR REPLACE`` / ``REPLACE
    INTO`` -> ``ON CONFLICT (<key>) DO UPDATE`` resolved against the catalog at execution
    time (``Translation.render``); ``INSERT/UPDATE OR ABORT|FAIL|ROLLBACK`` -> plain;
  - ``a IS b`` / ``a IS NOT b`` -> ``IS [NOT] DISTINCT FROM``; ``LIKE`` -> ``ILIKE ... ESCAPE ''``
    (SQLite LIKE is case-insensitive and has no default escape character); ``GLOB`` ->
    ``tam_compat.glob_match``; ``==`` -> ``=``; ``IN ()`` -> ``= ANY('{}')``;
  - types: ``INTEGER`` -> bigint, ``REAL`` -> double precision, ``BLOB`` -> bytea,
    ``INTEGER PRIMARY KEY [AUTOINCREMENT]`` -> identity primary key, ``WITHOUT ROWID`` /
    ``STRICT`` dropped; ``X'..'`` -> bytea literal;
  - ``INDEXED BY x`` / ``NOT INDEXED`` dropped, unary ``+`` planner hints dropped,
    ``CROSS JOIN t ON|USING`` -> ``JOIN`` (SQLite's join-order hint), ``LIMIT -1`` ->
    ``LIMIT ALL``, ``LIMIT a, b`` -> ``LIMIT b OFFSET a``;
  - SQLite functions provided by migrations/postgres/compat (strftime, datetime, date,
    time, julianday, unixepoch, json_extract, instr, hex, ifnull, glob, group_concat,
    round, multi-argument max/min) -> ``tam_compat.<name>(``;
  - ``json_object(`` -> ``tam_compat.json_compact(json_build_object(...))`` (its str
    parameters are sent typed, ``Translation.text_parameters``), ``char(`` ->
    ``chr``, ``CURRENT_TIMESTAMP/DATE/TIME`` -> the SQLite text forms, ``sqlite_schema`` ->
    ``sqlite_master``, backtick / bracket identifiers -> lower-case double-quoted ones;
  - ORDER BY terms without a NULLS clause get SQLite's NULL placement: ASC -> NULLS
    FIRST, DESC -> NULLS LAST (statement, window and aggregate ORDER BY); a bare column the
    catalog shows NOT NULL gets none (same result; primary-key indexes stay usable);
  - ``rowid`` -> the table's integer primary key (catalog, at execution time);
  - BEGIN DEFERRED|IMMEDIATE|EXCLUSIVE -> BEGIN, END -> COMMIT;
  - PRAGMA and ``SELECT last_insert_rowid()`` are classified for the connection to answer.

Refused on purpose with UntranslatableSQL (the call site needs an explicit PostgreSQL
branch): FTS5 (``MATCH``, ``bm25()``, ``highlight()``, ``snippet()``, any ``*_fts`` table),
``rowid`` without a resolvable integer key, ``CREATE VIRTUAL TABLE``, ``CREATE/DROP
TRIGGER``, ``ATTACH``/``DETACH``, ``VACUUM INTO``, ``REGEXP``, ``COLLATE NOCASE|RTRIM``,
``UPDATE OR IGNORE|REPLACE``, ``@name``/``$name`` parameters, a mix of named and
positional parameters.
"""

import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from typing import Protocol

from tam_db.contracts import COMPAT_SCHEMA, UntranslatableSQL

TRANSLATION_CACHE_SIZE = 4096

# INSERT OR REPLACE into a table with several candidate keys covered by the insert's
# columns: which key SQLite's REPLACE is meant to act on. Keys are lower-case table names.
REPLACE_CONFLICT_TARGETS: Mapping[str, tuple[str, ...]] = {}

_LEXER = re.compile(
    r"""
    (?P<space>\s+)
    |(?P<comment>--[^\n]*|/\*.*?(?:\*/|\Z))
    |(?P<blob>[xX]'(?:[0-9a-fA-F]{2})*')
    |(?P<string>'(?:[^']|'')*')
    |(?P<dquote>"(?:[^"]|"")*")
    |(?P<backtick>`(?:[^`]|``)*`)
    |(?P<bracket>\[(?=[^\W\d]|\s)[^\]]*\])
    |(?P<number>0[xX][0-9a-fA-F]+|(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)
    |(?P<param>\?\d*|:[^\W\d]\w*|@[^\W\d]\w*|\$[^\W\d][\w$]*)
    |(?P<word>[^\W\d][\w$]*)
    |(?P<op>\|\||<=|>=|<>|!=|==|<<|>>|->>|->|::|[-+*/%<>=~&|(),;.!]|\[(?![^\W\d]|\s)|\])
    |(?P<unterminated>['"`]|\[)
    |(?P<other>.)
    """,
    re.VERBOSE | re.DOTALL,
)

FTS_TABLE = re.compile(r"^\w+_fts$", re.IGNORECASE)
FTS_FUNCTIONS = frozenset({"BM25", "HIGHLIGHT", "SNIPPET"})
ROWID_WORDS = frozenset({"ROWID", "_ROWID_"})
CONFLICT_ALGORITHMS = frozenset({"ABORT", "FAIL", "ROLLBACK", "IGNORE", "REPLACE"})
TRANSACTION_MODES = frozenset({"DEFERRED", "IMMEDIATE", "EXCLUSIVE"})
POSTGRES_TRANSACTION_MODES = frozenset({"ISOLATION", "READ", "NOT"})
IS_KEEP = frozenset({"NULL", "TRUE", "FALSE", "UNKNOWN", "DISTINCT", "JSON", "DOCUMENT", "NORMALIZED"})
# Depth-0 words that end the right operand of LIKE / GLOB.
OPERAND_STOP_WORDS = frozenset({
    "AND", "OR", "ESCAPE", "THEN", "WHEN", "ELSE", "END", "ORDER", "GROUP", "HAVING", "LIMIT", "OFFSET",
    "UNION", "INTERSECT", "EXCEPT", "FROM", "WHERE", "AS", "ON", "RETURNING", "COLLATE", "IS", "NOT",
    "IN", "LIKE", "GLOB", "BETWEEN", "ASC", "DESC", "NULLS", "WINDOW", "DO", "SET", "VALUES", "USING",
    "JOIN", "INNER", "LEFT", "RIGHT", "FULL", "CROSS", "NATURAL", "SELECT",
})
OPERAND_STOP_OPS = frozenset({")", ",", ";", "=", "==", "<>", "!=", "<", ">", "<=", ">="})
# Tokens after which "+" is a unary operator.
UNARY_CONTEXT_OPS = frozenset({"(", ",", "=", "==", "<>", "!=", "<", ">", "<=", ">=", "+", "-", "*", "/", "%", "||"})
UNARY_CONTEXT_WORDS = frozenset({
    "SELECT", "WHERE", "AND", "OR", "NOT", "ON", "BY", "HAVING", "WHEN", "THEN", "ELSE", "RETURN", "SET",
    "DISTINCT", "ALL", "CASE", "IN", "IS", "LIKE", "BETWEEN", "VALUES",
})
TYPE_WORDS: Mapping[str, str] = {"INTEGER": "bigint", "REAL": "double precision", "BLOB": "bytea"}
CURRENT_WORDS: Mapping[str, str] = {
    "CURRENT_TIMESTAMP": f"{COMPAT_SCHEMA}.datetime('now')",
    "CURRENT_DATE": f"{COMPAT_SCHEMA}.date('now')",
    "CURRENT_TIME": f"{COMPAT_SCHEMA}.time('now')",
}
UNSUPPORTED_COLLATIONS = frozenset({"NOCASE", "RTRIM"})
# Calls resolved to tam_compat explicitly, so a pg_catalog function or aggregate of the
# same name (round, date, max, ...) never wins overload resolution.
COMPAT_FUNCTIONS = frozenset({
    "STRFTIME", "DATETIME", "DATE", "TIME", "JULIANDAY", "UNIXEPOCH", "JSON_EXTRACT", "INSTR", "HEX",
    "IFNULL", "GLOB", "GROUP_CONCAT", "ROUND",
})
SCALAR_MAX_MIN = frozenset({"MAX", "MIN"})
# Depth-0 words that end an ORDER BY clause.
ORDER_BY_END_WORDS = frozenset({
    "LIMIT", "OFFSET", "UNION", "INTERSECT", "EXCEPT", "ROWS", "RANGE", "GROUPS", "FETCH", "FOR", "RETURNING",
    "WINDOW", "ON", "DO",
})
# Words that can follow a table reference but are not an alias.
ALIAS_STOP_WORDS = frozenset({
    "WHERE", "JOIN", "ON", "USING", "LEFT", "RIGHT", "INNER", "OUTER", "FULL", "CROSS", "NATURAL", "GROUP",
    "ORDER", "LIMIT", "OFFSET", "UNION", "INTERSECT", "EXCEPT", "SET", "VALUES", "DEFAULT", "SELECT", "WINDOW",
    "HAVING", "RETURNING", "INDEXED", "NOT", "WITH", "DO",
})
# Words after a joined table that end its join clause.
JOIN_END_WORDS = frozenset({
    "JOIN", "CROSS", "INNER", "LEFT", "RIGHT", "FULL", "NATURAL", "WHERE", "GROUP", "HAVING", "ORDER", "LIMIT",
    "UNION", "INTERSECT", "EXCEPT", "WINDOW", "RETURNING",
})
IDENTITY_PRIMARY_KEY = "bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"


class TokenKind(StrEnum):
    SPACE = "space"
    COMMENT = "comment"
    BLOB = "blob"
    STRING = "string"
    IDENT = "ident"
    NUMBER = "number"
    PARAM = "param"
    WORD = "word"
    OP = "op"


class StatementKind(StrEnum):
    EMPTY = "empty"
    SELECT = "select"
    INSERT = "insert"
    UPDATE = "update"
    DELETE = "delete"
    DDL = "ddl"
    BEGIN = "begin"
    COMMIT = "commit"
    ROLLBACK = "rollback"
    ROLLBACK_TO = "rollback_to"
    SAVEPOINT = "savepoint"
    RELEASE = "release"
    PRAGMA = "pragma"
    LAST_INSERT_ROWID = "last_insert_rowid"
    OTHER = "other"


DML_KINDS = frozenset({StatementKind.INSERT, StatementKind.UPDATE, StatementKind.DELETE})
TRANSACTION_CONTROL_KINDS = frozenset({
    StatementKind.BEGIN, StatementKind.COMMIT, StatementKind.ROLLBACK, StatementKind.ROLLBACK_TO,
    StatementKind.SAVEPOINT, StatementKind.RELEASE,
})
DDL_WORDS = frozenset({"CREATE", "DROP", "ALTER"})


class _Token:
    __slots__ = ("kind", "out", "param", "suffix", "text", "untyped_context")

    def __init__(self, kind: TokenKind, text: str, out: str | None = None):
        self.kind = kind
        self.text = text
        self.out = text if out is None else out
        self.param: int | str | None = None
        # Text emitted after ``out``, appended by clause-level rules (NULLS ..., ON CONFLICT ...)
        # so token rewrites that replace ``out`` cannot drop it.
        self.suffix = ""
        # A parameter in an argument of type "any" (json_build_object): PostgreSQL cannot
        # infer an untyped parameter's type there, so text values are sent typed.
        self.untyped_context = False

    @property
    def upper(self) -> str:
        return self.text.upper() if self.kind is TokenKind.WORD else ""

    def is_op(self, *ops: str) -> bool:
        return self.kind is TokenKind.OP and self.text in ops


@dataclass(frozen=True, slots=True)
class TableShape:
    """What the translator needs to know about a table (from the PostgreSQL catalog)."""

    name: str
    columns: tuple[str, ...]
    rowid_column: str | None
    unique_keys: tuple[tuple[str, ...], ...]
    generated: frozenset[str] = frozenset()
    identity: frozenset[str] = frozenset()
    not_null: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class NullOrdering:
    """An ORDER BY term that is a bare column: its NULLS clause is dropped at render time when
    the column is NOT NULL (no NULLs to place), so constraint indexes such as the primary key,
    which PostgreSQL only builds with the default ordering, still serve the order."""

    marker: str
    qualifier: str | None
    column: str
    clause: str


class Catalog(Protocol):
    def table(self, name: str) -> TableShape | None:
        """Shape of the table ``name`` resolves to in the connection's search_path, or None."""
        ...


@dataclass(frozen=True, slots=True)
class PragmaCall:
    name: str
    argument: str | None
    assignment: bool


@dataclass(frozen=True, slots=True)
class ReplaceClause:
    table: str
    columns: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class Translation:
    """A translated statement.

    ``sql`` uses psycopg placeholders; ``param_order`` maps each placeholder to the index
    of the caller's positional parameter (None when named or already in order);
    ``param_count`` is how many positional parameters the SQLite statement binds.
    Catalog-dependent parts (REPLACE conflict clause, rowid) are resolved by ``render``.
    """

    source: str
    sql: str
    kind: StatementKind
    param_count: int = 0
    param_order: tuple[int, ...] | None = None
    named: bool = False
    insert_table: str | None = None
    replace: ReplaceClause | None = None
    rowid_table: str | None = None
    null_orderings: tuple[NullOrdering, ...] = ()
    tables: tuple[tuple[str, str], ...] = ()
    has_returning: bool = False
    pragma: PragmaCall | None = None
    text_parameters: tuple[int | str, ...] = ()

    @property
    def is_dml(self) -> bool:
        return self.kind in DML_KINDS

    @property
    def needs_catalog(self) -> bool:
        return self.replace is not None or self.rowid_table is not None or bool(self.null_orderings)

    def bind(self, parameters: Sequence[object] | Mapping[str, object] | None) -> Sequence[object] | Mapping[str, object]:
        """Parameters in psycopg order; raises sqlite3.ProgrammingError like sqlite3 on a count mismatch."""
        if self.named:
            if not isinstance(parameters, Mapping):
                raise sqlite3.ProgrammingError("named parameters require a mapping")
            return parameters
        values: Sequence[object] = () if parameters is None else parameters
        if isinstance(values, Mapping):
            if self.param_count:
                raise sqlite3.ProgrammingError("positional parameters require a sequence")
            values = ()
        if len(values) != self.param_count:
            raise sqlite3.ProgrammingError(
                f"Incorrect number of bindings supplied. The current statement uses {self.param_count}, "
                f"and there are {len(values)} supplied.")
        if self.param_order is None:
            return tuple(values)
        return tuple(values[index] for index in self.param_order)

    def render(self, catalog: Catalog) -> str:
        sql = self.sql
        if self.rowid_table is not None:
            shape = catalog.table(self.rowid_table)
            if shape is None or shape.rowid_column is None:
                raise UntranslatableSQL(f"rowid of {self.rowid_table} has no integer primary key", self.source)
            sql = sql.replace(_ROWID_MARKER, _quote_identifier(shape.rowid_column))
        if self.replace is not None:
            sql = sql.replace(_CONFLICT_MARKER, _replace_conflict(self.replace, catalog, self.source))
        for ordering in self.null_orderings:
            sql = sql.replace(ordering.marker, "" if self._not_null(ordering, catalog) else ordering.clause)
        return sql

    def _not_null(self, ordering: NullOrdering, catalog: Catalog) -> bool:
        """True when the term's column resolves to exactly one referenced table and is NOT NULL there."""
        if ordering.qualifier is not None:
            candidates = {table for alias, table in self.tables if alias == ordering.qualifier}
        else:
            candidates = {table for _, table in self.tables}
        owners = []
        for table in candidates:
            shape = catalog.table(table)
            if shape is not None and ordering.column in shape.columns:
                owners.append(shape)
        return len(owners) == 1 and ordering.column in owners[0].not_null


_CONFLICT_MARKER = "\x00tam_conflict\x00"
_ROWID_MARKER = "\x00tam_rowid\x00"


def _quote_identifier(name: str) -> str:
    if re.fullmatch(r"[a-z_][a-z0-9_]*", name):
        return name
    return '"' + name.replace('"', '""') + '"'


def _replace_conflict(clause: ReplaceClause, catalog: Catalog, source: str) -> str:
    shape = catalog.table(clause.table)
    if shape is None:
        raise UntranslatableSQL(f"REPLACE into unknown table {clause.table}", source)
    columns = clause.columns or tuple(column for column in shape.columns if column not in shape.generated)
    inserted = set(columns)
    candidates = [key for key in shape.unique_keys if set(key) <= inserted]
    override = REPLACE_CONFLICT_TARGETS.get(shape.name)
    if override is not None:
        if override not in shape.unique_keys:
            raise UntranslatableSQL(f"REPLACE target {override} is not a unique key of {shape.name}", source)
        target = override
    elif len(candidates) == 1:
        target = candidates[0]
    elif not candidates:
        raise UntranslatableSQL(f"REPLACE into {shape.name} sets no complete unique key", source)
    else:
        raise UntranslatableSQL(f"REPLACE into {shape.name} matches several unique keys {candidates}; "
                                "add it to REPLACE_CONFLICT_TARGETS", source)
    assignments = [f"{_quote_identifier(column)} = EXCLUDED.{_quote_identifier(column)}"
                   for column in columns if column not in target]
    # SQLite deletes the conflicting row and inserts a fresh one: columns the statement
    # does not set go back to their defaults. Identity keys keep their value.
    assignments += [f"{_quote_identifier(column)} = DEFAULT" for column in shape.columns
                    if column not in inserted and column not in target
                    and column not in shape.generated and column not in shape.identity]
    conflict = ", ".join(_quote_identifier(column) for column in target)
    if not assignments:
        return f" ON CONFLICT ({conflict}) DO NOTHING"
    return f" ON CONFLICT ({conflict}) DO UPDATE SET " + ", ".join(assignments)


def tokenize(sql: str) -> list[_Token]:
    tokens: list[_Token] = []
    for match in _LEXER.finditer(sql):
        group = match.lastgroup
        text = match.group()
        if group == "unterminated":
            raise UntranslatableSQL(f"unterminated literal or identifier starting with {text}", sql)
        if group == "comment" and text.startswith("/*") and not text.endswith("*/"):
            raise UntranslatableSQL("unterminated comment", sql)
        if group in ("dquote", "backtick", "bracket"):
            inner = text[1:-1]
            if group == "dquote":
                inner = inner.replace('""', '"')
            elif group == "backtick":
                inner = inner.replace("``", "`")
            # SQLite identifiers are case-insensitive even when quoted; the PostgreSQL
            # schema uses lower-case names.
            tokens.append(_Token(TokenKind.IDENT, text, '"' + inner.lower().replace('"', '""') + '"'))
        elif group == "other":
            tokens.append(_Token(TokenKind.OP, text))
        else:
            tokens.append(_Token(TokenKind(group if group != "dquote" else "ident"), text))
    return tokens


def split_statements(script: str) -> list[str]:
    """Split a script into statements at depth-0 semicolons (CREATE TRIGGER bodies kept whole)."""
    statements: list[str] = []
    current: list[str] = []
    significant: list[str] = []
    depth = 0
    in_trigger_body = False
    for token in tokenize(script):
        current.append(token.text)
        if token.kind in (TokenKind.SPACE, TokenKind.COMMENT):
            continue
        word = token.upper
        significant.append(word or token.text)
        if token.is_op("("):
            depth += 1
        elif token.is_op(")"):
            depth -= 1
        elif word == "BEGIN" and _is_trigger(significant):
            in_trigger_body = True
        elif word == "END" and in_trigger_body:
            in_trigger_body = False
        elif token.is_op(";") and depth == 0 and not in_trigger_body:
            statement = "".join(current).strip()
            if statement.rstrip(";").strip():
                statements.append(statement)
            current, significant = [], []
    tail = "".join(current).strip()
    if tail and any(token.kind not in (TokenKind.SPACE, TokenKind.COMMENT) for token in tokenize(tail)):
        statements.append(tail)
    return statements


def _is_trigger(significant: list[str]) -> bool:
    head = [word for word in significant[:4] if word not in ("TEMP", "TEMPORARY")]
    return len(head) >= 2 and head[0] == "CREATE" and head[1] == "TRIGGER"


@lru_cache(maxsize=TRANSLATION_CACHE_SIZE)
def translate(sql: str) -> Translation:
    """Translate one SQLite statement. Raises UntranslatableSQL or sqlite3.ProgrammingError."""
    return _Translator(sql).run()


class _Translator:
    def __init__(self, sql: str):
        self.source = sql
        self.tokens = tokenize(sql)
        # Comments are dropped: they may contain "?" or "%".
        for token in self.tokens:
            if token.kind is TokenKind.COMMENT:
                token.out = " "
        self.sig = [token for token in self.tokens if token.kind not in (TokenKind.SPACE, TokenKind.COMMENT)]
        self.kind = StatementKind.EMPTY
        self.insert_table: str | None = None
        self.replace: ReplaceClause | None = None
        self.rowid_table: str | None = None
        self.null_orderings: list[NullOrdering] = []
        self.named = False
        self.positional = False
        self.numbered = False

    # ── helpers ──

    def refuse(self, reason: str) -> UntranslatableSQL:
        return UntranslatableSQL(reason, self.source)

    def word(self, index: int) -> str:
        return self.sig[index].upper if 0 <= index < len(self.sig) else ""

    def matching_paren(self, index: int) -> int:
        depth = 0
        for position in range(index, len(self.sig)):
            token = self.sig[position]
            if token.is_op("("):
                depth += 1
            elif token.is_op(")"):
                depth -= 1
                if depth == 0:
                    return position
        raise self.refuse("unbalanced parentheses")

    def opening_paren(self, index: int) -> int:
        depth = 0
        for position in range(index, -1, -1):
            token = self.sig[position]
            if token.is_op(")"):
                depth += 1
            elif token.is_op("("):
                depth -= 1
                if depth == 0:
                    return position
        raise self.refuse("unbalanced parentheses")

    def depths(self) -> list[int]:
        result, depth = [], 0
        for token in self.sig:
            if token.is_op(")"):
                depth -= 1
            result.append(depth)
            if token.is_op("("):
                depth += 1
        return result

    # ── driver ──

    def run(self) -> Translation:
        self._check_single_statement()
        if not self.sig:
            return Translation(source=self.source, sql="", kind=StatementKind.EMPTY)
        self._classify()
        if self.kind is StatementKind.PRAGMA:
            return Translation(source=self.source, sql="", kind=self.kind, pragma=self._pragma())
        if self.kind is StatementKind.LAST_INSERT_ROWID:
            return Translation(source=self.source, sql="", kind=self.kind)
        self._refuse_unsupported()
        param_count = self._assign_parameters()
        self._transaction_control()
        self._null_ordering()
        self._conflict_clauses()
        self._identity_columns()
        self._rewrite_tokens()
        self._rowid()
        order = self._parameter_order(param_count)
        placeholders = [token for token in self.tokens if token.kind is TokenKind.PARAM]
        text_parameters = tuple(
            token.param if self.named else position
            for position, token in enumerate(placeholders) if token.untyped_context)
        return Translation(
            source=self.source, sql=self._render(), kind=self.kind, param_count=param_count, param_order=order,
            named=self.named, insert_table=self.insert_table, replace=self.replace, rowid_table=self.rowid_table,
            null_orderings=tuple(self.null_orderings),
            tables=self._table_aliases() if self.null_orderings else (),
            has_returning=any(token.upper == "RETURNING" for token in self._depth_zero()),
            text_parameters=text_parameters,
        )

    def _check_single_statement(self) -> None:
        if _is_trigger([token.upper for token in self.sig[:4]]):
            raise self.refuse("SQLite triggers need a PL/pgSQL port in the PostgreSQL schema")
        for index, token in enumerate(self.sig):
            if token.is_op(";"):
                if any(not rest.is_op(";") for rest in self.sig[index + 1:]):
                    raise sqlite3.ProgrammingError("You can only execute one statement at a time.")
                for rest in self.sig[index:]:
                    rest.out = ""
                self.sig = self.sig[:index]
                return

    def _depth_zero(self) -> list[_Token]:
        return [token for token, depth in zip(self.sig, self.depths(), strict=True) if depth == 0]

    def _classify(self) -> None:
        first = self.word(0)
        if first == "WITH":
            for token in self._depth_zero()[1:]:
                if token.upper in ("SELECT", "INSERT", "UPDATE", "DELETE", "REPLACE"):
                    self.kind = StatementKind.SELECT if token.upper == "SELECT" else StatementKind.OTHER
                    break
            else:
                self.kind = StatementKind.OTHER
            if self.kind is StatementKind.OTHER:
                # sqlite3 opens implicit transactions only for statements that start with DML.
                self._with_dml()
            return
        simple = {
            "SELECT": StatementKind.SELECT, "VALUES": StatementKind.SELECT, "INSERT": StatementKind.INSERT,
            "REPLACE": StatementKind.INSERT, "UPDATE": StatementKind.UPDATE, "DELETE": StatementKind.DELETE,
            "BEGIN": StatementKind.BEGIN, "COMMIT": StatementKind.COMMIT, "END": StatementKind.COMMIT,
            "ROLLBACK": StatementKind.ROLLBACK, "SAVEPOINT": StatementKind.SAVEPOINT,
            "RELEASE": StatementKind.RELEASE, "PRAGMA": StatementKind.PRAGMA,
        }
        self.kind = simple.get(first, StatementKind.DDL if first in DDL_WORDS else StatementKind.OTHER)
        if self.kind is StatementKind.ROLLBACK and any(token.upper == "TO" for token in self.sig):
            self.kind = StatementKind.ROLLBACK_TO
        if self.kind is StatementKind.SELECT and self._is_last_insert_rowid():
            self.kind = StatementKind.LAST_INSERT_ROWID

    def _with_dml(self) -> None:
        for index, token in enumerate(self.sig):
            if self.depths()[index] == 0 and token.upper in ("INSERT", "REPLACE"):
                self._insert_at(index)
                return

    def _is_last_insert_rowid(self) -> bool:
        words = [token.upper or token.text for token in self.sig]
        return words[:4] == ["SELECT", "LAST_INSERT_ROWID", "(", ")"] and len(words) == 4

    def _pragma(self) -> PragmaCall:
        position = 3 if len(self.sig) > 3 and self.sig[2].is_op(".") else 1
        if position >= len(self.sig):
            raise self.refuse("PRAGMA without a name")
        name = self.sig[position].text.lower()
        rest = self.sig[position + 1:]
        if not rest:
            return PragmaCall(name=name, argument=None, assignment=False)
        if rest[0].is_op("="):
            return PragmaCall(name=name, argument=_literal("".join(token.text for token in rest[1:])),
                              assignment=True)
        if rest[0].is_op("(") and rest[-1].is_op(")"):
            return PragmaCall(name=name, argument=_literal("".join(token.text for token in rest[1:-1])),
                              assignment=False)
        raise self.refuse(f"malformed PRAGMA {name}")

    def _refuse_unsupported(self) -> None:
        words = [token.upper for token in self.sig]
        if words[:2] == ["CREATE", "VIRTUAL"]:
            raise self.refuse("CREATE VIRTUAL TABLE (FTS5) needs the PostgreSQL schema")
        head = [word for word in words[:4] if word not in ("TEMP", "TEMPORARY")]
        if head[:2] in (["CREATE", "TRIGGER"], ["DROP", "TRIGGER"]):
            raise self.refuse("SQLite triggers need a PL/pgSQL port in the PostgreSQL schema")
        if words[0] in ("ATTACH", "DETACH"):
            raise self.refuse(f"{words[0]} DATABASE")
        if words[0] == "VACUUM" and "INTO" in words:
            raise self.refuse("VACUUM INTO")
        if words[0] == "UPDATE" and self.word(1) == "OR" and self.word(2) in ("IGNORE", "REPLACE"):
            raise self.refuse(f"UPDATE OR {self.word(2)}")
        for index, token in enumerate(self.sig):
            if token.kind is TokenKind.WORD:
                upper = token.upper
                if upper == "MATCH":
                    raise self.refuse("FTS5 MATCH")
                if upper == "REGEXP":
                    raise self.refuse("REGEXP")
                if upper in FTS_FUNCTIONS and index + 1 < len(self.sig) and self.sig[index + 1].is_op("("):
                    raise self.refuse(f"FTS5 {upper.lower()}()")
                if FTS_TABLE.match(token.text):
                    raise self.refuse(f"FTS5 table {token.text}")
                if upper == "COLLATE" and self.word(index + 1) in UNSUPPORTED_COLLATIONS:
                    raise self.refuse(f"COLLATE {self.word(index + 1)}")
            elif token.kind is TokenKind.IDENT and FTS_TABLE.match(token.text[1:-1]):
                raise self.refuse(f"FTS5 table {token.text}")
            elif token.kind is TokenKind.PARAM and token.text[0] in "@$":
                raise self.refuse(f"parameter style {token.text[0]}name")

    def _transaction_control(self) -> None:
        if self.kind is StatementKind.BEGIN:
            if self.word(1) in POSTGRES_TRANSACTION_MODES:
                # PostgreSQL transaction modes (control plane: SERIALIZABLE) pass through.
                return
            for token in self.sig[1:]:
                if token.upper not in TRANSACTION_MODES and token.upper != "TRANSACTION":
                    raise self.refuse(f"BEGIN {token.text}")
                token.out = ""
            self.sig[0].out = "BEGIN"
            self._drop_spaces_after(self.sig[0])
        elif self.kind is StatementKind.COMMIT and self.word(0) == "END":
            self.sig[0].out = "COMMIT"

    def _drop_spaces_after(self, anchor: _Token) -> None:
        start = self.tokens.index(anchor) + 1
        for token in self.tokens[start:]:
            if token.kind is TokenKind.SPACE:
                token.out = ""

    # ── INSERT / UPDATE conflict algorithms ──

    def _conflict_clauses(self) -> None:
        first = self.word(0)
        if (first == "REPLACE" and self.kind is StatementKind.INSERT) or first == "INSERT":
            self._insert_at(0)
        elif first == "UPDATE" and self.word(1) == "OR" and self.word(2) in CONFLICT_ALGORITHMS:
            self.sig[1].out = ""
            self.sig[2].out = ""

    def _insert_at(self, start: int) -> None:
        head = self.sig[start]
        algorithm = None
        position = start + 1
        if head.upper == "REPLACE":
            head.out = "INSERT"
            algorithm = "REPLACE"
        elif self.word(position) == "OR" and self.word(position + 1) in CONFLICT_ALGORITHMS:
            algorithm = self.word(position + 1)
            self.sig[position].out = ""
            self.sig[position + 1].out = ""
            position += 2
        if self.word(position) != "INTO":
            raise self.refuse("INSERT without INTO")
        position += 1
        table_token = self.sig[position]
        if position + 2 < len(self.sig) and self.sig[position + 1].is_op("."):
            if table_token.text.lower() == "main":
                table_token.out = ""
                self.sig[position + 1].out = ""
            position += 2
            table_token = self.sig[position]
        table = table_token.text[1:-1].lower() if table_token.kind is TokenKind.IDENT else table_token.text.lower()
        self.insert_table = table
        position += 1
        if self.word(position) == "AS":
            position += 2
        columns: tuple[str, ...] | None = None
        if position < len(self.sig) and self.sig[position].is_op("("):
            close = self.matching_paren(position)
            columns = tuple(
                (token.text[1:-1] if token.kind is TokenKind.IDENT else token.text).lower()
                for token in self.sig[position + 1:close] if not token.is_op(","))
        if algorithm in (None, "ABORT", "FAIL", "ROLLBACK"):
            return
        if any(token.upper == "CONFLICT" for token in self.sig[position:]):
            raise self.refuse(f"INSERT OR {algorithm} combined with an ON CONFLICT clause")
        clause = " ON CONFLICT DO NOTHING" if algorithm == "IGNORE" else _CONFLICT_MARKER
        if algorithm == "REPLACE":
            self.replace = ReplaceClause(table=table, columns=columns)
        self._append_before_returning(start, clause)

    def _append_before_returning(self, start: int, clause: str) -> None:
        depths = self.depths()
        base = depths[start]
        for index in range(start, len(self.sig)):
            if depths[index] == base and self.sig[index].upper == "RETURNING":
                self.sig[index].out = clause.lstrip() + " " + self.sig[index].out
                return
            if depths[index] < base:
                self.sig[index].out = clause + self.sig[index].out
                return
        self.sig[-1].suffix += clause

    # ── DDL ──

    def _identity_columns(self) -> None:
        if self.kind is not StatementKind.DDL:
            return
        words = [token.upper for token in self.sig]
        for index in range(len(words) - 2):
            if words[index:index + 3] == ["INTEGER", "PRIMARY", "KEY"]:
                self.sig[index].out = IDENTITY_PRIMARY_KEY
                self.sig[index + 1].out = ""
                self.sig[index + 2].out = ""
                self._drop_spaces_between(index, index + 2)
                follow = index + 3
                if words[follow:follow + 1] in (["ASC"], ["DESC"]):
                    self.sig[follow].out = ""
                    follow += 1
                if words[follow:follow + 1] == ["AUTOINCREMENT"]:
                    self.sig[follow].out = ""
        if words[:2] == ["CREATE", "TABLE"] or words[:3] in (["CREATE", "TEMP", "TABLE"],
                                                              ["CREATE", "TEMPORARY", "TABLE"]):
            depths = self.depths()
            last_close = max((index for index, token in enumerate(self.sig)
                              if token.is_op(")") and depths[index] == 0), default=None)
            if last_close is not None:
                for index in range(last_close + 1, len(self.sig)):
                    if words[index] in ("WITHOUT", "ROWID", "STRICT") or self.sig[index].is_op(","):
                        self.sig[index].out = ""

    def _drop_spaces_between(self, first: int, last: int) -> None:
        start = self.tokens.index(self.sig[first])
        end = self.tokens.index(self.sig[last])
        for token in self.tokens[start:end]:
            if token.kind is TokenKind.SPACE:
                token.out = ""

    # ── token rewrites ──

    def _rewrite_tokens(self) -> None:
        index = 0
        while index < len(self.sig):
            token = self.sig[index]
            upper = token.upper
            if token.is_op("=="):
                token.out = "="
            elif token.kind is TokenKind.BLOB:
                token.out = "'\\x" + token.text[2:-1].lower() + "'::bytea"
            elif upper in TYPE_WORDS and token.out == token.text and self._is_type_position(index):
                token.out = TYPE_WORDS[upper]
            elif upper == "AUTOINCREMENT":
                token.out = ""
            elif upper in CURRENT_WORDS and not self._is_column_reference(index):
                token.out = CURRENT_WORDS[upper]
            elif upper == "SQLITE_SCHEMA":
                token.out = "sqlite_master"
            elif upper == "IS":
                self._is(index)
            elif upper == "LIKE":
                self._like(index)
            elif upper == "GLOB" and not (self._next_is_paren(index) and self._is_unary(index)):
                self._glob(index)
            elif upper == "CROSS" and self.word(index + 1) == "JOIN" and self._join_has_condition(index + 1):
                # SQLite: CROSS JOIN ... ON is an inner join that fixes the join order.
                token.out = ""
                self._drop_spaces_between(index, index + 1)
            elif upper == "INDEXED" and self.word(index + 1) == "BY":
                token.out = self.sig[index + 1].out = self.sig[index + 2].out = ""
            elif upper == "NOT" and self.word(index + 1) == "INDEXED":
                token.out = self.sig[index + 1].out = ""
            elif upper == "LIMIT":
                self._limit(index)
            elif upper == "IN" and self._empty_list(index):
                pass
            elif upper == "JSON_OBJECT" and self._next_is_paren(index):
                close = self.matching_paren(index + 1)
                token.out = f"{COMPAT_SCHEMA}.json_compact(json_build_object"
                self.sig[close].out = "))"
                for inner in self.sig[index + 2:close]:
                    if inner.kind is TokenKind.PARAM:
                        inner.untyped_context = True
            elif upper == "CHAR" and self._next_is_paren(index):
                self._char(index)
            elif upper in COMPAT_FUNCTIONS and self._next_is_paren(index) and not self._is_column_reference(index) or upper in SCALAR_MAX_MIN and self._next_is_paren(index) and not self._is_column_reference(index) \
                    and self._argument_count(index + 1) > 1:
                token.out = f"{COMPAT_SCHEMA}.{upper.lower()}"
            elif upper == "COLLATE" and self.word(index + 1) == "BINARY":
                self.sig[index + 1].out = '"C"'
            elif token.is_op("+") and self._is_unary(index) and index + 1 < len(self.sig) \
                    and self.sig[index + 1].kind in (TokenKind.WORD, TokenKind.IDENT):
                token.out = ""
            index += 1

    def _is_type_position(self, index: int) -> bool:
        """A type name follows ``AS`` inside CAST( or a column name in a column definition."""
        previous = self.sig[index - 1] if index > 0 else None
        if previous is None:
            return False
        if previous.upper == "AS":
            opening = self._enclosing_paren(index)
            return opening is not None and opening > 0 and self.word(opening - 1) == "CAST"
        if self.kind is not StatementKind.DDL or previous.kind not in (TokenKind.WORD, TokenKind.IDENT):
            return False
        before = self.sig[index - 2] if index > 1 else None
        return before is not None and (before.is_op("(", ",") or before.upper in ("COLUMN", "ADD"))

    def _enclosing_paren(self, index: int) -> int | None:
        depth = 0
        for position in range(index - 1, -1, -1):
            token = self.sig[position]
            if token.is_op(")"):
                depth += 1
            elif token.is_op("("):
                if depth == 0:
                    return position
                depth -= 1
        return None

    def _join_has_condition(self, join: int) -> bool:
        depths = self.depths()
        for position in range(join + 1, len(self.sig)):
            if depths[position] < depths[join]:
                return False
            if depths[position] > depths[join]:
                continue
            word = self.sig[position].upper
            if word in ("ON", "USING"):
                return True
            if word in JOIN_END_WORDS or self.sig[position].is_op(",", ";"):
                return False
        return False

    def _next_is_paren(self, index: int) -> bool:
        return index + 1 < len(self.sig) and self.sig[index + 1].is_op("(")

    def _is_column_reference(self, index: int) -> bool:
        return index > 0 and self.sig[index - 1].is_op(".")

    def _is_unary(self, index: int) -> bool:
        if index == 0:
            return True
        previous = self.sig[index - 1]
        if previous.kind is TokenKind.OP:
            return previous.text in UNARY_CONTEXT_OPS
        return previous.upper in UNARY_CONTEXT_WORDS

    def _is(self, index: int) -> None:
        following = index + 1
        negated = self.word(following) == "NOT"
        if negated:
            following += 1
        if self.word(following) in IS_KEEP:
            return
        self.sig[index].out = "IS DISTINCT FROM" if negated else "IS NOT DISTINCT FROM"
        if negated:
            self.sig[index + 1].out = ""
            self._drop_spaces_between(index, index + 1)

    def _operand_end(self, start: int) -> int:
        """Index of the last token of the expression that starts at ``start``."""
        depth = 0
        end = start
        for position in range(start, len(self.sig)):
            token = self.sig[position]
            if depth == 0 and position > start and (
                    token.upper in OPERAND_STOP_WORDS or token.text in OPERAND_STOP_OPS
                    and token.kind is TokenKind.OP):
                break
            if token.is_op("("):
                depth += 1
            elif token.is_op(")"):
                if depth == 0:
                    break
                depth -= 1
            end = position
        return end

    def _like(self, index: int) -> None:
        self.sig[index].out = "ILIKE"
        end = self._operand_end(index + 1)
        if self.word(end + 1) != "ESCAPE":
            self.sig[end].out += " ESCAPE ''"

    def _operand_start(self, index: int) -> int:
        """Index of the first token of the simple operand that ends at ``index``."""
        token = self.sig[index]
        if token.is_op(")"):
            start = self.opening_paren(index)
            if start > 0 and self.sig[start - 1].kind is TokenKind.WORD:
                start -= 1
            return start
        if token.kind in (TokenKind.WORD, TokenKind.IDENT, TokenKind.STRING, TokenKind.NUMBER, TokenKind.PARAM):
            start = index
            while start >= 2 and self.sig[start - 1].is_op(".") and \
                    self.sig[start - 2].kind in (TokenKind.WORD, TokenKind.IDENT):
                start -= 2
            return start
        raise self.refuse("GLOB with an unsupported left operand")

    def _glob(self, index: int) -> None:
        """``a [NOT] GLOB b`` -> ``[NOT] tam_compat.glob_match(a, b)`` (= SQLite glob(b, a))."""
        negated = self.word(index - 1) == "NOT"
        left_end = index - 2 if negated else index - 1
        if left_end < 0:
            raise self.refuse("GLOB without a left operand")
        left_start = self._operand_start(left_end)
        right_end = self._operand_end(index + 1)
        if negated:
            self.sig[index - 1].out = ""
        self.sig[left_start].out = ("NOT " if negated else "") + f"{COMPAT_SCHEMA}.glob_match(" \
            + self.sig[left_start].out
        self.sig[index].out = ","
        self.sig[right_end].out += ")"

    def _limit(self, index: int) -> None:
        following = self.sig[index + 1: index + 4]
        if len(following) >= 2 and following[0].is_op("-") and following[1].text == "1":
            following[0].out = "ALL"
            following[1].out = ""
            return
        if len(following) < 3 or not following[1].is_op(","):
            return
        offset, count = following[0], following[2]
        if not (self._simple(offset) and self._simple(count)):
            raise self.refuse("LIMIT offset, count with expressions")
        self.sig[index + 1], self.sig[index + 3] = count, offset
        first, last = self.tokens.index(offset), self.tokens.index(count)
        self.tokens[first], self.tokens[last] = count, offset
        following[1].out = " OFFSET"

    @staticmethod
    def _simple(token: _Token) -> bool:
        return token.kind in (TokenKind.NUMBER, TokenKind.PARAM)

    def _empty_list(self, index: int) -> bool:
        if index + 2 < len(self.sig) and self.sig[index + 1].is_op("(") and self.sig[index + 2].is_op(")"):
            negated = self.word(index - 1) == "NOT"
            if negated:
                self.sig[index - 1].out = ""
            self.sig[index].out = "<> ALL('{}')" if negated else "= ANY('{}')"
            self.sig[index + 1].out = self.sig[index + 2].out = ""
            return True
        return False

    def _argument_count(self, opening: int) -> int:
        close = self.matching_paren(opening)
        if close == opening + 1:
            return 0
        depth = 0
        count = 1
        for position in range(opening + 1, close):
            token = self.sig[position]
            if token.is_op("("):
                depth += 1
            elif token.is_op(")"):
                depth -= 1
            elif token.is_op(",") and depth == 0:
                count += 1
        return count

    def _char(self, index: int) -> None:
        close = self.matching_paren(index + 1)
        depth = 0
        commas = []
        for position in range(index + 2, close):
            token = self.sig[position]
            if token.is_op("("):
                depth += 1
            elif token.is_op(")"):
                depth -= 1
            elif token.is_op(",") and depth == 0:
                commas.append(position)
        if not commas:
            self.sig[index].out = "chr"
            return
        self.sig[index].out = "(chr"
        for comma in commas:
            self.sig[comma].out = ") || chr("
        self.sig[close].out = "))"

    # ── rowid ──

    # ── ORDER BY ──

    def _null_ordering(self) -> None:
        """SQLite sorts NULL as the smallest value: every ORDER BY term without an explicit
        NULLS clause gets NULLS FIRST (ASC) or NULLS LAST (DESC). Covers statement, window
        and aggregate ORDER BY clauses alike."""
        depths = self.depths()
        for index, token in enumerate(self.sig[:-1]):
            if token.upper != "ORDER" or self.word(index + 1) != "BY":
                continue
            depth = depths[index]
            term: list[int] = []
            for position in range(index + 2, len(self.sig) + 1):
                if position == len(self.sig):
                    self._order_term(term, depths, depth)
                    break
                current = self.sig[position]
                if depths[position] < depth or (depths[position] == depth and (
                        current.upper in ORDER_BY_END_WORDS or current.is_op(";"))):
                    self._order_term(term, depths, depth)
                    break
                if depths[position] == depth and current.is_op(","):
                    self._order_term(term, depths, depth)
                    term = []
                    continue
                term.append(position)

    def _order_term(self, term: list[int], depths: list[int], depth: int) -> None:
        if not term:
            return
        words = [self.sig[position].upper for position in term if depths[position] == depth]
        if "NULLS" in words:
            return
        descending = bool(words) and words[-1] == "DESC"
        clause = " NULLS LAST" if descending else " NULLS FIRST"
        column = self._bare_column([position for position in term if depths[position] == depth])
        if column is None:
            self.sig[term[-1]].suffix += clause
            return
        marker = f"\x00tam_nulls_{len(self.null_orderings)}\x00"
        self.null_orderings.append(NullOrdering(marker=marker, qualifier=column[0], column=column[1], clause=clause))
        self.sig[term[-1]].suffix += marker

    def _bare_column(self, positions: list[int]) -> tuple[str | None, str] | None:
        """(qualifier, column) when the term is ``[q.]column [COLLATE x] [ASC|DESC]``."""
        tokens = [self.sig[position] for position in positions]
        if tokens and tokens[-1].upper in ("ASC", "DESC"):
            tokens = tokens[:-1]
        if len(tokens) >= 2 and tokens[-2].upper == "COLLATE":
            tokens = tokens[:-2]
        names = [_name(token) for token in tokens[::2]]
        if any(name is None for name in names) or any(not token.is_op(".") for token in tokens[1::2]):
            return None
        if len(tokens) == 1:
            return None, names[0]
        if len(tokens) == 3:
            return names[0], names[1]
        return None

    def _table_aliases(self) -> tuple[tuple[str, str], ...]:
        """(alias, table) for every table reference; a table is also its own alias."""
        pairs: set[tuple[str, str]] = set()
        depths = self.depths()
        for index, token in enumerate(self.sig[:-1]):
            if token.upper not in ("FROM", "JOIN", "UPDATE", "INTO"):
                continue
            starts = [index + 1]
            if token.upper == "FROM":
                for position in range(index + 1, len(self.sig)):
                    current = self.sig[position]
                    if depths[position] < depths[index] or depths[position] == depths[index] and (
                            current.upper in OPERAND_STOP_WORDS or current.is_op(";")):
                        break
                    if depths[position] == depths[index] and current.is_op(","):
                        starts.append(position + 1)
            for start in starts:
                table = _name(self.sig[start]) if start < len(self.sig) else None
                if table is None or table.upper() in ("SELECT", "OR"):
                    continue
                pairs.add((table, table))
                follow = start + 1
                if self.word(follow) == "AS":
                    follow += 1
                alias = _name(self.sig[follow]) if follow < len(self.sig) else None
                if alias is not None and alias.upper() not in ALIAS_STOP_WORDS:
                    pairs.add((alias, table))
        return tuple(sorted(pairs))

    def _rowid(self) -> None:
        rowid_tokens = [token for token in self.sig if token.upper in ROWID_WORDS and token.out == token.text]
        if not rowid_tokens:
            return
        tables = self._referenced_tables()
        if len(tables) != 1:
            raise self.refuse("rowid in a statement that references several tables")
        self.rowid_table = tables.pop()
        for token in rowid_tokens:
            token.out = _ROWID_MARKER

    def _referenced_tables(self) -> set[str]:
        tables: set[str] = set()
        depths = self.depths()
        for index, token in enumerate(self.sig[:-1]):
            if token.upper not in ("FROM", "JOIN", "INTO", "UPDATE", "TABLE"):
                continue
            self._add_table(tables, index + 1)
            if token.upper != "FROM":
                continue
            # FROM a, b: comma-separated tables at the FROM's depth.
            for position in range(index + 1, len(self.sig) - 1):
                current = self.sig[position]
                if depths[position] < depths[index] or depths[position] == depths[index] and (
                        current.upper in OPERAND_STOP_WORDS or current.is_op(";")):
                    break
                if depths[position] == depths[index] and current.is_op(","):
                    self._add_table(tables, position + 1)
        return tables

    def _add_table(self, tables: set[str], index: int) -> None:
        target = self.sig[index]
        if target.kind is TokenKind.WORD and target.upper not in ("SELECT", "OR"):
            tables.add(target.text.lower())
        elif target.kind is TokenKind.IDENT:
            tables.add(target.text[1:-1].lower())

    # ── parameters and output ──

    def _assign_parameters(self) -> int:
        """Placeholders become psycopg ones before any rewrite, so rewrites may append to them."""
        highest = 0
        self.numbered = False
        for token in self.sig:
            if token.kind is not TokenKind.PARAM:
                continue
            if token.text[0] == ":":
                self.named = True
                token.param = token.text[1:]
                token.out = f"%({token.text[1:]})s"
                continue
            self.positional = True
            if len(token.text) > 1:
                number = int(token.text[1:])
                if number < 1:
                    raise sqlite3.ProgrammingError(f"parameter number out of range: {token.text}")
                self.numbered = True
                highest = max(highest, number)
                token.param = number - 1
            else:
                highest += 1
                token.param = highest - 1
            token.out = "%s"
        if self.named and self.positional:
            raise self.refuse("mixed named and positional parameters")
        return highest if self.positional else 0

    def _parameter_order(self, param_count: int) -> tuple[int, ...] | None:
        if not self.positional:
            return None
        order = tuple(token.param for token in self.tokens
                      if token.kind is TokenKind.PARAM and isinstance(token.param, int))
        if order != tuple(range(param_count)) or self.numbered:
            return order
        return None

    def _render(self) -> str:
        parts = []
        for token in self.tokens:
            if token.kind is TokenKind.PARAM:
                parts.append(token.out)
            else:
                parts.append(token.out.replace("%", "%%"))
            parts.append(token.suffix)
        return "".join(parts).strip()


def _name(token: _Token) -> str | None:
    """Lower-case identifier of a word or quoted identifier token, None otherwise."""
    if token.kind is TokenKind.WORD:
        return token.text.lower()
    if token.kind is TokenKind.IDENT:
        return token.out[1:-1].replace('""', '"')
    return None


def _literal(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return text[1:-1].replace(text[0] * 2, text[0])
    return text

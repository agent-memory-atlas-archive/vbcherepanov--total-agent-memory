"""FTS5 MATCH expressions and bm25() ranking on PostgreSQL.

The workspace baseline (migrations/postgres/workspace/0001_baseline.sql) replaces
each FTS5 table with a side table ``<table>_tsv`` holding, per indexed column, the
tokens FTS5's unicode61 tokenizer would produce, plus the row's token count and
table-wide totals in ``fts_stats``. This module compiles the subset of the FTS5
query language TAM generates (memory_core.query_terms, dedup, fts_schema,
episodes.retriever, Store._fts_escape) into SQL over those tables:

* phrases (``"a b"``, barewords, ``+`` concatenation) with an optional trailing
  ``*`` prefix, implicit AND, ``AND``, ``OR``, ``NOT`` and parentheses;
* column filters ``col : x``, ``{a b} : (x)`` and ``- col : x``.

Phrases are tokenized with SQLite's own FTS5 tokenizer, so query tokens are exactly
the ones FTS5 would look up; phrases without tokens are dropped from the expression
as FTS5 drops them. Ranking is FTS5's bm25(): Okapi BM25 with k1=1.2, b=0.75, an
idf of ln((N - n + 0.5) / (n + 0.5)) floored at 1e-6, per-column weights applied to
phrase frequencies, and document length over all columns. Scores are negated like
bm25(), so lower is better.

``match_source(table, expression, weights=...)`` returns an SQL subquery with
columns ``id`` (the FTS5 rowid) and ``rank`` (the bm25() value) for every matching
row, plus its parameters, written in the ``?`` style the tam_db translator accepts.
Callers join it to the content table and add their own filters and ORDER BY.
``rebuild(connection)`` is FTS5's 'rebuild' for all side tables.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache

from tam_db.contracts import PgOperationalError

BM25_K1 = 1.2
BM25_B = 0.75
# FTS5 replaces a non-positive idf (a term in more than half the rows) with this.
BM25_MIN_IDF = 1e-6
DEFAULT_WEIGHT = 1.0
TOKEN_CACHE_SIZE = 4096
# tam_fts_tokens() cuts document tokens to this many characters (tsvector limit).
MAX_TOKEN_CHARS = 500

KEYWORDS = frozenset({"AND", "OR", "NOT"})
_WHITESPACE = " \t\n\r\f\v"
_PUNCTUATION = "(){}:*+-^,"


class FtsQueryError(PgOperationalError):
    """The MATCH expression is malformed or uses FTS5 syntax this compiler does not support.

    A sqlite3.OperationalError, like FTS5's own "fts5: syntax error", so callers that
    degrade on bad expressions keep doing so.
    """


class ColumnKind(Enum):
    TOKENS = "tokens"
    """text[] of tokens plus a stored tsvector ``<name>_lexemes`` with a GIN index."""
    TOKEN = "token"
    """A single-token column stored as text with a btree index (knowledge.fts_project)."""


@dataclass(frozen=True)
class FtsColumn:
    name: str
    kind: ColumnKind = ColumnKind.TOKENS


@dataclass(frozen=True)
class FtsSource:
    """An FTS5 table and the side table that replaces it on PostgreSQL."""

    fts_table: str
    side_table: str
    columns: tuple[FtsColumn, ...]

    def column(self, name: str) -> FtsColumn:
        for column in self.columns:
            if column.name.lower() == name.lower():
                return column
        raise FtsQueryError(f"no such column: {name}")


FTS_SOURCES: dict[str, FtsSource] = {
    source.fts_table: source
    for source in (
        FtsSource("knowledge_fts", "knowledge_tsv", (
            FtsColumn("content"), FtsColumn("context"), FtsColumn("tags"),
            FtsColumn("fts_project", ColumnKind.TOKEN),
        )),
        FtsSource("errors_fts", "errors_tsv", (
            FtsColumn("description"), FtsColumn("context"), FtsColumn("fix"), FtsColumn("tags"),
        )),
        FtsSource("atomic_facts_fts", "atomic_facts_tsv", (FtsColumn("content"),)),
        FtsSource("evidence_passages_fts", "evidence_passages_tsv", (FtsColumn("content"),)),
        FtsSource("episodes_v11_fts", "episodes_v11_tsv", (
            FtsColumn("summary"), FtsColumn("participants"), FtsColumn("outcome"),
        )),
    )
}


# ─── Query tokenization (SQLite's own unicode61 tokenizer) ──────────────────

class _Fts5Tokenizer:
    """Tokenizes phrase text with an in-memory FTS5 table and fts5vocab."""

    def __init__(self) -> None:
        self._db = sqlite3.connect(":memory:", check_same_thread=False)
        self._db.execute("CREATE VIRTUAL TABLE phrase USING fts5(text)")
        self._db.execute("CREATE VIRTUAL TABLE phrase_terms USING fts5vocab(phrase, 'instance')")
        self._lock = threading.Lock()

    def tokens(self, text: str) -> tuple[str, ...]:
        with self._lock:
            self._db.execute("INSERT INTO phrase(rowid, text) VALUES (1, ?)", (text,))
            try:
                rows = self._db.execute("SELECT term FROM phrase_terms ORDER BY offset").fetchall()
            finally:
                self._db.execute("DELETE FROM phrase")
        return tuple(row[0][:MAX_TOKEN_CHARS] for row in rows)


_TOKENIZER = _Fts5Tokenizer()


@lru_cache(maxsize=TOKEN_CACHE_SIZE)
def fts5_tokens(text: str) -> tuple[str, ...]:
    """The tokens FTS5 (unicode61, remove_diacritics=1) produces for ``text``."""
    return _TOKENIZER.tokens(text)


# ─── Parsing ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Phrase:
    tokens: tuple[str, ...]
    prefix: bool
    columns: frozenset[str] | None = None
    """Lower-case column names the phrase may match in; None = every column."""


@dataclass(frozen=True)
class And:
    children: tuple[Node, ...]


@dataclass(frozen=True)
class Or:
    children: tuple[Node, ...]


@dataclass(frozen=True)
class Not:
    include: Node
    exclude: Node


Node = Phrase | And | Or | Not


@dataclass(frozen=True)
class _Token:
    kind: str  # "string", "word", or one punctuation character
    text: str
    position: int


def _lex(expression: str) -> list[_Token]:
    tokens: list[_Token] = []
    index = 0
    length = len(expression)
    while index < length:
        char = expression[index]
        if char in _WHITESPACE:
            index += 1
        elif char == '"':
            start, index, parts = index, index + 1, []
            while True:
                end = expression.find('"', index)
                if end < 0:
                    raise FtsQueryError(f'fts5: syntax error near "{expression[start:]}"')
                parts.append(expression[index:end])
                if expression.startswith('"', end + 1):
                    parts.append('"')
                    index = end + 2
                    continue
                index = end + 1
                break
            tokens.append(_Token("string", "".join(parts), start))
        elif char in _PUNCTUATION:
            tokens.append(_Token(char, char, index))
            index += 1
        elif _is_bareword(char):
            start = index
            while index < length and _is_bareword(expression[index]):
                index += 1
            tokens.append(_Token("word", expression[start:index], start))
        else:
            raise FtsQueryError(f'fts5: syntax error near "{char}"')
    return tokens


def _is_bareword(char: str) -> bool:
    # sqlite3Fts5IsBareword: ASCII letters and digits, "_", 0x1A and all non-ASCII.
    return char.isascii() and (char.isalnum() or char in "_\x1a") or not char.isascii()


class _Parser:
    """Recursive descent over FTS5's precedence: OR < AND < NOT < implicit AND."""

    def __init__(self, expression: str, source: FtsSource):
        self.expression = expression
        self.source = source
        self.tokens = _lex(expression)
        self.index = 0

    def parse(self) -> Node | None:
        if not self.tokens:
            raise FtsQueryError('fts5: syntax error near ""')
        node = self._or(None)
        if self.index != len(self.tokens):
            self._fail()
        return node

    def _peek(self) -> _Token | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def _fail(self) -> None:
        token = self._peek()
        near = "" if token is None else self.expression[token.position:token.position + max(len(token.text), 1)]
        raise FtsQueryError(f'fts5: syntax error near "{near}"')

    def _keyword(self, word: str) -> bool:
        token = self._peek()
        if token is not None and token.kind == "word" and token.text == word:
            self.index += 1
            return True
        return False

    def _expect(self, kind: str) -> _Token:
        token = self._peek()
        if token is None or token.kind != kind:
            self._fail()
        self.index += 1
        return token

    def _or(self, columns: frozenset[str] | None) -> Node | None:
        children = [self._and(columns)]
        while self._keyword("OR"):
            children.append(self._and(columns))
        return _or(children)

    def _and(self, columns: frozenset[str] | None) -> Node | None:
        children = [self._not(columns)]
        while self._keyword("AND"):
            children.append(self._not(columns))
        return _and(children)

    def _not(self, columns: frozenset[str] | None) -> Node | None:
        node = self._sequence(columns)
        while self._keyword("NOT"):
            exclude = self._sequence(columns)
            if node is not None and exclude is not None:
                node = Not(node, exclude)
        return node

    def _sequence(self, columns: frozenset[str] | None) -> Node | None:
        children = [self._primary(columns)]
        while self._starts_primary():
            children.append(self._primary(columns))
        return _and(children)

    def _starts_primary(self) -> bool:
        token = self._peek()
        if token is None:
            return False
        if token.kind == "word":
            return token.text not in KEYWORDS
        return token.kind in ("string", "(", "{", "-")

    def _primary(self, columns: frozenset[str] | None) -> Node | None:
        token = self._peek()
        if token is None:
            self._fail()
        if token.kind == "(":
            self.index += 1
            node = self._or(columns)
            self._expect(")")
            return node
        if token.kind in ("{", "-") or (token.kind == "word" and self._next_kind() == ":"):
            restricted = _intersect(columns, self._column_filter())
            if self._peek() is not None and self._peek().kind == "(":
                self.index += 1
                node = self._or(restricted)
                self._expect(")")
                return node
            return self._phrase(restricted)
        return self._phrase(columns)

    def _next_kind(self) -> str | None:
        following = self.index + 1
        return self.tokens[following].kind if following < len(self.tokens) else None

    def _column_filter(self) -> frozenset[str]:
        names = {column.name.lower() for column in self.source.columns}
        negate = False
        token = self._peek()
        if token.kind == "-":
            negate = True
            self.index += 1
            token = self._peek()
            if token is None:
                self._fail()
        if token.kind == "{":
            self.index += 1
            selected = set()
            while self._peek() is not None and self._peek().kind in ("word", "string"):
                selected.add(self.source.column(self.tokens[self.index].text).name.lower())
                self.index += 1
            self._expect("}")
        else:
            selected = {self.source.column(self._expect("word").text).name.lower()}
        self._expect(":")
        return frozenset(names - selected if negate else selected)

    def _phrase(self, columns: frozenset[str] | None) -> Node | None:
        parts: list[str] = []
        prefix = False
        while True:
            token = self._peek()
            if token is None or token.kind not in ("string", "word"):
                self._fail()
            if token.kind == "word" and token.text == "NEAR" and self._next_kind() == "(":
                raise FtsQueryError("fts5: NEAR queries are not supported on PostgreSQL")
            if token.kind == "word" and token.text in KEYWORDS:
                self._fail()
            parts.append(token.text)
            self.index += 1
            following = self._peek()
            if following is not None and following.kind == "*":
                self.index += 1
                prefix = True
                following = self._peek()
            if following is None or following.kind != "+":
                break
            if prefix:
                raise FtsQueryError("fts5: a prefix is only supported at the end of a phrase")
            self.index += 1
        if self._peek() is not None and self._peek().kind == "^":
            raise FtsQueryError("fts5: initial-token queries are not supported on PostgreSQL")
        tokens = tuple(token for part in parts for token in fts5_tokens(part))
        if not tokens:
            return None
        return Phrase(tokens, prefix, columns)


def _intersect(outer: frozenset[str] | None, inner: frozenset[str]) -> frozenset[str]:
    return inner if outer is None else outer & inner


def _and(children: list[Node | None]) -> Node | None:
    present = tuple(child for child in children if child is not None)
    if not present:
        return None
    return present[0] if len(present) == 1 else And(present)


def _or(children: list[Node | None]) -> Node | None:
    present = tuple(child for child in children if child is not None)
    if not present:
        return None
    return present[0] if len(present) == 1 else Or(present)


def parse(expression: str, table: str) -> Node | None:
    """The expression tree FTS5 would evaluate for ``expression`` on ``table``.

    None when every phrase is empty (FTS5 then matches no row).
    """
    return _Parser(expression, _source(table)).parse()


def _source(table: str) -> FtsSource:
    try:
        return FTS_SOURCES[table]
    except KeyError:
        raise FtsQueryError(f"no PostgreSQL full-text index for {table}") from None


# ─── SQL generation ──────────────────────────────────────────────────────────

def _phrases(node: Node) -> list[Phrase]:
    if isinstance(node, Phrase):
        return [node]
    if isinstance(node, Not):
        return _phrases(node.include) + _phrases(node.exclude)
    return [phrase for child in node.children for phrase in _phrases(child)]


def _tsquery(phrase: Phrase) -> str:
    """tsquery text requiring every token of the phrase (the last as a prefix if needed)."""
    last = len(phrase.tokens) - 1
    terms = []
    for position, token in enumerate(phrase.tokens):
        quoted = "'" + token.replace("\\", "\\\\").replace("'", "''") + "'"
        terms.append(quoted + (":*" if phrase.prefix and position == last else ""))
    return " & ".join(terms)


class _Sql:
    """SQL text with its ``?`` parameters, built in step."""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.params: list[object] = []

    def add(self, text: str, *params: object) -> None:
        self.parts.append(text)
        self.params.extend(params)

    def text(self) -> str:
        return "".join(self.parts)


def _columns_of(phrase: Phrase, source: FtsSource) -> list[FtsColumn]:
    return [column for column in source.columns
            if phrase.columns is None or column.name.lower() in phrase.columns]


def _column_match(out: _Sql, alias: str, column: FtsColumn, phrase: Phrase) -> None:
    single = len(phrase.tokens) == 1
    if column.kind is ColumnKind.TOKEN:
        if not single:
            out.add("FALSE")
        elif phrase.prefix:
            out.add(f"starts_with({alias}.{column.name}, ?)", phrase.tokens[0])
        else:
            out.add(f"{alias}.{column.name} = ?", phrase.tokens[0])
        return
    out.add(f"{alias}.{column.name}_lexemes @@ CAST(? AS tsquery)", _tsquery(phrase))
    if not single:
        out.add(f" AND tam_fts_tf({alias}.{column.name}, {_TOKEN_ARRAY}, {_bool(phrase.prefix)}) > 0",
                _token_list(phrase))


def _phrase_match(out: _Sql, alias: str, phrase: Phrase, source: FtsSource) -> None:
    columns = _columns_of(phrase, source)
    if not columns:
        out.add("FALSE")
        return
    out.add("(")
    for index, column in enumerate(columns):
        if index:
            out.add(" OR ")
        out.add("(")
        _column_match(out, alias, column, phrase)
        out.add(")")
    out.add(")")


def _node_match(out: _Sql, alias: str, node: Node, source: FtsSource) -> None:
    if isinstance(node, Phrase):
        _phrase_match(out, alias, node, source)
        return
    if isinstance(node, Not):
        out.add("(")
        _node_match(out, alias, node.include, source)
        out.add(" AND NOT ")
        _node_match(out, alias, node.exclude, source)
        out.add(")")
        return
    joiner = " AND " if isinstance(node, And) else " OR "
    out.add("(")
    for index, child in enumerate(node.children):
        if index:
            out.add(joiner)
        _node_match(out, alias, child, source)
    out.add(")")


def _column_frequency(out: _Sql, alias: str, column: FtsColumn, phrase: Phrase) -> None:
    single = len(phrase.tokens) == 1
    if column.kind is ColumnKind.TOKEN:
        if not single:
            out.add("0")
        elif phrase.prefix:
            out.add(f"CASE WHEN starts_with({alias}.{column.name}, ?) THEN 1 ELSE 0 END", phrase.tokens[0])
        else:
            out.add(f"CASE WHEN {alias}.{column.name} = ? THEN 1 ELSE 0 END", phrase.tokens[0])
    elif single and not phrase.prefix:
        out.add(f"COALESCE(CAST(jsonb_extract_path_text({alias}.{column.name}_counts, ?) AS integer), 0)",
                phrase.tokens[0])
    else:
        out.add(f"tam_fts_tf({alias}.{column.name}, {_TOKEN_ARRAY}, {_bool(phrase.prefix)})",
                _token_list(phrase))


# Phrase tokens travel as one space-separated text parameter: a space is always a
# token separator, and "text[]" is not valid SQLite syntax for the translator.
_TOKEN_ARRAY = "string_to_array(CAST(? AS text), ' ')"


def _token_list(phrase: Phrase) -> str:
    return " ".join(phrase.tokens)


def _bool(value: bool) -> str:
    return "TRUE" if value else "FALSE"


def _weights_for(source: FtsSource, weights: tuple[float, ...] | None) -> dict[str, float]:
    given = tuple(weights or ())
    return {column.name: float(given[index]) if index < len(given) else DEFAULT_WEIGHT
            for index, column in enumerate(source.columns)}


def _empty_source() -> tuple[str, list[object]]:
    return "SELECT CAST(NULL AS bigint) AS id, CAST(NULL AS double precision) AS rank WHERE FALSE", []


def match_source(table: str, expression: str, *,
                 weights: tuple[float, ...] | None = None) -> tuple[str, list[object]]:
    """SQL subquery ``(id, rank)`` for the rows of ``table`` matching ``expression``.

    ``table`` is the FTS5 table name (knowledge_fts, errors_fts, atomic_facts_fts,
    evidence_passages_fts, episodes_v11_fts); ``weights`` are bm25()'s per-column
    weights in column order (missing ones are 1.0). ``rank`` equals FTS5's
    ``bm25(table, *weights)`` for the row, so ``ORDER BY rank`` is FTS5's order.
    Raises FtsQueryError (a sqlite3.OperationalError) for malformed or unsupported
    expressions.
    """
    source = _source(table)
    node = _Parser(expression, source).parse()
    if node is None:
        return _empty_source()
    column_weights = _weights_for(source, weights)
    phrases = _phrases(node)
    # A phrase whose columns all weigh 0 adds nothing to the score; skip its df count.
    scored = [
        (index, phrase, [(column, column_weights[column.name]) for column in _columns_of(phrase, source)
                         if column_weights[column.name] != 0.0])
        for index, phrase in enumerate(phrases)
    ]
    scored = [(index, phrase, columns) for index, phrase, columns in scored if columns]

    out = _Sql()
    out.add("WITH corpus AS MATERIALIZED (SELECT CAST(doc_count AS double precision) AS n, "
            "CASE WHEN doc_count > 0 THEN CAST(total_length AS double precision) / doc_count ELSE 1 END AS avgdl "
            "FROM fts_stats WHERE source = ?), ", source.side_table)
    # Document frequencies over the whole table (FTS5 counts a phrase's rows without
    # the rest of the expression), all phrases in one scan of the rows holding any.
    out.add("hits AS MATERIALIZED (SELECT ")
    if scored:
        for position, (index, phrase, _) in enumerate(scored):
            if position:
                out.add(", ")
            out.add("CAST(count(*) FILTER (WHERE ")
            _phrase_match(out, "d", phrase, source)
            out.add(f") AS double precision) AS h{index}")
        out.add(f" FROM {source.side_table} d WHERE ")
        for position, (_, phrase, _) in enumerate(scored):
            if position:
                out.add(" OR ")
            _phrase_match(out, "d", phrase, source)
    else:
        out.add("0 AS unused")
    out.add("), ")
    # Weighted phrase frequencies are computed once per matching row, then scored.
    out.add("matched AS MATERIALIZED (SELECT d.id, d.doc_length")
    for index, phrase, columns in scored:
        out.add(", ")
        for column_index, (column, weight) in enumerate(columns):
            if column_index:
                out.add(" + ")
            out.add(f"{weight!r} * ")
            _column_frequency(out, "d", column, phrase)
        out.add(f" AS f{index}")
    out.add(f" FROM {source.side_table} d WHERE ")
    _node_match(out, "d", node, source)
    out.add(") SELECT m.id AS id, -(")
    if scored:
        length_norm = f"{BM25_K1!r} * (1 - {BM25_B!r} + {BM25_B!r} * m.doc_length / c.avgdl)"
        for position, (index, _, _) in enumerate(scored):
            if position:
                out.add(" + ")
            ratio = f"(c.n - h.h{index} + 0.5) / (h.h{index} + 0.5)"
            out.add(f"(CASE WHEN {ratio} > 1 THEN ln({ratio}) ELSE {BM25_MIN_IDF!r} END)"
                    f" * m.f{index} * {BM25_K1 + 1!r} / (m.f{index} + {length_norm})")
    else:
        out.add("0")
    out.add(") AS rank FROM matched m CROSS JOIN corpus c CROSS JOIN hits h")
    return out.text(), out.params


def rebuild(connection) -> None:
    """FTS5's 'rebuild' for every side table: re-derive tokens and fts_stats from the
    indexed tables, in the caller's transaction (commit afterwards)."""
    connection.execute("SELECT tam_fts_rebuild()").fetchall()


def fts_source(table: str) -> FtsSource:
    """The side-table description of an FTS5 table (for schema checks and tooling)."""
    return _source(table)


__all__ = [
    "BM25_B", "BM25_K1", "FTS_SOURCES", "And", "ColumnKind", "FtsColumn", "FtsQueryError", "FtsSource",
    "Not", "Or", "Phrase", "fts5_tokens", "fts_source", "match_source", "parse", "rebuild",
]

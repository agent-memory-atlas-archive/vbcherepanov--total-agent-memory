"""v11.0 Phase 3 — Deterministic dedup helpers.

Two cheap operations that run in the save hot path:

* :func:`exact_dedup` — returns (sha256 of raw content, sha256 of
  normalized content). The normalized hash collapses whitespace, lowercases
  and strips so trivially different inputs share a key.
* :func:`repeats` — whether a new record says exactly what a stored one
  says. `Store._find_duplicate` finds candidates with FTS5 and keeps only
  those that pass this check.
* :func:`find_duplicate` — wraps `Store._find_duplicate`. The wrapper
  isolates that call site so future replacements don't need to touch
  every save path.

No LLM, no network. Pure local logic.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any, Optional

from tam_db.contracts import Backend

_RE_WS = re.compile(r"\s+")
_RE_TOKEN = re.compile(r"\w+(?:[./:-]\w+)*", re.UNICODE)


def normalize(text: str) -> str:
    """Lowercase, strip, collapse whitespace runs to single spaces."""
    if not text:
        return ""
    return _RE_WS.sub(" ", text.strip().lower())


# A repeat contains every word of the new text, so a few of them are enough
# to find the candidates.
MAX_CANDIDATE_TERMS = 32
MIN_CANDIDATE_TERM_LEN = 3


def _tokens(text: str) -> list[str]:
    folded = unicodedata.normalize("NFKC", text or "").casefold().replace("ё", "е")
    return _RE_TOKEN.findall(folded)


# updates_value: a value is the differing tail of two statements that share a
# prefix ("... is Argentina" / "... is Armenia"). At most one shared word may
# follow it ("любит [красный] цвет"), unless the shared tail is a modifier that
# opens with a preposition ("at [9:30] on Mondays", "в [14:00] по пятницам").
# Any other longer shared tail means the subject differed and the value was the
# same ("The company that produced [X] is GM").
MIN_SHARED_PREFIX = 2
MAX_VALUE_WORDS = 8
MAX_SHARED_SUFFIX = 1
MAX_MODIFIER_SUFFIX = 6
MIN_SHARED_FRACTION = 0.5
MODIFIER_OPENERS = frozenset({
    "on", "in", "at", "for", "from", "since", "until", "till", "every", "each", "during", "after",
    "before", "per", "with", "without", "via", "within", "near", "starting",
    "в", "во", "на", "по", "с", "со", "до", "после", "перед", "для", "из", "у", "при", "через",
    "около", "каждый", "каждую", "каждое", "каждого", "каждые", "начиная",
})


def updates_value(new: str, stored: str) -> bool:
    """True when `new` states a different value for what `stored` stated.

    The two must share their opening words and differ in a trailing value
    with no word in common. This fits functional facts (citizenship, capital,
    version in use) and misfires on multi-valued ones ("likes jazz" does not
    retract "likes rock") and on logs with a shared header, so callers opt in
    per record (`memory_save(supersede=true)`).
    """
    return tokens_update_value(_tokens(new), _tokens(stored))


def value_tokens(text: str) -> list[str]:
    """Tokens in the form `tokens_update_value` compares."""
    return _tokens(text)


def tokens_update_value(a: list[str], b: list[str]) -> bool:
    """`updates_value` on texts already split by `value_tokens`."""
    if not a or not b or a == b:
        return False
    prefix = 0
    while prefix < min(len(a), len(b)) and a[prefix] == b[prefix]:
        prefix += 1
    suffix = 0
    while suffix < min(len(a), len(b)) - prefix and a[-1 - suffix] == b[-1 - suffix]:
        suffix += 1
    value_a, value_b = a[prefix:len(a) - suffix], b[prefix:len(b) - suffix]
    return (
        prefix >= MIN_SHARED_PREFIX
        and (suffix <= MAX_SHARED_SUFFIX
             or (suffix <= MAX_MODIFIER_SUFFIX and a[len(a) - suffix] in MODIFIER_OPENERS))
        and 1 <= len(value_a) <= MAX_VALUE_WORDS
        and 1 <= len(value_b) <= MAX_VALUE_WORDS
        and not set(value_a) & set(value_b)
        and prefix + suffix >= MIN_SHARED_FRACTION * max(len(a), len(b))
    )


def prefix_match_query(text: str, words: int = MIN_SHARED_PREFIX) -> str:
    """FTS5 MATCH expression for records that start like `text` (candidates only)."""
    terms = [token for token in _tokens(text)[:words] if "е" not in token]
    return " ".join('"' + term.replace('"', '""') + '"' for term in terms)


def candidate_match_query(text: str) -> str:
    """FTS5 MATCH expression that finds records containing the words of `text`.

    Words with "е" are left out: `repeats` treats "ё" and "е" as one letter,
    the FTS5 index does not. Short words are left out when longer ones
    remain. Empty when no word is usable.
    """
    words = [token for token in _tokens(text) if "е" not in token]
    # Short words ("на", "7") are in most records and only slow the match.
    selective = [word for word in words if len(word) >= MIN_CANDIDATE_TERM_LEN] or words
    terms = list(dict.fromkeys(selective))[:MAX_CANDIDATE_TERMS]
    return " ".join('"' + term.replace('"', '""') + '"' for term in terms)


def repeats(new: str, stored: str) -> bool:
    """True when `new` states nothing that `stored` does not.

    Both texts must have the same words in the same order, ignoring case,
    punctuation and whitespace. Articles count: "Monk" and "The Monk" can be
    different works. A similarity score is the wrong test: "Messi's
    citizenship is Argentina" and "... is Armenia" score 0.96 and are an
    update, not a repeat.
    """
    tokens = _tokens(new)
    return bool(tokens) and tokens == _tokens(stored)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def exact_dedup(content: str) -> tuple[str, str]:
    """Return (raw_sha256, normalized_sha256) for the given content."""
    return _sha256(content or ""), _sha256(normalize(content or ""))


def find_duplicate(
    db_or_store: Any,
    content: str,
    ktype: str,
    project: str,
) -> Optional[int]:
    """Look up a near-duplicate via the legacy `Store._find_duplicate`.

    Accepts either a `Store` instance (preferred — gives access to the
    full FTS+Jaccard ladder) or a raw sqlite connection (falls back to a
    minimal FTS-only check).
    """
    # Preferred: full store-level dedup.
    finder = getattr(db_or_store, "_find_duplicate", None)
    if callable(finder):
        try:
            return finder(content, ktype, project)
        except Exception:  # noqa: BLE001 — never let dedup raise into save
            return None

    # Fallback: ad-hoc FTS5 nearest-match if we only have a connection.
    try:
        words = [w for w in (content or "").split()[:10] if len(w) > 2]
        if not words:
            return None
        fts_q = " OR ".join(
            re.sub(r'[^a-zA-Z0-9_]+', "", w) or w for w in words
        )
        if getattr(db_or_store, "backend", None) is Backend.POSTGRES:
            from memory_core.pg_fts import match_source

            source, source_params = match_source("knowledge_fts", f"content : ({fts_q})")
            rows = db_or_store.execute(
                f"""
                SELECT k.id FROM ({source}) f
                JOIN knowledge k ON k.id = f.id
                WHERE k.status='active' AND k.project=? AND k.type=?
                ORDER BY f.rank, k.id LIMIT 1
                """,
                (*source_params, project, ktype),
            ).fetchall()
            return int(rows[0][0]) if rows else None
        rows = db_or_store.execute(
            """
            SELECT k.id FROM knowledge_fts f
            JOIN knowledge k ON k.id = f.rowid
            WHERE f.content MATCH ? AND k.status='active'
              AND k.project=? AND k.type=?
            ORDER BY rank LIMIT 1
            """,
            (fts_q, project, ktype),
        ).fetchall()
        if rows:
            return int(rows[0][0])
    except Exception:  # noqa: BLE001
        pass
    return None


__all__ = ["candidate_match_query", "exact_dedup", "find_duplicate", "normalize", "prefix_match_query", "repeats", "updates_value"]

"""Regression tests for Store._binary_search candidate-pool edge cases.

Bug 2026-04-27: when the active embedding pool was equal to or smaller
than `n_candidates`, `np.argpartition(hamming, kth=n_cand)` raised
``ValueError: kth(=N) out of bounds (N)`` because argpartition needs
``kth STRICTLY < array length``. The save-path swallowed the exception
in a generic ``except Exception``, so v10 ``contradiction_log`` silently
stopped recording. Tiny test-projects (≤ n_candidates active rows) were
the natural trigger.

These tests pin three pool sizes against n_candidates=3:
  pool < n_candidates  → return everything
  pool == n_candidates → return everything (the regressed case)
  pool > n_candidates  → take top-n via argpartition
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pytest

from tests.pg_store_support import store_backend  # noqa: F401 — fixture

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


@pytest.fixture
def store(store_backend, monkeypatch, tmp_path):  # noqa: F811 — pytest fixture injection
    """Spin up a real Store on a tmp SQLite, no MCP/outbox side effects."""
    monkeypatch.setenv("MEMORY_QUALITY_GATE_ENABLED", "false")
    monkeypatch.setenv("MEMORY_CONTRADICTION_DETECT_ENABLED", "false")
    monkeypatch.setenv("MEMORY_OUTBOX_ENABLED", "false")
    monkeypatch.setenv("MEMORY_EPISODIC_ENABLED", "false")

    import server as srv
    # MEMORY_DIR is resolved at import time, so patch the module attribute.
    monkeypatch.setattr(srv, "MEMORY_DIR", tmp_path)
    s = srv.Store(database=store_backend)
    yield s
    try:
        s.db.close()
    except Exception:
        pass


def _seed_embeddings(store, vectors: dict[int, np.ndarray], project: str = "p"):
    """Insert (knowledge, embeddings) rows for the given knowledge_id → vector map."""
    now = "2026-04-27T00:00:00Z"
    for kid, vec in vectors.items():
        store.db.execute(
            """INSERT INTO knowledge (id, session_id, type, content, project, status, created_at)
                   VALUES (?, 's', 'fact', ?, ?, 'active', ?)""",
            (kid, f"row-{kid}", project, now),
        )
        store._upsert_embedding(kid, vec.tolist(), "test-model")
    store.db.commit()


def _rand_vec(rng: np.random.Generator, dim: int = 32) -> np.ndarray:
    return rng.standard_normal(dim).astype(np.float32)


def _exact_order(vectors: dict[int, np.ndarray], query: np.ndarray) -> list[int]:
    def cosine(vector):
        return float(vector @ query / (np.linalg.norm(vector) * np.linalg.norm(query)))

    return sorted(vectors, key=lambda kid: (-cosine(vectors[kid]), kid))


def test_binary_search_pool_smaller_than_n_candidates(store):
    """pool=2, n_candidates=3 → all 2 returned, no argpartition call."""
    rng = np.random.default_rng(0)
    vecs = {1: _rand_vec(rng), 2: _rand_vec(rng)}
    _seed_embeddings(store, vecs)

    query = _rand_vec(rng)
    results = store._binary_search(query.tolist(), n_candidates=3, project="p")

    assert {kid for kid, _score in results} == {1, 2}
    # Cosine similarity is bounded in [-1, 1].
    assert all(-1.0 <= score <= 1.0 for _kid, score in results)


def test_binary_search_pool_equal_to_n_candidates_does_not_raise(store):
    """pool=3, n_candidates=3 → the exact case that used to raise ValueError."""
    rng = np.random.default_rng(1)
    vecs = {kid: _rand_vec(rng) for kid in (10, 11, 12)}
    _seed_embeddings(store, vecs)

    query = _rand_vec(rng)
    # Must NOT raise — this is the regression we are pinning.
    results = store._binary_search(query.tolist(), n_candidates=3, project="p")

    assert {kid for kid, _score in results} == {10, 11, 12}


def test_binary_search_pool_larger_than_n_candidates_truncates(store):
    """pool=5, n_candidates=3 → at most 3 candidates flow to cosine re-rank.

    n_results default is 10 so we cannot assert cardinality from the return
    value alone, but we can assert it does not exceed n_candidates.
    """
    rng = np.random.default_rng(2)
    vecs = {kid: _rand_vec(rng) for kid in range(20, 25)}
    _seed_embeddings(store, vecs)

    query = _rand_vec(rng)
    results = store._binary_search(query.tolist(), n_candidates=3, project="p")

    if store.is_postgres:
        # PostgreSQL ranks every row in scope by exact cosine: there is no binary
        # prefilter for n_candidates to bound.
        assert [kid for kid, _ in results] == _exact_order(vecs, query)
        return
    assert len(results) <= 3
    seen_kids = {kid for kid, _score in results}
    assert seen_kids.issubset({20, 21, 22, 23, 24})


def test_binary_search_empty_pool_returns_empty(store):
    """Sanity: zero embeddings → empty result, no NumPy ops attempted."""
    rng = np.random.default_rng(3)
    query = _rand_vec(rng)
    assert store._binary_search(query.tolist(), n_candidates=3, project="p") == []


def test_exact_small_pool_recovers_best_cosine_despite_sign_difference(store):
    _seed_embeddings(store, {
        1: np.array([0.01, 1.0], dtype=np.float32),
        2: np.array([1.0, -0.01], dtype=np.float32),
    })
    query = [1.0, 0.01]
    approximate = store._binary_search(query, n_candidates=1, n_results=1, project="p")
    exact = store._binary_search(
        query, n_candidates=1, n_results=1, project="p", exact_small_pool=True,
    )
    if store.is_postgres:
        # Exact cosine on both calls: the sign-bit approximation never applies.
        assert approximate == exact and exact[0][0] == 2
        return
    assert approximate[0][0] == 1
    assert exact[0][0] == 2
    assert exact[0][1] > approximate[0][1]


def test_large_pool_still_uses_bounded_candidates(store, monkeypatch):
    import memory_core.vector_math as vectors

    monkeypatch.setattr(vectors, "EXACT_VECTOR_SCAN_LIMIT", 2)
    rng = np.random.default_rng(5)
    vecs = {kid: _rand_vec(rng) for kid in range(1, 5)}
    _seed_embeddings(store, vecs)
    query = _rand_vec(rng)
    result = store._binary_search(
        query.tolist(), n_candidates=1, project="p", exact_small_pool=True,
    )
    if store.is_postgres:
        assert [kid for kid, _ in result] == _exact_order(vecs, query)
        return
    assert len(result) == 1


@pytest.mark.parametrize("scope", [{"kind": "solution"}, {"branch": "feature"}])
def test_search_spaces_filters_before_selecting_top_result(store, monkeypatch, scope):
    _seed_embeddings(store, {
        1: np.array([1.0, 0.0], dtype=np.float32),
        2: np.array([0.8, 0.2], dtype=np.float32),
    })
    store.db.execute("UPDATE knowledge SET branch='other' WHERE id=1")
    store.db.execute("UPDATE knowledge SET type='solution' WHERE id=2")
    store.db.commit()
    monkeypatch.setattr(store, "_active_embed_model_name", lambda: "test-model")
    monkeypatch.setattr(store, "embed", lambda texts: [[1.0, 0.0]])
    result = store._search_spaces("query", project="p", limit=1, **scope)
    assert [identity for identity, _ in result] == [2]


def test_sqlite_adapter_consumes_real_store_tuples_and_filters_first(store):
    from memory_core.vector_store import SQLiteBinaryVectorStore

    _seed_embeddings(store, {kid: np.array([1.0, 0.0], dtype=np.float32) for kid in range(1, 61)})
    _seed_embeddings(store, {61: np.array([0.8, 0.2], dtype=np.float32)})
    store.db.execute("UPDATE embeddings SET embedding_space='code' WHERE knowledge_id=61")
    store.db.commit()
    adapter = SQLiteBinaryVectorStore(store)
    assert adapter.search([1.0, 0.0], top_k=1, project="p")[0] == ("1", 1.0)
    assert adapter.search([1.0, 0.0], top_k=1, project="p", embedding_space=" CODE ")[0][0] == "61"
    assert adapter.search([1.0, 0.0], top_k=0) == []
    assert adapter.search([], top_k=1) == []


@pytest.mark.parametrize("mode,provider_calls", [("fastembed", 0), ("openai", 2)])
def test_shared_local_model_encodes_query_once_across_spaces(store, monkeypatch, mode, provider_calls):
    from types import SimpleNamespace

    _seed_embeddings(store, {kid: np.array([1.0, 0.0], dtype=np.float32) for kid in (1, 2, 3)})
    store.db.execute("UPDATE embeddings SET embedding_space='config' WHERE knowledge_id=1")
    store.db.execute("UPDATE embeddings SET embedding_space='log' WHERE knowledge_id=3")
    store.db.commit()
    calls = {"store": 0, "provider": 0}

    def encode(texts):
        calls["store"] += 1
        return [[1.0, 0.0]]

    def encode_space(query, *, space):
        calls["provider"] += 1
        return [1.0, 0.0]

    store._embed_mode = mode
    monkeypatch.setattr(store, "_active_embed_model_name", lambda: "test-model")
    monkeypatch.setattr(store, "embed", encode)
    store._v11_embed_provider = SimpleNamespace(active_model=lambda space: "test-model", embed_query=encode_space)
    assert {identity for identity, _ in store._search_spaces("query", project="p")} == {1, 2, 3}
    assert not store._semantic_diagnostics
    assert calls == {"store": 1, "provider": provider_calls}


def test_different_space_model_keeps_its_own_query_encoder(store, monkeypatch):
    from types import SimpleNamespace

    _seed_embeddings(store, {1: np.array([1.0, 0.0], dtype=np.float32), 2: np.array([0.0, 1.0], dtype=np.float32)})
    store.db.execute("UPDATE embeddings SET embedding_space='code',embed_model='code-model' WHERE knowledge_id=2")
    store.db.commit()
    calls = []

    def encode_code(query, *, space):
        calls.append(space)
        return [0.0, 1.0]

    store._embed_mode = "fastembed"
    monkeypatch.setattr(store, "_active_embed_model_name", lambda: "test-model")
    monkeypatch.setattr(store, "embed", lambda texts: [[1.0, 0.0]])
    store._v11_embed_provider = SimpleNamespace(active_model=lambda space: "code-model", embed_query=encode_code)
    assert {identity for identity, _ in store._search_spaces("query", project="p")} == {1, 2}
    assert calls == ["code"]
    assert not store._semantic_diagnostics

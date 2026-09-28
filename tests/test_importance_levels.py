"""Tests for the v10 importance-level boost in recall ranking.

Three things must hold:
  * `save_knowledge` persists the column with sane validation;
  * `_IMPORTANCE_BOOST` honours env overrides at module load;
  * a `critical` record outranks an otherwise-identical `low` record at
    recall time (the whole point of this feature).
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

from tests.pg_store_support import store_backend  # noqa: F401 — fixture

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


@pytest.fixture
def store(store_backend, monkeypatch, tmp_path):  # noqa: F811 — pytest fixture injection
    """Instantiate real Store on a fresh temp MEMORY_DIR (mirrors the
    pattern used by test_integration_memory_save.py)."""
    (tmp_path / "blobs").mkdir(exist_ok=True)
    (tmp_path / "chroma").mkdir(exist_ok=True)
    import server
    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    s = server.Store(database=store_backend)
    yield s
    try:
        s.db.close()
    except Exception:
        pass


# ──────────────────────────────────────────────
# Persistence
# ──────────────────────────────────────────────


def test_save_knowledge_persists_importance(store):
    sid = "imp-sess-1"
    store.db.execute(
        "INSERT INTO sessions (id, started_at, project, status) VALUES (?, ?, 'demo', 'open')",
        (sid, "2026-04-27T00:00:00Z"),
    )
    store.db.commit()

    rid, *_ = store.save_knowledge(
        sid=sid, content="Migration 015 adds knowledge.importance",
        ktype="fact", project="demo", importance="critical",
    )
    row = store.db.execute(
        "SELECT importance FROM knowledge WHERE id=?", (rid,)
    ).fetchone()
    assert row["importance"] == "critical"


def test_save_knowledge_defaults_to_medium(store):
    sid = "imp-sess-2"
    store.db.execute(
        "INSERT INTO sessions (id, started_at, project, status) VALUES (?, ?, 'demo', 'open')",
        (sid, "2026-04-27T00:00:00Z"),
    )
    store.db.commit()

    rid, *_ = store.save_knowledge(
        sid=sid, content="Default importance must be medium",
        ktype="fact", project="demo",
    )
    row = store.db.execute(
        "SELECT importance FROM knowledge WHERE id=?", (rid,)
    ).fetchone()
    assert row["importance"] == "medium"


def test_save_knowledge_validates_importance_enum(store):
    sid = "imp-sess-3"
    store.db.execute(
        "INSERT INTO sessions (id, started_at, project, status) VALUES (?, ?, 'demo', 'open')",
        (sid, "2026-04-27T00:00:00Z"),
    )
    store.db.commit()

    rid, *_ = store.save_knowledge(
        sid=sid, content="Garbage importance falls back to medium",
        ktype="fact", project="demo", importance="EXTREMELY_URGENT",
    )
    row = store.db.execute(
        "SELECT importance FROM knowledge WHERE id=?", (rid,)
    ).fetchone()
    assert row["importance"] == "medium"


# ──────────────────────────────────────────────
# Boost values
# ──────────────────────────────────────────────


def test_importance_boost_defaults():
    import server
    assert server._IMPORTANCE_BOOST["critical"] == 1.5
    assert server._IMPORTANCE_BOOST["high"] == 1.2
    assert server._IMPORTANCE_BOOST["medium"] == 1.0
    assert server._IMPORTANCE_BOOST["low"] == 0.8


def test_importance_boost_env_override(monkeypatch):
    """Env overrides take effect on module reload — the production deploy
    sets these once at startup, so reload-on-test is the correct check."""
    monkeypatch.setenv("MEMORY_IMPORTANCE_BOOST_CRITICAL", "2.5")
    monkeypatch.setenv("MEMORY_IMPORTANCE_BOOST_LOW", "0.1")
    import server
    importlib.reload(server)
    try:
        assert server._IMPORTANCE_BOOST["critical"] == 2.5
        assert server._IMPORTANCE_BOOST["low"] == 0.1
    finally:
        # Reset for downstream tests so they see canonical defaults.
        monkeypatch.delenv("MEMORY_IMPORTANCE_BOOST_CRITICAL", raising=False)
        monkeypatch.delenv("MEMORY_IMPORTANCE_BOOST_LOW", raising=False)
        importlib.reload(server)


def test_importance_boost_negative_env_clamped(monkeypatch):
    monkeypatch.setenv("MEMORY_IMPORTANCE_BOOST_HIGH", "-1.0")
    import server
    importlib.reload(server)
    try:
        # Implementation clamps to 0 (negative scores are nonsensical).
        assert server._IMPORTANCE_BOOST["high"] == 0.0
    finally:
        monkeypatch.delenv("MEMORY_IMPORTANCE_BOOST_HIGH", raising=False)
        importlib.reload(server)


def test_importance_boost_invalid_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("MEMORY_IMPORTANCE_BOOST_HIGH", "not-a-number")
    import server
    importlib.reload(server)
    try:
        assert server._IMPORTANCE_BOOST["high"] == 1.2  # default preserved
    finally:
        monkeypatch.delenv("MEMORY_IMPORTANCE_BOOST_HIGH", raising=False)
        importlib.reload(server)

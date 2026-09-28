"""Integration test: save_knowledge must strip <private> tags and expose count."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.pg_store_support import store_backend  # noqa: F401 — fixture


@pytest.fixture
def store(store_backend, monkeypatch, tmp_path):  # noqa: F811 — pytest fixture injection
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
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


def _seed_session(s, sid="s1", project="demo"):
    s.db.execute(
        "INSERT INTO sessions (id, started_at, project, status) VALUES (?, ?, ?, 'open')",
        (sid, "2026-04-14T00:00:00Z", project),
    )
    s.db.commit()


def test_save_redacts_private_tag(store):
    _seed_session(store)
    rid, _dup, _red, priv, _qm = store.save_knowledge(
        sid="s1",
        content="hello <private>API_KEY=sk-xxx</private> world",
        ktype="fact",
        project="demo",
    )
    assert rid
    assert priv == 1
    saved = store.db.execute(
        "SELECT content FROM knowledge WHERE id=?", (rid,)
    ).fetchone()["content"]
    assert "sk-xxx" not in saved
    assert "API_KEY" not in saved
    assert "hello" in saved and "world" in saved


def test_save_no_tag_no_redaction(store):
    _seed_session(store)
    rid, _dup, _red, priv, _qm = store.save_knowledge(
        sid="s1", content="plain content no secrets", ktype="fact", project="demo",
    )
    assert priv == 0


def test_save_multiple_tags_counter(store):
    _seed_session(store)
    _rid, _dup, _red, priv, _qm = store.save_knowledge(
        sid="s1",
        content="a <private>s1</private> b <private>s2</private> c",
        ktype="fact",
        project="demo",
    )
    assert priv == 2


def test_private_redactions_total_counter(store):
    _seed_session(store)
    store.save_knowledge(
        sid="s1", content="x <private>one</private> y", ktype="fact", project="demo",
    )
    store.save_knowledge(
        sid="s1",
        content="m <private>two</private> n <private>three</private> o",
        ktype="fact", project="demo",
    )
    row = store.db.execute(
        "SELECT value FROM privacy_counters WHERE key='private_redactions_total'"
    ).fetchone()
    assert row is not None
    assert row["value"] == 3


def test_mcp_memory_save_exposes_field(store):
    # Emulate the MCP handler result-building block around save_knowledge.
    _seed_session(store)
    rid, was_dedup, was_redacted, private_sections, _qm = store.save_knowledge(
        "s1", "before <private>secret</private> after", "fact",
        "demo", [], "",
    )
    result = {"saved": True, "id": rid, "deduplicated": was_dedup}
    if was_redacted:
        result["privacy_redacted"] = True
    if private_sections:
        result["privacy_redacted_sections"] = private_sections
    assert result["saved"] is True
    assert result["privacy_redacted_sections"] == 1


def test_recall_does_not_return_secret(store):
    _seed_session(store)
    store.save_knowledge(
        sid="s1",
        content="note before <private>TOKEN=abc123def</private> note after",
        ktype="fact", project="demo",
    )
    import server as _srv
    recall = _srv.Recall(store)
    res = recall.search(query="note", project="demo", limit=10)
    items = [i for g in res.get("results", {}).values() for i in g]
    for it in items:
        assert "abc123def" not in (it.get("content") or "")
        assert "TOKEN=" not in (it.get("content") or "")

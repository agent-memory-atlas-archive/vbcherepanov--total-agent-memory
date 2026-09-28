"""Tests for MCP tool ``memory_get`` — batched fetch by ID.

Exercises the dispatcher via ``server._do(...)`` so both the SQL glue and
the JSON envelope are covered. Works on a real ``Store`` with an isolated
``MEMORY_DIR``.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from tests.pg_store_support import store_backend  # noqa: F401 — fixture

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


@pytest.fixture
def live_store(store_backend, monkeypatch, tmp_path):  # noqa: F811 — pytest fixture injection
    (tmp_path / "blobs").mkdir(exist_ok=True)
    (tmp_path / "chroma").mkdir(exist_ok=True)

    import server
    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    s = server.Store(database=store_backend)
    server.store = s
    server.recall = server.Recall(s)
    server.SID = "sess-get-1"
    server.BRANCH = ""
    s.db.execute(
        "INSERT INTO sessions (id, started_at, project, status) VALUES (?, ?, ?, ?)",
        (server.SID, "2026-04-14T00:00:00Z", "demo", "open"),
    )
    s.db.commit()
    yield s, server
    try:
        s.db.close()
    except Exception:
        pass


def _save(server, store, content: str, *, ktype: str = "fact", project: str = "demo") -> int:
    rid, *_ = store.save_knowledge(
        sid=server.SID, content=content, ktype=ktype, project=project,
    )
    return rid


def _call_get(server, **args) -> dict:
    raw = asyncio.run(server._do("memory_get", args))
    return json.loads(raw)


def test_memory_get_returns_full_by_id(live_store):
    s, server = live_store
    rid = _save(server, s, "Contract-first: use protobuf for all RPC.", ktype="convention")
    out = _call_get(server, ids=[rid])
    assert out["total"] == 1
    assert out["detail"] == "full"
    entry = out["results"][0]
    assert entry["id"] == rid
    # Full payload includes all the structured fields.
    for key in ("content", "context", "session_id", "status",
                "confidence", "created_at", "recall_count", "branch"):
        assert key in entry, f"missing {key} in full payload"
    assert entry["content"].startswith("Contract-first")


def test_memory_get_summary_truncates_150(live_store):
    s, server = live_store
    long_body = "A" * 400
    rid = _save(server, s, long_body)
    out = _call_get(server, ids=[rid], detail="summary")
    entry = out["results"][0]
    # 150 chars + "..." suffix
    assert entry["content"].endswith("...")
    assert len(entry["content"]) == 153
    # Summary mode omits heavy auxiliary fields.
    assert "context" not in entry
    assert "session_id" not in entry


def test_memory_get_missing_ids_skipped(live_store):
    s, server = live_store
    rid = _save(server, s, "alpha")
    out = _call_get(server, ids=[rid, 999_999_999])  # second is non-existent
    assert out["total"] == 1
    assert out["results"][0]["id"] == rid


def test_memory_get_max_50_ids_enforced(live_store):
    s, server = live_store
    # Save 60 records and ask for all of them — dispatcher should cap at 50.
    ids = [_save(server, s, f"item-{i}") for i in range(60)]
    out = _call_get(server, ids=ids)
    # Dispatcher keeps caller order and truncates at 50.
    assert out["total"] == 50
    returned = [e["id"] for e in out["results"]]
    assert returned == ids[:50]


def test_memory_get_empty_ids_returns_empty_list(live_store):
    _, server = live_store
    out = _call_get(server, ids=[])
    assert out == {"total": 0, "detail": "full", "results": []}


def test_memory_get_integration_with_index_mode(live_store, monkeypatch):
    """Full 3-layer flow: recall(mode=index) → pick IDs → memory_get(ids=[...]).
    """
    s, server = live_store
    # Avoid cognitive engine noise.
    monkeypatch.setattr(server, "_get_v5", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError()))

    rid1 = _save(server, s, "JWT refresh-token rotation prevents replay.")
    rid2 = _save(server, s, "Use Postgres BRIN index for append-only logs.")
    rid3 = _save(server, s, "Prefer uv over pip for fresh Python envs.")

    # Layer 1: index
    raw = asyncio.run(server._do("memory_recall", {
        "query": "token", "project": "demo", "mode": "index", "limit": 10,
    }))
    idx = json.loads(raw)
    assert idx.get("mode") == "index"
    assert idx["results"], "index mode returned nothing"
    # Pick the one matching the JWT record — its title starts with "JWT".
    chosen = [e["id"] for e in idx["results"] if "JWT" in (e.get("title") or "")]
    assert rid1 in chosen

    # Layer 3: fetch full content for exactly the picked IDs.
    out = _call_get(server, ids=[rid1])
    assert out["total"] == 1
    got = out["results"][0]
    assert got["id"] == rid1
    assert "refresh-token" in got["content"]
    # And we must not see the other records.
    assert rid2 not in [e["id"] for e in out["results"]]
    assert rid3 not in [e["id"] for e in out["results"]]

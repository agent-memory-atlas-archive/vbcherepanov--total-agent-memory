"""MCP tool contracts: empty writes refused, scoped updates, back-dated facts, explicit supersede outcome."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from tests.pg_store_support import store_backend  # noqa: F401 — fixture

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


@pytest.fixture
def live_server(store_backend, monkeypatch, tmp_path):  # noqa: F811 — pytest fixture injection
    for sub in ("raw", "blobs", "chroma"):
        (tmp_path / sub).mkdir(exist_ok=True)
    import server
    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    store = server.Store(database=store_backend)
    monkeypatch.setattr(server, "store", store)
    monkeypatch.setattr(server, "recall", server.Recall(store))
    monkeypatch.setattr(server, "SID", "sess-contracts")
    monkeypatch.setattr(server, "BRANCH", "")
    monkeypatch.setattr(server, "_v5_modules", {})
    store.db.execute("INSERT INTO sessions (id, started_at, project, status) VALUES (?, ?, ?, ?)",
                     (server.SID, "2026-09-28T00:00:00Z", "p", "open"))
    store.db.commit()
    yield server, store
    store.db.close()


def call(server, tool, **args):
    content, is_error = asyncio.run(server._call_tool_impl(tool, args))
    text = content[0].text
    try:
        return json.loads(text), is_error
    except ValueError:
        return text, is_error


def active_count(store):
    return store.db.execute("SELECT COUNT(*) FROM knowledge WHERE status='active'").fetchone()[0]


@pytest.mark.parametrize("tool", ["memory_save", "memory_save_fast"])
@pytest.mark.parametrize("content", ["", "   ", "\n\t"])
def test_blank_content_is_refused(live_server, tool, content):
    server, store = live_server
    result, is_error = call(server, tool, type="fact", project="p", content=content)
    assert is_error, result
    assert "content" in str(result)
    assert active_count(store) == 0


def test_blank_update_is_refused_and_keeps_the_record(live_server):
    server, store = live_server
    saved, _ = call(server, "memory_save", type="fact", project="p", content="Deploys run on Fridays at 10:00")
    result, is_error = call(server, "memory_update", id=saved["id"], new_content="  ")
    assert is_error, result
    row = store.db.execute("SELECT status FROM knowledge WHERE id=?", (saved["id"],)).fetchone()
    assert row[0] == "active"


def test_update_by_id_replaces_exactly_that_record(live_server):
    server, store = live_server
    a, _ = call(server, "memory_save", type="fact", project="alpha", content="Staging database host is db-stage-1")
    b, _ = call(server, "memory_save", type="fact", project="beta", content="Staging database host is db-stage-9")
    result, is_error = call(server, "memory_update", id=b["id"], new_content="Staging database host is db-stage-10")
    assert not is_error, result
    assert result["old_id"] == b["id"]
    status = dict(store.db.execute("SELECT id, status FROM knowledge WHERE id IN (?, ?)", (a["id"], b["id"])).fetchall())
    assert status == {a["id"]: "active", b["id"]: "superseded"}
    new = store.db.execute("SELECT project, content FROM knowledge WHERE id=?", (result["new_id"],)).fetchone()
    assert tuple(new) == ("beta", "Staging database host is db-stage-10")


def test_update_by_find_stays_inside_the_given_project(live_server):
    server, store = live_server
    a, _ = call(server, "memory_save", type="fact", project="alpha", content="Cache TTL for sessions is 30 minutes")
    b, _ = call(server, "memory_save", type="fact", project="beta", content="Cache TTL for sessions is 15 minutes")
    result, is_error = call(server, "memory_update", find="cache TTL sessions", project="alpha",
                            new_content="Cache TTL for sessions is 45 minutes")
    assert not is_error, result
    assert result["old_id"] == a["id"]
    assert store.db.execute("SELECT status FROM knowledge WHERE id=?", (b["id"],)).fetchone()[0] == "active"


def test_update_refuses_an_id_that_is_not_active(live_server):
    server, _ = live_server
    saved, _ = call(server, "memory_save", type="fact", project="p", content="Queue is RabbitMQ 4")
    call(server, "memory_delete", id=saved["id"])
    result, is_error = call(server, "memory_update", id=saved["id"], new_content="Queue is Kafka")
    assert not is_error
    assert result["error"] == "Record not found in DB"


def test_update_needs_find_or_id(live_server):
    server, _ = live_server
    result, is_error = call(server, "memory_update", new_content="anything")
    assert is_error, result


def test_kg_add_fact_accepts_valid_from_for_point_in_time_queries(live_server):
    server, _ = live_server
    call(server, "kg_add_fact", subject="shop", predicate="uses_cache", object="Redis",
         project="p", valid_from="2025-01-01T00:00:00Z")
    call(server, "kg_add_fact", subject="shop", predicate="uses_cache", object="Memcached",
         project="p", valid_from="2026-06-01T00:00:00Z")
    past, _ = call(server, "kg_at", subject="shop", project="p", timestamp="2025-12-01T00:00:00Z")
    now, _ = call(server, "kg_at", subject="shop", project="p")
    assert [f["object"] for f in past["assertions"]] == ["Redis"]
    assert [f["object"] for f in now["assertions"]] == ["Memcached"]


def test_kg_invalidate_fact_accepts_the_end_time(live_server):
    server, _ = live_server
    call(server, "kg_add_fact", subject="shop", predicate="runs_on", object="Heroku",
         project="p", valid_from="2024-01-01T00:00:00Z")
    closed, _ = call(server, "kg_invalidate_fact", subject="shop", predicate="runs_on", object="Heroku",
                     project="p", at="2025-03-01T00:00:00Z")
    assert closed == {"closed": 1}
    before, _ = call(server, "kg_at", subject="shop", project="p", timestamp="2025-02-01T00:00:00Z")
    after, _ = call(server, "kg_at", subject="shop", project="p", timestamp="2025-04-01T00:00:00Z")
    assert [f["object"] for f in before["assertions"]] == ["Heroku"]
    assert after["assertions"] == []


def test_supersede_request_that_retires_nothing_says_so(live_server):
    server, _ = live_server
    call(server, "memory_save", type="decision", project="p",
         content="We chose Redis Streams for the order queue instead of RabbitMQ", context="WHY: ops cost")
    result, is_error = call(server, "memory_save", type="decision", project="p", supersede=True,
                            content="Billing retries use exponential backoff with jitter", context="WHY: thundering herd")
    assert not is_error
    assert "superseded" not in result
    assert "memory_update" in result["supersede_note"]

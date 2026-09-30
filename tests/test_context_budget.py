import sqlite3

import pytest

from memory_core.context_budget import fill_budget, group_by_anchor
from memory_core.evidence_context import EvidenceContext
from memory_core.evidence_pack import packed_size
from memory_core.retrieval import SearchScope


def _size(hit):
    return len(hit["content"])


def test_fill_keeps_whole_hits_in_rank_order_and_skips_what_does_not_fit():
    groups = [[{"id": 1, "content": "a" * 40}], [{"id": 2, "content": "b" * 50}],
              [{"id": 3, "content": "c" * 10}], [{"id": 4, "content": "d" * 60}]]
    kept = fill_budget(groups, 60, cost=_size)
    assert [hit["id"] for hit in kept] == [1, 3]


def test_fill_uses_budget_a_fixed_top_k_would_leave_empty():
    groups = [[{"id": index, "content": "x" * 10}] for index in range(30)]
    assert len(fill_budget(groups, 200, cost=_size)) == 20


def test_fill_always_keeps_the_best_hit_even_when_it_alone_is_too_large():
    kept = fill_budget([[{"id": 1, "content": "a" * 500}], [{"id": 2, "content": "b"}]], 100, cost=_size)
    assert [hit["id"] for hit in kept] == [1]


def test_fill_counts_a_record_shared_by_two_groups_once():
    shared = {"id": 2, "content": "s" * 30}
    groups = [[{"id": 1, "content": "a" * 30}, shared], [shared, {"id": 3, "content": "c" * 30}]]
    kept = fill_budget(groups, 90, cost=_size)
    assert [hit["id"] for hit in kept] == [1, 2, 3]


def test_fill_rejects_a_non_positive_budget():
    with pytest.raises(ValueError, match="max_chars"):
        fill_budget([], 0, cost=_size)


def test_group_by_anchor_puts_each_anchor_before_its_neighbours_in_rank_order():
    evidence = [{"id": 5}, {"id": 9}, {"id": 4, "anchor_id": 5}, {"id": 8, "anchor_id": 9},
                {"id": 6, "anchor_id": 5}]
    assert [[hit["id"] for hit in group] for group in group_by_anchor(evidence)] == [[5, 4, 6], [9, 8]]


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE knowledge (id INTEGER PRIMARY KEY, content TEXT, project TEXT, session_id TEXT, "
        "created_at TEXT, status TEXT, type TEXT, branch TEXT, tags TEXT DEFAULT '[]')")
    connection.execute("CREATE TABLE embeddings (knowledge_id INTEGER, embedding_space TEXT)")
    rows = [(index, f"record {index} " + "w" * 180, "app", "s", f"2026-01-01T00:00:{index:02d}", "active",
             "fact", "") for index in range(1, 21)]
    connection.executemany("INSERT INTO knowledge (id, content, project, session_id, created_at, status, type, "
                           "branch) VALUES (?,?,?,?,?,?,?,?)", rows)
    yield connection
    connection.close()


def test_context_fill_keeps_records_whole_within_budget(db, monkeypatch):
    monkeypatch.setenv("MEMORY_CONTEXT_RESOLVE_DATES", "off")
    hits = [{"id": index} for index in (10, 3, 17, 7, 12, 1)]
    scope = SearchScope(project="app")
    budget = 1400
    packed = EvidenceContext(db).build(hits, query="record", scope=scope, radius=0, max_chars=budget, fill=True)
    assert [hit["id"] for hit in packed] == [10, 3, 17, 7, 12]
    assert all(hit["content"].startswith(f"record {hit['id']} ") and hit["content"].endswith("w" * 180)
               for hit in packed)
    assert sum(packed_size(hit) for hit in packed) <= budget


def test_context_fill_brings_each_hit_with_its_neighbours(db, monkeypatch):
    monkeypatch.setenv("MEMORY_CONTEXT_RESOLVE_DATES", "off")
    packed = EvidenceContext(db).build([{"id": 10}, {"id": 3}], query="record", scope=SearchScope(project="app"),
                                       radius=1, max_chars=1900, fill=True)
    assert sorted(hit["id"] for hit in packed) == [2, 3, 4, 9, 10, 11]


def test_context_without_fill_is_unchanged(db, monkeypatch):
    monkeypatch.setenv("MEMORY_CONTEXT_RESOLVE_DATES", "off")
    hits = [{"id": index} for index in (10, 3, 17, 7, 12, 1)]
    packed = EvidenceContext(db).build(hits, query="record", scope=SearchScope(project="app"), radius=0,
                                       max_chars=1400)
    assert [hit["id"] for hit in packed] == [10, 3, 17, 7, 12, 1]


def test_public_context_mode_fill_budget_searches_deeper_and_keeps_whole_records(db, monkeypatch):
    import asyncio
    import json
    from types import SimpleNamespace

    import server

    monkeypatch.setenv("MEMORY_CONTEXT_RESOLVE_DATES", "off")
    limits = []

    def search(*args):
        limits.append(args[3])
        return {"results": {"fact": [{"id": index} for index in range(1, 21)][:args[3]]}}

    monkeypatch.setattr(server, "store", SimpleNamespace(db=db))
    monkeypatch.setattr(server, "recall", SimpleNamespace(search=search))
    request = {"query": "record", "project": "app", "mode": "context", "neighbors": 0,
               "context_max_chars": 2000, "limit": 3}
    plain = json.loads(asyncio.run(server._do("memory_recall", request)))
    filled = json.loads(asyncio.run(server._do("memory_recall", {**request, "fill_budget": True})))
    assert limits == [3, 100]
    assert [hit["id"] for hit in plain["results"]] == [1, 2, 3]
    assert [hit["id"] for hit in filled["results"]] == [1, 2, 3, 4, 5, 6, 7]
    assert all(hit["content"].endswith("w" * 180) for hit in filled["results"])


def test_public_context_mode_fill_budget_takes_no_neighbours_unless_asked(db, monkeypatch):
    import asyncio
    import json
    from types import SimpleNamespace

    import server

    monkeypatch.setenv("MEMORY_CONTEXT_RESOLVE_DATES", "off")
    monkeypatch.setattr(server, "store", SimpleNamespace(db=db))
    monkeypatch.setattr(server, "recall", SimpleNamespace(
        search=lambda *args: {"results": {"fact": [{"id": 10}, {"id": 3}]}}))
    request = {"query": "record", "project": "app", "mode": "context", "context_max_chars": 4000,
               "fill_budget": True}
    default = json.loads(asyncio.run(server._do("memory_recall", request)))
    asked = json.loads(asyncio.run(server._do("memory_recall", {**request, "neighbors": 1})))
    assert [hit["id"] for hit in default["results"]] == [10, 3]
    assert sorted(hit["id"] for hit in asked["results"]) == [2, 3, 4, 9, 10, 11]

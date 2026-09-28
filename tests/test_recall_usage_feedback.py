"""`recall_count` is a usage statistic, not a ranking signal.

`Recall.search` bumps `recall_count` on every row it returns. Until 14.4.0 the
scorer also added `min(0.3, recall_count * 0.05)`, so rows that merely showed
up often (short turns matching a common name) outranked the ones that answered
the question: on LoCoMo an agent's own queries cost 4.7 points of R@10 within
one pass. Counting stays (dashboard, consolidation); ranking ignores it.
Benchmarks and `memory_explain_search` pass `record_usage=False` so their runs
leave no trace in the statistics.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import server  # noqa: E402
from tests.pg_store_support import store_backend  # noqa: F401 — fixture


@pytest.fixture
def seeded(store_backend, tmp_path, monkeypatch):  # noqa: F811 — pytest fixture injection
    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    store = server.Store(database=store_backend)
    store.session_start("s1", project="usage")
    for text in (
        "PostgreSQL 18 uses UUID v7 primary keys for the orders table",
        "The billing service publishes invoice events to RabbitMQ",
        "Deploys run through Docker Compose with health checks",
    ):
        store.save_knowledge("s1", "fact", text, project="usage")
    try:
        yield store, server.Recall(store)
    finally:
        try:
            store.db.close()
        except Exception:
            pass


def _counts(store) -> list[int]:
    return [
        r[0]
        for r in store.db.execute(
            "SELECT recall_count FROM knowledge WHERE project='usage' ORDER BY id"
        ).fetchall()
    ]


def test_search_records_usage_by_default(seeded):
    store, recall = seeded
    assert sum(_counts(store)) == 0
    recall.search(query="UUID v7 primary keys", project="usage", limit=5)
    assert sum(_counts(store)) > 0, "spaced repetition stopped working"


def test_record_usage_false_leaves_counters_untouched(seeded):
    store, recall = seeded
    for _ in range(3):
        recall.search(
            query="UUID v7 primary keys",
            project="usage",
            limit=5,
            record_usage=False,
        )
    assert sum(_counts(store)) == 0


def test_repeated_measurement_is_stable(seeded):
    """The property the benchmark depends on: same query, same ranking."""
    _store, recall = seeded

    def ids() -> list[int]:
        res = recall.search(
            query="how do we deploy", project="usage", limit=3,
            detail="compact", record_usage=False,
        )
        return [r["id"] for group in res["results"].values() for r in group]

    first = ids()
    assert first, "search returned nothing — fixture is not exercising the path"
    for _ in range(4):
        assert ids() == first


def test_explain_search_does_not_mutate_counters(seeded, monkeypatch):
    """memory_explain_search is a diagnostic; it must observe, not disturb."""
    store, recall = seeded
    monkeypatch.setattr(server, "store", store)
    monkeypatch.setattr(server, "recall", recall)

    import asyncio

    asyncio.run(
        server._do("memory_explain_search", {"query": "RabbitMQ", "project": "usage"})
    )
    assert sum(_counts(store)) == 0


def test_recorded_usage_does_not_change_ranking(seeded):
    """Rows returned many times must not climb above better matches."""
    store, recall = seeded

    def ids(record: bool) -> list[int]:
        res = recall.search(query="how do we deploy", project="usage", limit=3,
                            detail="compact", record_usage=record)
        return [r["id"] for group in res["results"].values() for r in group]

    before = ids(False)
    assert before, "search returned nothing — fixture is not exercising the path"
    last = before[-1]
    store.db.execute("UPDATE knowledge SET recall_count=500 WHERE id=?", (last,))
    store.db.commit()
    for _ in range(3):
        ids(True)
    assert ids(False) == before

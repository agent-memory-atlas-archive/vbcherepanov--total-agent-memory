import re
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from memory_core.timestamps import format_utc, normalize_timestamp, utc_now
from tests.pg_store_support import store_backend  # noqa: F401 — fixture

CANONICAL = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
MIGRATION = Path(__file__).parents[1] / "migrations/034_canonical_timestamps.sql"


@pytest.fixture
def berlin(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Berlin")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_utc_now_is_canonical_even_on_a_whole_second():
    assert CANONICAL.match(utc_now())
    assert format_utc(datetime(2026, 9, 21, 8, 0, 0, tzinfo=UTC)) == "2026-09-21T08:00:00.000000Z"


def test_format_utc_converts_offsets_and_rejects_naive_values():
    cest = timezone(timedelta(hours=2))
    assert format_utc(datetime(2026, 9, 21, 10, 0, tzinfo=cest)) == "2026-09-21T08:00:00.000000Z"
    with pytest.raises(ValueError, match="timezone-aware"):
        format_utc(datetime(2026, 9, 21, 10, 0))


@pytest.mark.parametrize(("stored", "expected"), [
    ("2026-09-21T08:21:37.622445Z", "2026-09-21T08:21:37.622445Z"),
    ("2026-09-21T08:03:19.489091+00:00", "2026-09-21T08:03:19.489091Z"),
    ("2026-09-21T08:03:19Z", "2026-09-21T08:03:19.000000Z"),
    ("2026-09-21T10:03:19+02:00", "2026-09-21T08:03:19.000000Z"),
    ("2026-03-27 09:04:00", "2026-03-27T08:04:00.000000Z"),
    ("2026-04-07 13:04:13", "2026-04-07T11:04:13.000000Z"),
])
def test_normalize_reads_every_legacy_shape_and_local_time_with_dst(berlin, stored, expected):
    assert normalize_timestamp(stored) == expected


def test_normalize_rejects_garbage():
    for value in ("", "   ", "yesterday"):
        with pytest.raises(ValueError):
            normalize_timestamp(value)


def test_migration_canonicalizes_knowledge_without_rebuilding_atomic_facts(tmp_path, monkeypatch, berlin):
    import server
    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    store = server.Store()
    legacy = [
        ("2026-09-21T08:21:37.622445Z", "2026-09-21T08:21:37.622445Z"),
        ("2026-09-21T08:03:19.489091+00:00", None),
        ("2026-09-21T08:03:19Z", "2026-09-21T09:00:00+00:00"),
        ("2026-03-27 09:04:00", "2026-03-27 09:04:00"),
        ("not a date", None),
    ]
    ids = []
    for index, (created, confirmed) in enumerate(legacy):
        cursor = store.db.execute(
            "INSERT INTO knowledge(session_id,type,content,project,tags,created_at,last_confirmed)"
            " VALUES('s','fact',?,'p','[]',?,?)", (f"fact {index}", created, confirmed))
        ids.append(cursor.lastrowid)
    store.db.execute("INSERT INTO atomic_fact_runs(knowledge_id,source_content,fact_count,model) VALUES(?,?,0,'m')",
                     (ids[1], "fact 1"))
    store.db.commit()

    store.db.executescript(MIGRATION.read_text())

    rows = {row[0]: row[1:] for row in store.db.execute(
        f"SELECT id, created_at, last_confirmed FROM knowledge WHERE id IN ({','.join('?' * len(ids))})", ids)}
    assert [rows[i] for i in ids] == [
        ("2026-09-21T08:21:37.622445Z", "2026-09-21T08:21:37.622445Z"),
        ("2026-09-21T08:03:19.489091Z", None),
        ("2026-09-21T08:03:19.000000Z", "2026-09-21T09:00:00.000000Z"),
        ("2026-03-27T08:04:00.000000Z", "2026-03-27T08:04:00.000000Z"),
        ("not a date", None),
    ]
    assert store.db.execute("SELECT count(*) FROM atomic_fact_runs WHERE knowledge_id=?", (ids[1],)).fetchone()[0] == 1
    assert store.db.execute("SELECT count(*) FROM atomic_fact_rebuild").fetchone()[0] == 0

    store.db.execute("UPDATE knowledge SET created_at=? WHERE id=?", ("2026-09-22T00:00:00.000000Z", ids[1]))
    assert store.db.execute("SELECT count(*) FROM atomic_fact_rebuild WHERE knowledge_id=?", (ids[1],)).fetchone()[0] == 1
    store.db.close()


def test_saved_knowledge_uses_the_canonical_format(store_backend, tmp_path, monkeypatch):  # noqa: F811 — pytest fixture injection
    import server
    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(server.Store, "embed", lambda self, texts: [[1.0] + [0.0] * 383 for _ in texts])
    store = server.Store(database=store_backend)
    store.session_start("s", project="p")
    kid, *_ = store.save_knowledge("s", "Маша любит зелёный цвет.", "fact", project="p", skip_quality=True)
    created, confirmed = store.db.execute("SELECT created_at, last_confirmed FROM knowledge WHERE id=?", (kid,)).fetchone()
    assert CANONICAL.match(created) and created == confirmed
    store.db.close()

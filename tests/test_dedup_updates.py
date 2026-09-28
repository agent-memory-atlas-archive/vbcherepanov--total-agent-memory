"""Dedup must drop repeats and keep updates.

Regression: a similarity threshold treated "... is Argentina" -> "... is Armenia"
(fuzzy 0.96) as a duplicate, so the new value was never stored.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from memory_core.dedup import repeats
from tests.pg_store_support import store_backend  # noqa: F401 — fixture

UPDATES = [
    ("Lionel Messi's country of citizenship is Argentina.", "Lionel Messi's country of citizenship is Armenia."),
    ("Сервис billing работает на PostgreSQL 16.", "Сервис billing работает на PostgreSQL 18."),
    ("Маша любит красный цвет.", "Маша любит зелёный цвет."),
    ("Alice reports to Bob.", "Bob reports to Alice."),
    ("Redis is used for sessions.", "Redis is used for sessions and rate limits."),
    ("Alice is the CEO.", "Alice was the CEO."),
    ("Monk was written in the language of English.", "The Monk was written in the language of English."),
]

REPEATS = [
    ("Lionel Messi's country of citizenship is Argentina.", "lionel messi's country of citizenship is argentina"),
    ("Сервис billing  работает на PostgreSQL 16.", "Сервис billing работает на PostgreSQL 16"),
    ("Ёлка стоит в холле.", "Елка стоит в холле."),
    ("Safety is associated with the sport of American football", "safety is associated with the sport of American football."),
]


@pytest.mark.parametrize(("old", "new"), UPDATES)
def test_update_is_not_a_repeat(old, new):
    assert not repeats(new, old)


@pytest.mark.parametrize(("old", "new"), REPEATS)
def test_repeat_is_detected(old, new):
    assert repeats(new, old)


def test_empty_text_never_repeats():
    assert not repeats("", "")
    assert not repeats("...", "!!!")


@pytest.fixture
def store(store_backend, monkeypatch, tmp_path):  # noqa: F811 — pytest fixture injection
    (tmp_path / "blobs").mkdir(exist_ok=True)
    (tmp_path / "chroma").mkdir(exist_ok=True)
    import server

    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    s = server.Store(database=store_backend)
    yield s
    s.db.close()


@pytest.mark.parametrize(("old", "new"), UPDATES[:2])
def test_save_keeps_updated_value(store, old, new):
    first_id, first_dup, *_ = store.save_knowledge("s1", old, "fact", project="p")
    second_id, second_dup, *_ = store.save_knowledge("s1", new, "fact", project="p")

    assert not first_dup and not second_dup
    assert second_id != first_id
    stored = {row[0] for row in store.db.execute("SELECT content FROM knowledge WHERE status='active'")}
    assert stored == {old, new}


def active(store):
    return [row[0] for row in store.db.execute("SELECT content FROM knowledge WHERE status='active' ORDER BY id")]


@pytest.mark.parametrize(("old", "new"), REPEATS)
def test_repeat_replaces_stored_record(store, old, new):
    first_id, _, *_ = store.save_knowledge("s1", old, "fact", project="p")
    second_id, second_dup, *_ = store.save_knowledge("s1", new, "fact", project="p")

    assert second_dup
    assert second_id != first_id
    assert active(store) == [new]
    status, superseded_by = store.db.execute(
        "SELECT status, superseded_by FROM knowledge WHERE id=?", (first_id,)).fetchone()
    assert (status, superseded_by) == ("superseded", second_id)
    assert store.db.execute("SELECT COUNT(*) FROM embeddings WHERE knowledge_id=?", (first_id,)).fetchone()[0] == 0


def test_value_restated_after_change_is_the_latest(store):
    """A, then B, then A again: A is current, so it must carry the latest date."""
    a1 = "Monk was written in the language of English"
    b = "Monk was written in the language of Hebrew"
    store.save_knowledge("s1", a1, "fact", project="p")
    b_id, *_ = store.save_knowledge("s1", b, "fact", project="p")
    a2_id, dup, *_ = store.save_knowledge("s1", a1, "fact", project="p")

    assert dup
    assert active(store) == [b, a1]
    created = dict(store.db.execute("SELECT id, created_at FROM knowledge WHERE id IN (?, ?)", (b_id, a2_id)))
    assert created[a2_id] > created[b_id]


def test_outbox_replay_keeps_the_stored_record(store):
    first_id, *_ = store.save_knowledge("s1", REPEATS[0][0], "fact", project="p")
    replay_id, dup, *_ = store.save_knowledge("s1", REPEATS[0][0], "fact", project="p", _from_outbox=True)

    assert dup
    assert replay_id == first_id
    assert store.db.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0] == 1


def test_repeat_in_another_project_is_kept(store):
    store.save_knowledge("s1", REPEATS[0][0], "fact", project="p")
    _, dup, *_ = store.save_knowledge("s1", REPEATS[0][0], "fact", project="q")
    assert not dup


def test_confirm_keeps_the_stored_record(store):
    first_id, *_ = store.save_knowledge("s1", REPEATS[0][0], "fact", project="p")
    second_id, dup, *_ = store.save_knowledge("s1", REPEATS[0][1], "fact", project="p", repeat="confirm")

    assert dup
    assert second_id == first_id
    assert store.db.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0] == 1


def test_unknown_repeat_mode_is_rejected(store):
    with pytest.raises(ValueError, match="repeat must be"):
        store.save_knowledge("s1", "Deploy runs nightly", "fact", project="p", repeat="merge")

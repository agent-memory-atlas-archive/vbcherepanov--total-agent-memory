"""PgAuditedConnection and the PL/pgSQL audit triggers behave like the SQLite audit layer."""
import json

import pytest

from team_memory.audit import LEGACY, SYSTEM, authorship, install, pg_audited_connection
from team_memory.contracts import Actor, Conflict
from tests.pg_store_support import provision_workspace

pytestmark = pytest.mark.postgres

ANNA = Actor(user_id="anna", display_name="Anna", client="test")
BORIS = Actor(user_id="boris", display_name="Boris", client="test")
INSERT = "INSERT INTO knowledge(session_id,type,content,created_at) VALUES ('s','fact',?,'2026-09-25T00:00:00Z')"


@pytest.fixture
def db(pg_database):
    from tam_db import pg_connection, pg_schema

    database = provision_workspace(pg_database.url)
    connection = pg_connection.connect(database, factory=pg_audited_connection())
    pg_schema.ensure(connection)
    connection.execute(INSERT, ("Legacy record before auditing",))
    connection.commit()
    install(connection)
    try:
        yield connection
    finally:
        connection.close()


def history(db, record_id):
    rows = db.execute("SELECT operation,actor,reason,revision,before_state,after_state FROM tam_history "
                      "WHERE record_id=? ORDER BY sequence", (record_id,)).fetchall()
    return [(row[0], json.loads(row[1])["user_id"], row[2], row[3],
             json.loads(row[4]) if row[4] else None, json.loads(row[5]) if row[5] else None) for row in rows]


def test_install_is_idempotent_and_backfills_legacy_records(db):
    install(db)
    assert authorship(db, 1) == {"created_by": LEGACY.model_dump(mode="json"),
                                 "updated_by": LEGACY.model_dump(mode="json"), "revision": 1}
    assert db.execute("SELECT count(*) FROM tam_history").fetchone()[0] == 0


def test_transaction_records_actor_origin_and_reason(db):
    with db.transaction(ANNA, "initial"):
        record_id = db.execute(INSERT, ("Anna wrote this",)).lastrowid
    with db.transaction(BORIS, "edit"):
        db.origin = ANNA
        db.execute("UPDATE knowledge SET content='Boris edited this' WHERE id=?", (record_id,))
        db.execute("UPDATE knowledge SET project='general' WHERE id=?", (record_id,))
    assert authorship(db, record_id) == {"created_by": ANNA.model_dump(mode="json"),
                                         "updated_by": BORIS.model_dump(mode="json"), "revision": 2}
    entries = history(db, record_id)
    assert [entry[:4] for entry in entries] == [("insert", "anna", "initial", 1), ("update", "boris", "edit", 2)]
    assert entries[0][4] is None and entries[0][5]["content"] == "Anna wrote this"
    assert entries[1][4]["content"] == "Anna wrote this" and entries[1][5]["content"] == "Boris edited this"
    assert list(entries[1][5]) == ["id", "type", "content", "context", "project", "tags", "status", "superseded_by"]


def test_outside_a_transaction_the_actor_is_system(db):
    record_id = db.execute(INSERT, ("System wrote this",)).lastrowid
    db.commit()
    assert authorship(db, record_id)["created_by"] == SYSTEM.model_dump(mode="json")
    assert db.execute("SELECT tam_reason()").fetchone()[0] == ""


def test_failed_transaction_rolls_back_record_and_audit(db):
    before = db.execute("SELECT count(*) FROM tam_history").fetchone()[0]
    with pytest.raises(Conflict), db.transaction(ANNA, "doomed"):
        db.execute(INSERT, ("Never stored",))
        raise Conflict("Injected failure")
    assert db.execute("SELECT count(*) FROM knowledge WHERE content='Never stored'").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM tam_history").fetchone()[0] == before
    assert db.actor == SYSTEM and db.origin is None and db.reason == ""


def test_inner_commit_and_rollback_do_not_end_the_operation(db):
    with pytest.raises(Conflict), db.transaction(ANNA, "nested"):
        db.execute(INSERT, ("Inner commit",))
        db.commit()
        db.rollback()
    assert db.execute("SELECT count(*) FROM knowledge WHERE content='Inner commit'").fetchone()[0] == 0


def test_delete_is_audited_with_the_last_state(db):
    with db.transaction(ANNA, "create"):
        record_id = db.execute(INSERT, ("Temporary",)).lastrowid
    with db.transaction(BORIS, "purge"):
        db.execute("DELETE FROM knowledge WHERE id=?", (record_id,))
    last = history(db, record_id)[-1]
    assert last[:4] == ("delete", "boris", "purge", 2)
    assert last[4]["content"] == "Temporary" and last[5] is None


def test_no_op_update_is_not_audited(db):
    with db.transaction(ANNA, "create"):
        record_id = db.execute(INSERT, ("Stable",)).lastrowid
    with db.transaction(BORIS, "noop"):
        db.execute("UPDATE knowledge SET content='Stable' WHERE id=?", (record_id,))
    assert [entry[0] for entry in history(db, record_id)] == ["insert"]
    assert authorship(db, record_id)["revision"] == 1

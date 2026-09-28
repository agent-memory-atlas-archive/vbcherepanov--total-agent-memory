"""Team worker operations through Runtime on both backends: the same assertions for SQLite and PostgreSQL."""
import uuid

import pytest

from team_memory.contracts import (
    Actor,
    Conflict,
    Delete,
    History,
    Save,
    Scope,
    Update,
    Work,
    Workspace,
)
from tests.pg_store_support import store_backend  # noqa: F401 — fixture

ANNA = Actor(user_id="anna", display_name="Anna", client="test")
BORIS = Actor(user_id="boris", display_name="Boris", client="test")
WORKSPACE = Workspace(key="shared", scope=Scope(), writable=True)
BACKGROUND_QUEUES = ("triple_extraction_queue", "deep_enrichment_queue", "representations_queue")


@pytest.fixture
def runtime(store_backend, monkeypatch, tmp_path):  # noqa: F811 — pytest fixture injection
    import server
    from team_memory.worker import Runtime

    data_dir = tmp_path / "workspace"
    monkeypatch.setattr(server, "MEMORY_DIR", data_dir)
    for key, value in {"TAM_MEMORY_DIR": str(data_dir), "CLAUDE_MEMORY_DIR": str(data_dir),
                       "MEMORY_QUALITY_GATE_ENABLED": "false", "MEMORY_ASYNC_ENRICHMENT": "false",
                       "USE_BINARY_SEARCH": "true"}.items():
        monkeypatch.setenv(key, value)
    result = Runtime(str(data_dir), store_backend)
    assert result.store.is_postgres is (store_backend is not None)
    try:
        yield result
    finally:
        result.store.db.close()


def work(operation: str, request, actor: Actor = ANNA) -> Work:
    arguments = request if isinstance(request, dict) else request.model_dump(mode="json", exclude={"scope"})
    return Work(actor=actor, workspace=WORKSPACE, operation=operation, arguments=arguments)


def save(runtime, content: str, **fields) -> dict:
    return runtime.execute(work("memory_save", Save(content=content, **fields)))


def count(runtime, table: str) -> int:
    return runtime.store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_save_returns_the_record_with_authorship(runtime):
    record = save(runtime, "The release train leaves every second Tuesday at noon.", project="ops", tags=["release"])
    assert record["saved"] is True and record["deduplicated"] is False
    assert record["content"] == "The release train leaves every second Tuesday at noon."
    assert record["project"] == "ops"
    assert record["status"] == "active"
    assert record["revision"] == 1
    assert record["created_by"] == ANNA.model_dump(mode="json")
    assert record["updated_by"] == ANNA.model_dump(mode="json")
    assert runtime.execute(work("memory_get", {"id": record["id"]}))["id"] == record["id"]


def test_repeated_save_confirms_instead_of_duplicating(runtime):
    first = save(runtime, "Invoices are approved by the finance lead before payment.")
    second = save(runtime, "Invoices are approved by the finance lead before payment.")
    assert second["id"] == first["id"]
    assert second["deduplicated"] is True
    assert count(runtime, "knowledge") == 1
    history = runtime.execute(work("memory_history", History(id=first["id"])))
    assert [entry["operation"] for entry in history] == ["insert", "confirm"]


def test_recall_finds_saved_records_lexically_and_semantically(runtime):
    target = save(runtime, "Kubernetes clusters are upgraded during the Sunday maintenance window.", project="infra")
    save(runtime, "Lunch orders are collected by the office manager on Fridays.", project="office")
    save(runtime, "Password rotation for the vault happens quarterly.", project="infra")
    hits = runtime.execute(work("memory_recall", {"query": "kubernetes upgrade maintenance window",
                                                  "limit": 3, "project": None}))
    assert hits and hits[0]["id"] == target["id"]
    assert hits[0]["created_by"] == ANNA.model_dump(mode="json")
    scoped = runtime.execute(work("memory_recall", {"query": "kubernetes maintenance", "limit": 5, "project": "office"}))
    assert target["id"] not in [hit["id"] for hit in scoped]


def test_update_supersedes_and_keeps_the_creator(runtime):
    original = save(runtime, "The staging database runs on version 16.")
    update = Update(id=original["id"], expected_revision=original["revision"],
                    content="The staging database runs on version 18.", reason="Upgraded staging")
    replacement = runtime.execute(work("memory_update", update, actor=BORIS))
    assert replacement["previous_id"] == original["id"]
    assert replacement["created_by"] == ANNA.model_dump(mode="json")
    assert replacement["updated_by"] == BORIS.model_dump(mode="json")
    old = runtime.record(original["id"])
    assert old["status"] == "superseded" and old["superseded_by"] == replacement["id"]
    assert old["revision"] == original["revision"] + 1
    stale = Update(id=original["id"], expected_revision=original["revision"],
                   content="The staging database runs on version 17.", reason="Stale edit")
    with pytest.raises(Conflict):
        runtime.execute(work("memory_update", stale))
    history = runtime.execute(work("memory_history", History(id=replacement["id"])))
    operations = [(entry["record_id"], entry["operation"]) for entry in history]
    assert (original["id"], "insert") in operations
    assert (original["id"], "update") in operations
    assert (replacement["id"], "insert") in operations
    reasons = {entry["reason"] for entry in history if entry["operation"] == "update"}
    assert reasons == {"Upgraded staging"}


def test_delete_marks_the_record_and_audits_it(runtime):
    record = save(runtime, "The office door code changes on the first of every month.")
    deleted = runtime.execute(work("memory_delete", Delete(id=record["id"], expected_revision=record["revision"],
                                                           reason="Obsolete")))
    assert deleted["status"] != "active"
    assert deleted["revision"] == record["revision"] + 1
    assert count(runtime, "embeddings") == 0
    history = runtime.execute(work("memory_history", History(id=record["id"])))
    last = history[-1]
    assert last["operation"] == "update" and last["reason"] == "Obsolete"
    assert last["actor"] == ANNA.model_dump(mode="json")
    assert last["before_state"]["status"] == "active"
    assert last["after_state"]["status"] == deleted["status"]


def test_history_pages_by_sequence(runtime):
    record = save(runtime, "Backups are verified with a restore drill every month.")
    for _ in range(3):
        save(runtime, "Backups are verified with a restore drill every month.")
    history = runtime.execute(work("memory_history", History(id=record["id"])))
    sequences = [entry["sequence"] for entry in history]
    assert sequences == sorted(sequences) and len(sequences) == 4
    page = runtime.execute(work("memory_history", History(id=record["id"], after=sequences[1], limit=1)))
    assert [entry["sequence"] for entry in page] == [sequences[2]]
    assert all(entry["at"].endswith("Z") and "T" in entry["at"] for entry in history)


def test_export_pages_by_id(runtime):
    ids = [save(runtime, f"Export fixture number {n} covers a distinct topic {n * 7}.")["id"] for n in range(5)]
    first = runtime.execute(work("memory_export", {"after": 0, "limit": 3}))
    assert [record["id"] for record in first] == ids[:3]
    rest = runtime.execute(work("memory_export", {"after": first[-1]["id"], "limit": 10}))
    assert [record["id"] for record in rest] == ids[3:]
    assert all("created_by" in record and "revision" in record for record in first + rest)


def test_request_id_replays_and_rejects_reuse(runtime):
    request_id = uuid.uuid4()
    request = Save(request_id=request_id, content="Deploys are frozen during the end-of-quarter close.")
    first = runtime.execute(work("memory_save", request))
    replay = runtime.execute(work("memory_save", request))
    assert replay == first
    assert count(runtime, "knowledge") == 1
    other = Save(request_id=request_id, content="A different statement under the same request id.")
    with pytest.raises(Conflict):
        runtime.execute(work("memory_save", other))
    assert count(runtime, "knowledge") == 1


def test_failed_update_rolls_back_record_and_audit(runtime, monkeypatch):
    original = save(runtime, "The on-call rotation hands over on Monday mornings.")
    before = (count(runtime, "knowledge"), count(runtime, "tam_history"))
    real_save = runtime.save

    def fail_after_save(*args, **kwargs):
        real_save(*args, **kwargs)
        raise Conflict("Injected failure after the replacement was saved")

    monkeypatch.setattr(runtime, "save", fail_after_save)
    update = Update(id=original["id"], expected_revision=original["revision"],
                    content="The on-call rotation hands over on Tuesday mornings.", reason="Changed")
    with pytest.raises(Conflict):
        runtime.execute(work("memory_update", update))
    assert (count(runtime, "knowledge"), count(runtime, "tam_history")) == before
    assert runtime.record(original["id"])["status"] == "active"


def test_team_worker_does_not_fill_background_queues(runtime):
    """Decision D4: nothing in the team server drains these queues, so the worker never fills them."""
    save(runtime, "Customer escalations go to the duty manager within one hour.")
    assert runtime.store.background_queues is False
    for table in BACKGROUND_QUEUES:
        assert count(runtime, table) == 0, table


# Lexical call sites that have an explicit PostgreSQL branch (FTS5 MATCH / bm25()).

@pytest.fixture
def store(store_backend, monkeypatch, tmp_path):  # noqa: F811 — pytest fixture injection
    import server

    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    result = server.Store(database=store_backend)
    assert result.is_postgres is (store_backend is not None)
    try:
        yield result
    finally:
        result.db.close()


def remember(store, content: str, project: str = "lexical", session: str = "s1") -> int:
    record_id, *_ = store.save_knowledge(session, content, "fact", project=project, skip_quality=True)
    return record_id


def test_connection_only_duplicate_lookup_uses_the_lexical_index(store):
    from memory_core.dedup import find_duplicate

    record_id = remember(store, "Quarterly compliance audit covers vendor contracts.")
    remember(store, "Unrelated note about the cafeteria menu.")
    assert find_duplicate(store.db, "compliance audit vendor contracts", "fact", "lexical") == record_id
    assert find_duplicate(store.db, "compliance audit vendor contracts", "fact", "other") is None


def test_timeline_query_finds_sessions_by_content(store):
    import server

    store.session_start("alpha", project="lexical")
    store.session_start("beta", project="lexical")
    remember(store, "Incident postmortem for the payment gateway outage.", session="alpha")
    remember(store, "Holiday calendar for the support team.", session="beta")
    timeline = server.Recall(store).timeline(query="payment gateway outage", limit=5)
    assert [session["id"] for session in timeline["sessions"]] == ["alpha"]


def test_passage_ranking_uses_lexical_matches(store):
    from memory_core.passage_index import PassageIndex

    record_id = remember(store, "user: The Golden Retriever sleeps in the garden.\nassistant: Noted, the garden.")
    row = dict(store.db.execute("SELECT * FROM knowledge WHERE id=?", (record_id,)).fetchone())
    index = PassageIndex(store.db, lambda texts: [[1.0, 0.5] for _ in texts], "test")
    index.ensure([row])
    ranked = index.rank("Golden Retriever", [record_id])
    assert ranked and ranked[0].source_id == record_id
    assert any(passage.lexical_rank == 1 for passage in ranked)


def test_episode_bm25_channel_ranks_matching_episodes(store):
    from memory_core.episodes.retriever import _bm25_search

    episodes = (("Rotated the vault secrets after the audit.", "alice"),
                ("Planned the team offsite agenda.", "bob"))
    for hour, (summary, participant) in enumerate(episodes):
        episode_id = store.db.execute(
            "INSERT INTO episodes_v11(project,started_at,ended_at,participants,summary) VALUES (?,?,?,?,?)",
            ("lexical", f"2026-09-25T0{hour}:00:00Z", f"2026-09-25T0{hour}:30:00Z", f'["{participant}"]',
             summary)).lastrowid
        if not store.is_postgres:
            # SQLite's contentless FTS mirror is written by the episode extractor, not a trigger.
            store.db.execute("INSERT INTO episodes_v11_fts(rowid,summary,participants,outcome) VALUES (?,?,?,'')",
                             (episode_id, summary, participant))
    store.db.commit()
    hits = _bm25_search(store.db, "vault secrets rotation", "lexical", 5)
    summaries = [store.db.execute("SELECT summary FROM episodes_v11 WHERE id=?", (eid,)).fetchone()[0]
                 for eid, _ in hits]
    assert summaries[:1] == ["Rotated the vault secrets after the audit."]
    assert _bm25_search(store.db, "vault secrets", "elsewhere", 5) == []


def test_atomic_fact_search_returns_the_source_records(store):
    from memory_core.atomic_facts import FactRepository
    from memory_core.retrieval import SearchScope

    record_id = remember(store, "Maria moved the design review to Thursday afternoons.")
    other_id = remember(store, "The parking garage closes at midnight.")
    for knowledge_id, content in ((record_id, "Design review happens on Thursday"),
                                  (other_id, "Parking garage closes at midnight")):
        fact_id = store.db.execute(
            "INSERT INTO atomic_facts(knowledge_id,subject,predicate,object,temporal_text,content,observed_at,"
            "event_start,event_end,event_precision,event_key,temporal_anchor_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (knowledge_id, "s", "p", "o", "", content, "2026-09-25T00:00:00Z", "", "", "", f"key-{knowledge_id}", ""),
        ).lastrowid
        store.db.execute("INSERT INTO atomic_fact_sources(fact_id,knowledge_id,quote) VALUES (?,?,?)",
                         (fact_id, knowledge_id, content))
    store.db.commit()
    hits = FactRepository(store.db).search("design review thursday", SearchScope(project="lexical"), 5)
    assert [hit["id"] for hit in hits] == [record_id]


def test_recall_lexical_tiers_do_not_fail(store):
    import server
    from memory_core.telemetry import counters

    target = remember(store, "Terraform state is stored in the encrypted bucket.")
    remember(store, "Lunch is served at noon in the atrium.", project="elsewhere")
    error_counters = ("retrieval_fts_errors", "retrieval_atomic_facts_errors")
    before = {name: counters.snapshot().get(name, 0) for name in error_counters}
    for project in ("lexical", None):
        result = server.Recall(store).search("terraform encrypted bucket", project=project, limit=3,
                                             record_usage=False)
        found = [item for group in result.get("results", {}).values() for item in group]
        assert found and found[0]["id"] == target
    assert {name: counters.snapshot().get(name, 0) for name in error_counters} == before

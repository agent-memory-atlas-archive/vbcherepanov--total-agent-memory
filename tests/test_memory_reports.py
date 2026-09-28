import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from memory_reports import cli, tool
from memory_reports.contracts import ReportRequest
from memory_reports.llm_summary import NO_LLM, ReportSummarizer, prompt
from memory_reports.render import render_markdown
from memory_reports.repository import ReportRepository, parse_instant
from memory_reports.service import ReportService, Source
from memory_reports.storage import report_path, slug

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "tests" / "fixtures" / "reports" / "billing_week.md"
NOW = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)
PROJECT = "billing-api"
TZ = "Europe/Berlin"

KNOWLEDGE = [
    # id, session, type, content, context, project, tags, status, superseded_by, importance, created, confirmed
    (1, "s-prev", "decision", "Keep invoices in PostgreSQL", "Transactions matter more than raw speed.", PROJECT, [],
     "active", None, "medium", "2026-09-15T09:00:00.000000Z", None),
    (2, "s-mon", "fact", "Invoice PDFs are rendered by WeasyPrint 61", "", PROJECT, ["pdf", "file:src/pdf/render.py"],
     "superseded", 8, "medium", "2026-09-21T08:15:00.000000Z", None),
    (3, "s-mon", "decision", "DECISION: Move invoice numbering to a PostgreSQL sequence",
     "Gapless numbering is a legal requirement; app-side counters raced under load.", PROJECT, ["postgres"],
     "active", None, "high", "2026-09-21T09:30:00.000000Z", None),
    (4, "s-mon", "solution", "Fix duplicate invoice numbers with nextval in the same transaction",
     "Two workers read MAX(number)+1.", PROJECT, ["invoices", "file:src/invoices/numbering.py"], "active", None,
     "medium", "2026-09-21T11:05:00.000000Z", None),
    (5, "s-wed", "lesson", "Run the numbering migration with lock_timeout set", "The first attempt blocked writes.",
     PROJECT, ["postgres"], "active", None, "medium", "2026-09-23T14:00:00.000000Z", None),
    (6, "s-wed", "convention", "Money amounts are stored as integer cents", "", PROJECT, ["invoices"], "active", None,
     "medium", "2026-09-23T22:30:00.000000Z", None),
    (7, "s-web", "fact", "The marketing site runs on Astro", "", "website", ["astro"], "active", None, "medium",
     "2026-09-22T12:00:00.000000Z", None),
    (8, "s-thu", "fact", "Invoice PDFs are rendered by WeasyPrint 62", "", PROJECT, ["pdf"], "active", None, "medium",
     "2026-09-24T10:00:00.000000Z", None),
    (9, "s-thu", "decision", "DECISION: Queue invoice e-mails in Redis streams",
     json.dumps({"schema": 1, "title": "Queue", "selected": "Redis streams",
                 "rationale": "Already deployed; consumer groups give at-least-once delivery."}),
     PROJECT, ["structured", "redis"], "active", None, "critical", "2026-09-24T16:45:00.000000Z", None),
    (10, "s-old", "fact", "VAT rates live in the tax table", "", PROJECT, [], "active", None, "medium",
     "2026-09-01T08:00:00.000000Z", "2026-09-22T09:00:00.000000Z"),
]
ERRORS = [
    (1, "s-prev", "IntegrityError: duplicate invoice number", "src/invoices/numbering.py", "duplicate-number", "high",
     "open", "2026-09-18T10:00:00Z"),
    (2, "s-mon", "IntegrityError: duplicate invoice number", "src/invoices/numbering.py", "duplicate-number", "high",
     "open", "2026-09-21T10:40:00Z"),
    (3, "s-thu", "IntegrityError: duplicate invoice number", "src/invoices/export.py", "duplicate-number", "high",
     "open", "2026-09-24T09:10:00Z"),
    (4, "s-thu", "WeasyPrint crashed on CSS grid", "src/pdf/render.py", "pdf-render", "medium", "resolved",
     "2026-09-24T09:40:00Z"),
]


def seed(db) -> None:
    """The known week of activity, written through any sqlite3-compatible connection."""
    for row in KNOWLEDGE:
        (kid, session, kind, content, context, project, tags, status, successor, importance, created,
         confirmed) = row
        db.execute("INSERT INTO knowledge (id,session_id,type,content,context,project,tags,status,superseded_by,"
                   "importance,created_at,last_confirmed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (kid, session, kind, content, context, project, json.dumps(tags), status, successor,
                    importance, created, confirmed or created))
    for eid, session, text, file, pattern, severity, status, created in ERRORS:
        db.execute("INSERT INTO errors (id,session_id,category,severity,description,context,fix,project,tags,status,"
                   "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (eid, session, "bug", severity, text, f"root_cause: concurrent writers | pattern: {pattern}",
                    "Use a database sequence", PROJECT,
                    json.dumps([f"file:{file}", f"pattern:{pattern}", "learn_error"]), status, created))
    db.execute("INSERT INTO rules (id,session_id,content,context,category,project,created_at,updated_at) "
               "VALUES (1,'s-thu','Never compute invoice numbers in the application','from duplicate-number',"
               "'bug',?, '2026-09-24T09:11:00Z','2026-09-24T09:11:00Z')", (PROJECT,))
    summaries = [("sum-mon-1", "s-mon", ["Backfill September numbers", "Add a numbering load test"],
                  ["Do not backfill during business hours"], [], 0, "2026-09-21T18:00:00Z"),
                 ("sum-thu-2", "s-thu", ["add a numbering  load test", "Wire the Redis consumer into the mailer"],
                  [], ["Do we need per-country numbering?"], 1, "2026-09-24T18:30:00Z")]
    for sid, session, steps, pitfalls, questions, consumed, ended in summaries:
        db.execute("INSERT INTO session_summaries (id,session_id,project,summary,next_steps,pitfalls,open_questions,"
                   "consumed,ended_at) VALUES (?,?,?,?,?,?,?,?,?)",
                   (sid, session, PROJECT, "work", json.dumps(steps), json.dumps(pitfalls), json.dumps(questions),
                    consumed, ended))
    db.execute("INSERT INTO observations (id,session_id,tool_name,observation_type,summary,files_affected,project,"
               "created_at) VALUES (1,'s-wed','Edit','change','edit',?,?,'2026-09-23T13:00:00Z')",
               (json.dumps(["src/invoices/numbering.py", "migrations/0042_invoice_seq.sql"]), PROJECT))
    for node, kind, name in (("n-pg", "technology", "PostgreSQL"), ("n-redis", "technology", "Redis"),
                             ("n-ev", "event", "save:3"), ("n-proj", "project", PROJECT)):
        db.execute("INSERT INTO graph_nodes (id,type,name) VALUES (?,?,?)", (node, kind, name))
    for kid, node in ((3, "n-pg"), (5, "n-pg"), (9, "n-redis"), (3, "n-ev"), (3, "n-proj")):
        db.execute("INSERT INTO knowledge_nodes (knowledge_id,node_id) VALUES (?,?)", (kid, node))


@pytest.fixture(scope="module")
def database(tmp_path_factory):
    """A real store schema (all migrations applied) seeded with a known week of activity."""
    root = tmp_path_factory.mktemp("report-store")
    import config
    import server
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("MEMORY_LLM_ENABLED", "false")
        patch.setattr(server, "MEMORY_DIR", root)
        config._cache_clear()
        store = server.Store()
        db = store.db
        seed(db)
        db.commit()
        db.close()
        config._cache_clear()
    return root / "memory.db"


@pytest.fixture
def db(database):
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        yield connection


def build(db, **fields):
    request = ReportRequest(**{"project": PROJECT, "period": "week", "tz": TZ, **fields})
    return ReportService(clock=lambda: NOW).build(request, [Source(None, ReportRepository(db))])


def test_summary_counts_new_updated_superseded_and_confirmed(db):
    report = build(db)
    summary = report.summary
    assert report.window.label == "ISO week 2026-W39 (2026-09-21 – 2026-09-27)" and report.window.in_progress
    assert summary.records == 7 and summary.records_by_type == {"convention": 1, "decision": 2, "fact": 2,
                                                                 "lesson": 1, "solution": 1}
    assert (summary.new, summary.updated, summary.superseded, summary.confirmed) == (6, 1, 1, 1)
    assert summary.errors == 3 and summary.session_summaries == 2 and summary.sessions == 3
    assert summary.active_days == 3
    assert [d.date for d in report.timeline] == ["2026-09-21", "2026-09-23", "2026-09-24"]


def test_local_timezone_moves_records_across_midnight(db):
    berlin = {d.date: d.by_type for d in build(db).timeline}
    utc = {d.date: d.by_type for d in build(db, tz="UTC").timeline}
    assert berlin["2026-09-24"].get("convention") == 1 and "convention" not in berlin["2026-09-23"]
    assert utc["2026-09-23"].get("convention") == 1


def test_deltas_against_previous_week(db):
    changes = {m.key: m for m in build(db).changes}
    assert (changes["records"].current, changes["records"].previous, changes["records"].delta) == (7, 1, 6)
    assert changes["records"].change_pct == 600.0
    assert (changes["errors"].current, changes["errors"].previous) == (3, 1)
    assert changes["lessons"].previous == 0 and changes["lessons"].change_pct is None
    assert changes["decisions"].delta == 1
    assert all(m.previous is None for m in build(db, period="all").changes)


def test_sections_carry_source_ids(db):
    report = build(db)
    assert [i.sources[0].id for i in report.decisions.items] == [9, 3]  # critical first
    assert report.decisions.items[0].why == "Selected Redis streams. Already deployed; consumer groups give at-least-once delivery."
    assert report.decisions.items[1].title == "Move invoice numbering to a PostgreSQL sequence"
    assert [i.sources[0].id for i in report.solutions.items] == [4]
    assert [(i.type, i.sources[0].kind) for i in report.lessons.items] == [("lesson", "knowledge"), ("rule", "rule")]
    assert [i.sources[0].id for i in report.errors.items] == [2, 3, 4]
    assert report.errors.items[0].why == "concurrent writers" and report.errors.items[0].detail == "Use a database sequence"
    pattern = report.error_patterns[0]
    assert (pattern.pattern, pattern.count, pattern.total, pattern.recurring) == ("duplicate-number", 2, 3, True)
    assert [s.id for s in pattern.sources] == [2, 3]
    assert report.error_patterns[1].recurring is False
    for listing in (report.decisions, report.solutions, report.errors, report.lessons, report.open_items,
                    report.files, report.entities, report.tags):
        assert all(item.sources for item in listing.items)
    assert all(entry.sources for day in report.timeline for entry in day.entries)


def test_open_items_are_deduplicated_and_ordered(db):
    items = build(db).open_items.items
    assert [(i.kind, i.text, i.occurrences, i.picked_up) for i in items] == [
        ("next_step", "add a numbering load test", 2, True),
        ("next_step", "Wire the Redis consumer into the mailer", 1, True),
        ("next_step", "Backfill September numbers", 1, False),
        ("open_question", "Do we need per-country numbering?", 1, True),
        ("pitfall", "Do not backfill during business hours", 1, False)]
    assert [s.id for s in items[0].sources] == ["sum-mon-1", "sum-thu-2"]


def test_files_entities_and_tags(db):
    report = build(db)
    assert [(f.name, f.count) for f in report.files.items][:2] == [("src/invoices/numbering.py", 3), ("src/pdf/render.py", 2)]
    assert [(e.name, e.kind, e.count) for e in report.entities.items] == [("PostgreSQL", "technology", 2),
                                                                          ("Redis", "technology", 1)]
    assert "structured" not in {t.name for t in report.tags.items}
    everywhere = build(db, project=None)
    assert everywhere.summary.records == 8 and PROJECT in {e.name for e in everywhere.entities.items}


def test_project_filter_and_limit(db):
    assert "Astro" not in render_markdown(build(db))
    small = build(db, limit=1)
    assert small.decisions.total == 2 and len(small.decisions.items) == 1
    assert "## Key decisions (showing 1 of 2)" in render_markdown(small)


def test_empty_period(db):
    report = build(db, period="custom", since="2025-01-01", until="2025-01-31")
    assert report.empty and report.summary.records == 0 and report.timeline == []
    assert all(m.current == 0 for m in report.changes)
    markdown = render_markdown(report)
    assert "No activity was recorded in this period." in markdown and "## Key decisions" not in markdown
    nothing = build(db, project="no-such-project", period="all")
    assert nothing.empty and nothing.window.first_day == nothing.window.last_day == "2026-09-25"


def test_output_is_deterministic(db, database):
    first = build(db).model_dump_json()
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as other:
        assert build(other).model_dump_json() == first
    assert render_markdown(build(db)) == render_markdown(build(db))


def test_markdown_snapshot(db):
    markdown = render_markdown(build(db))
    if os.environ.get("UPDATE_REPORT_SNAPSHOT") == "1":
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(markdown, encoding="utf-8")
    assert markdown == SNAPSHOT.read_text(encoding="utf-8")


def test_json_shape_omits_empty_workspace(db):
    payload = json.loads(tool.handle(db, {"project": PROJECT, "tz": TZ, "format": "json"}, Path("/nonexistent"),
                                     service=ReportService(clock=lambda: NOW)))
    ref = payload["report"]["decisions"]["items"][0]["sources"][0]
    assert ref == {"kind": "knowledge", "id": 9} and payload["saved_to"] is None
    assert payload["report"]["schema_version"] == 1


class FakeLLM:
    def __init__(self, reply="The team moved invoice numbering to a sequence.", fail=None):
        self.reply, self.fail, self.prompts = reply, fail, []

    def complete(self, text, **kwargs):
        self.prompts.append((text, kwargs))
        if self.fail:
            raise self.fail
        return self.reply


def test_llm_summary_success_uses_the_digest(db):
    llm = FakeLLM()
    markdown = tool.handle(db, {"project": PROJECT, "tz": TZ, "include_llm_summary": True}, Path("/nonexistent"),
                           service=ReportService(clock=lambda: NOW), summarizer=ReportSummarizer(lambda: llm))
    assert "> The team moved invoice numbering to a sequence." in markdown
    text, options = llm.prompts[0]
    assert "untrusted data" in text and "Move invoice numbering to a PostgreSQL sequence" in text
    assert options["max_tokens"] == 400 and options["temperature"] == 0.2


@pytest.mark.parametrize("factory,reason", [
    (lambda: None, NO_LLM),
    (lambda: FakeLLM(fail=RuntimeError("HTTP 500")), "LLM call failed (RuntimeError)"),
    (lambda: FakeLLM(fail=TimeoutError()), "LLM call failed (TimeoutError)"),
    (lambda: FakeLLM(reply="   "), "the LLM returned an empty summary"),
])
def test_llm_summary_failures_keep_the_report(db, factory, reason):
    report = ReportSummarizer(factory).apply(build(db))
    assert report.llm_summary is None and report.llm_summary_error == reason
    assert f"_LLM summary unavailable: {reason}_" in render_markdown(report)
    assert report.decisions.total == 2


def test_llm_is_not_called_without_opt_in_or_for_empty_reports(db):
    def forbidden():
        raise AssertionError("LLM factory must not be called")
    tool.handle(db, {"project": PROJECT}, Path("/nonexistent"), summarizer=ReportSummarizer(forbidden))
    empty = build(db, period="custom", since="2025-01-01", until="2025-01-02")
    assert ReportSummarizer(forbidden).apply(empty).llm_summary_error.startswith("nothing to summarize")
    misconfigured = ReportSummarizer(lambda: (_ for _ in ()).throw(ValueError("MEMORY_LLM_MODEL is required")))
    assert "misconfigured" in misconfigured.apply(build(db)).llm_summary_error
    assert "DATA:" in prompt(build(db))


def test_save_writes_under_reports_dir(db, tmp_path):
    markdown = tool.handle(db, {"project": PROJECT, "tz": TZ, "save": True}, tmp_path,
                           service=ReportService(clock=lambda: NOW))
    path = tmp_path / "reports" / PROJECT / "week-2026-09-21.md"
    assert path.is_file() and markdown.endswith(f"_Saved to: {path}_\n")
    assert path.read_text(encoding="utf-8") == markdown.split("\n_Saved to:")[0]
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    report = build(db, project=None, period="custom", since="2026-09-01", until="2026-09-15")
    assert report_path(tmp_path, report) == tmp_path / "reports" / "all-projects" / "custom-2026-09-01_2026-09-15.md"
    assert slug("../../etc") == "etc" and slug("a/b c") == "a-b-c" and slug("...") == "project"


def test_tool_rejects_bad_arguments(db):
    with pytest.raises(ValueError):
        tool.handle(db, {"period": "custom"}, Path("/nonexistent"))
    with pytest.raises(ValueError):
        tool.handle(db, {"period": "week", "format": "pdf"}, Path("/nonexistent"))
    with pytest.raises(ValueError, match="Unknown timezone"):
        tool.handle(db, {"period": "week", "tz": "Nowhere/City"}, Path("/nonexistent"))
    schema = tool.input_schema()
    assert schema["type"] == "object" and schema["additionalProperties"] is False
    assert set(schema["properties"]) >= {"project", "period", "since", "until", "format", "include_llm_summary", "save"}


def test_repository_tolerates_old_or_foreign_stores(tmp_path):
    with closing(sqlite3.connect(tmp_path / "old.db")) as old:
        old.execute("CREATE TABLE knowledge (id INTEGER PRIMARY KEY, session_id TEXT, type TEXT, content TEXT, "
                    "context TEXT, project TEXT, tags TEXT, status TEXT, superseded_by INTEGER, created_at TEXT)")
        old.execute("INSERT INTO knowledge VALUES (1,'s','fact','x','', 'p','not json','active',NULL,'2026-09-22 10:00:00')")
        old.execute("INSERT INTO knowledge VALUES (2,'s','fact','y','', 'p','[]','active',NULL,'garbage')")
        report = build(old, project="p")
    assert report.summary.records == 1 and report.files.total == 0 and report.entities.total == 0
    assert parse_instant("2026-09-22T10:00:00+02:00") == datetime(2026, 9, 22, 8, tzinfo=UTC)
    assert parse_instant("nope") is None and parse_instant(None) is None


def test_cli_prints_writes_and_fails_cleanly(database, tmp_path, capsys):
    memory = database.parent
    assert cli.main(["--memory-dir", str(memory), "--project", PROJECT, "--period", "all", "--tz", TZ]) == 0
    assert capsys.readouterr().out.startswith("# Activity report: billing-api")
    out = tmp_path / "out" / "report.json"
    assert cli.main(["--memory-dir", str(memory), "--period", "month", "--format", "json", "--out", str(out)]) == 0
    assert json.loads(out.read_text())["report"]["window"]["kind"] == "month"
    assert cli.main(["--memory-dir", str(tmp_path / "missing"), "--period", "day"]) == 1
    assert "no memory database" in capsys.readouterr().err
    assert cli.main(["--memory-dir", str(memory), "--period", "custom"]) == 2
    assert cli.main(["--memory-dir", str(memory), "--period", "custom", "--since", "2026-09-10",
                     "--until", "2026-09-01"]) == 2
    assert "until must be after since" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(["--period", "fortnight"])


def test_tam_console_entry_dispatches_report(database, tmp_path):
    env = {**os.environ, "HOME": str(tmp_path), "TAM_MEMORY_DIR": str(database.parent), "PYTHONPATH": str(ROOT)}
    script = ("import sys; sys.argv=['tam','report','--period','all','--tz','UTC'];"
              "from total_agent_memory.server import main_sync; main_sync()")
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, cwd=str(ROOT),
                            timeout=120, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("# Activity report: all projects")
    assert not (tmp_path / ".claude").exists() and not (tmp_path / ".codex").exists()


def test_mcp_stdio_serves_memory_report(tmp_path):
    env = {**os.environ, "HOME": str(tmp_path), "TAM_MEMORY_DIR": str(tmp_path / "mem"),
           "CLAUDE_MEMORY_DIR": str(tmp_path / "mem"), "MEMORY_MODE": "fast", "MCP_TRANSPORT": "stdio",
           "MEMORY_ASYNC_ENRICHMENT": "false", "MEMORY_LLM_ENABLED": "false"}
    frames = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "tam-tests", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "memory_save", "arguments": {"content": "Chose SQLite WAL mode for concurrent readers",
                                                 "type": "decision", "project": "demo",
                                                 "context": "Readers must not block the writer."}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
            "name": "memory_report", "arguments": {"project": "demo", "period": "day", "tz": "UTC"}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {
            "name": "memory_report", "arguments": {"period": "custom"}}},
    ]
    proc = subprocess.Popen([sys.executable, str(ROOT / "src" / "server.py")], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, cwd=str(ROOT), text=True)
    try:
        for frame in frames:
            proc.stdin.write(json.dumps(frame) + "\n")
        proc.stdin.flush()
        replies = {}
        while len(replies) < 5:
            line = proc.stdout.readline()
            assert line, f"server closed stdout after {len(replies)} replies"
            message = json.loads(line)
            if "id" in message:
                replies[message["id"]] = message
    finally:
        proc.stdin.close()
        proc.terminate()
        proc.wait(timeout=15)
    tools = {t["name"]: t for t in replies[2]["result"]["tools"]}
    assert tools["memory_report"]["inputSchema"]["properties"]["period"]["enum"] == ["day", "week", "month", "all",
                                                                                       "custom"]
    report = replies[4]["result"]
    assert report.get("isError") is not True
    text = report["content"][0]["text"]
    assert text.startswith("# Activity report: demo") and "Chose SQLite WAL mode for concurrent readers" in text
    assert "Readers must not block the writer." in text
    assert replies[5]["result"]["isError"] is True


@pytest.mark.postgres
def test_postgres_workspace_gives_the_same_report(db, tmp_path, pg_database):
    """The team gateway reads PostgreSQL workspaces with this module unchanged (compat connection)."""
    from tam_db import pg_connection
    from team_memory.database_config import open_control_plane
    from team_memory.database_contracts import DATABASE_URL_ENV

    plane = open_control_plane(tmp_path, {DATABASE_URL_ENV: pg_database.url})
    try:
        plane.workspaces.ensure("shared")
        with closing(pg_connection.connect(plane.workspaces.store_database("shared"))) as writer:
            seed(writer)
            writer.commit()
        for fields in ({}, {"period": "all"}, {"project": None, "period": "custom", "since": "2026-09-20", "until": "2026-09-24"}):
            with plane.workspace_reader("shared") as reader:
                assert render_markdown(build(reader, **fields)) == render_markdown(build(db, **fields)), fields
    finally:
        plane.close()

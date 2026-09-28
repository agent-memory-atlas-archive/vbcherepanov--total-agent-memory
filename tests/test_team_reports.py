import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from memory_reports.llm_summary import ReportSummarizer
from team_memory.contracts import Forbidden
from team_memory.registry import Registry
from team_memory.reports.contracts import TeamReportRequest
from team_memory.reports.service import TeamReportService
from team_memory.reports.tools import REPORT_TOOLS
from team_memory.service import TOOLS
from tests.team_db_helpers import seed_workspace
from tests.test_team_dashboard import USERS, build, seed, sign_in

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = """
CREATE TABLE knowledge (id INTEGER PRIMARY KEY, session_id TEXT, type TEXT, content TEXT, context TEXT DEFAULT '',
    project TEXT DEFAULT 'general', tags TEXT DEFAULT '[]', status TEXT DEFAULT 'active', superseded_by INTEGER,
    importance TEXT DEFAULT 'medium', created_at TEXT, last_confirmed TEXT);
CREATE TABLE tam_authorship (record_id INTEGER PRIMARY KEY, created_by TEXT NOT NULL, updated_by TEXT NOT NULL,
    revision INTEGER NOT NULL);
CREATE TABLE tam_history (sequence INTEGER PRIMARY KEY, record_id INTEGER NOT NULL, at TEXT NOT NULL,
    operation TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL, revision INTEGER NOT NULL,
    before_state TEXT, after_state TEXT);
CREATE TABLE errors (id INTEGER PRIMARY KEY, session_id TEXT, category TEXT, severity TEXT, description TEXT,
    context TEXT, fix TEXT, project TEXT, tags TEXT, status TEXT, created_at TEXT);
"""
# (scope key, [(user, type, content, context, days_ago)])
CONTENT = {
    "personal:dev": [("dev", "fact", "Dev private diary about the nebula", "", 0)],
    "personal:boss": [("boss", "decision", "Boss private salary note", "confidential", 1)],
    "team:eng": [("dev", "decision", "Deploy with blue-green on Tuesdays", "Rollbacks take one switch.", 1),
                 ("boss", "solution", "Pin the Helm chart version in CI", "Unpinned charts broke staging.", 2),
                 ("dev", "lesson", "Always drain nodes before upgrading", "", 3)],
    "team:ops": [("other", "fact", "Pager rotation changes on Mondays", "", 1)],
    "shared": [("root", "convention", "Company holidays are listed in the HR wiki", "", 2)],
}


@pytest.fixture(autouse=True)
def backend(team_backend):
    """Every test of this module runs on each selected team backend (--backend)."""
    return team_backend


def when(days_ago):
    return (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S.000000Z")


def actor(user_id):
    return json.dumps({"user_id": user_id, "display_name": user_id.title(), "client": "test", "org_role": "member"})


def workspace(registry: Registry, key: str, rows):
    statements = []
    for record_id, (user, kind, content, context, days_ago) in enumerate(rows, 1):
        at = when(days_ago)
        statements += [(("INSERT INTO knowledge (id,session_id,type,content,context,created_at,last_confirmed) "
                         "VALUES (?,?,?,?,?,?,?)"), (record_id, "workspace", kind, content, context, at, at)),
                       ("INSERT INTO tam_authorship VALUES (?,?,?,1)", (record_id, actor(user), actor(user))),
                       ("INSERT INTO tam_history (record_id,at,operation,actor,reason,revision) VALUES (?,?,?,?,?,1)",
                        (record_id, at, "insert", actor(user), ""))]
    if key.startswith("team_"):
        statements.append((("INSERT INTO errors VALUES (1,'workspace','bug','high','Helm upgrade timed out',"
                            "'root_cause: slow hooks | pattern: helm-timeout','Raise the timeout','general',?,'open',?)"),
                           (json.dumps(["pattern:helm-timeout", "file:deploy/chart.yaml"]), when(1))))
    seed_workspace(registry, key, SCHEMA, statements)


def key_of(registry, scope):
    kind, _, name = scope.partition(":")
    if kind == "personal":
        return "personal_" + Registry.digest(name)
    return registry.team_workspace_key(name) if kind == "team" else "shared"


@pytest.fixture
def world(tmp_path):
    registry, accounts, _settings, _pool, dashboard, app = build(tmp_path / "root")
    seed(registry, accounts)
    for scope, rows in CONTENT.items():
        workspace(registry, key_of(registry, scope), rows)
    with TestClient(app) as client:
        yield {"registry": registry, "client": client, "dashboard": dashboard}


def report(client, **params):
    return client.get("/reports/api/report", params={"period": "all", **params})


MATRIX = [
    ({"scope": "personal"}, set(USERS)),
    ({"scope": "team", "team_id": "eng"}, {"root", "audit", "boss"}),
    ({"scope": "team", "team_id": "ops"}, {"root", "audit"}),
    ({"scope": "team", "team_id": "nope"}, set()),
    ({"scope": "company"}, {"root", "audit"}),
]


def test_authorization_matrix(world):
    client = world["client"]
    for user_id in USERS:
        sign_in(client, user_id)
        for params, allowed in MATRIX:
            response = report(client, **params)
            assert response.status_code == (200 if user_id in allowed else 403), (user_id, params, response.text)
            download = client.get("/reports/api/report.md", params={"period": "week", **params})
            assert download.status_code == (200 if user_id in allowed else 403), (user_id, params)
    client.cookies.clear()
    for params, _allowed in MATRIX:
        assert report(client, **params).status_code == 401
    assert client.get("/reports/api/options").status_code == 401


def test_personal_memory_stays_private(world):
    client = world["client"]
    sign_in(client, "dev")
    mine = report(client, scope="personal").json()["report"]
    assert mine["scope"] == "personal:dev" and mine["summary"]["records"] == 1
    assert "diary" in json.dumps(mine) and "salary" not in json.dumps(mine)
    for user_id in ("root", "audit", "boss"):
        sign_in(client, user_id)
        texts = [report(client, scope="personal").text]
        texts += [report(client, **params).text for params, allowed in MATRIX[1:] if user_id in allowed]
        texts += [client.get("/reports/api/report.md", params={"period": "all", "scope": "company"}).text]
        joined = " ".join(texts)
        assert "diary" not in joined and "nebula" not in joined
        if user_id != "boss":
            assert "salary" not in joined


def test_department_report_content_and_contributors(world):
    client = world["client"]
    sign_in(client, "boss")
    data = report(client, scope="team", team_id="eng").json()["report"]
    assert data["scope"] == "team:eng" and data["summary"]["records"] == 3 and data["summary"]["errors"] == 1
    assert [i["title"] for i in data["decisions"]["items"]] == ["Deploy with blue-green on Tuesdays"]
    assert data["decisions"]["items"][0]["author"] == "Dev" and data["decisions"]["items"][0]["why"]
    assert {(c["user_id"], c["saves"]) for c in data["contributors"]} == {("dev", 2), ("boss", 1)}
    assert data["error_patterns"][0]["pattern"] == "helm-timeout"
    assert data["files"]["items"][0]["name"] == "deploy/chart.yaml"
    assert "Pager rotation" not in json.dumps(data)


def test_company_report_covers_departments_and_shared_only(world):
    client = world["client"]
    sign_in(client, "audit")
    data = report(client, scope="company").json()["report"]
    assert data["scope"] == "company" and data["summary"]["records"] == 5
    workspaces = {i["workspace"] for key in ("decisions", "solutions", "lessons") for i in data[key]["items"]}
    assert workspaces == {"team:eng"}
    timeline = json.dumps(data["timeline"])
    assert "Pager rotation" in timeline and "HR wiki" in timeline
    assert {c["user_id"] for c in data["contributors"]} == {"dev", "boss", "other", "root"}
    assert all("team:" in p["pattern"] for p in data["error_patterns"])


def test_options_follow_roles(world):
    client = world["client"]
    expected = {"dev": ([], False), "other": ([], False), "boss": (["eng"], False), "audit": (["eng", "ops"], True),
                "root": (["eng", "ops"], True)}
    for user_id, (teams, company) in expected.items():
        sign_in(client, user_id)
        options = client.get("/reports/api/options").json()
        assert [t["team_id"] for t in options["teams"]] == teams and options["company"] is company
        sections = client.get("/dashboard/api/session").json()["sections"]
        assert "reports" in {s["id"] for s in sections}


def test_download_is_markdown_attachment(world):
    client = world["client"]
    sign_in(client, "boss")
    response = client.get("/reports/api/report.md", params={"scope": "team", "team_id": "eng", "period": "month",
                                                             "project": "general", "tz": "UTC"})
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/markdown")
    disposition = response.headers["content-disposition"]
    assert re.fullmatch(r'attachment; filename="report-team-eng-general-month-\d{4}-\d{2}-01_\d{4}-\d{2}-\d{2}\.md"',
                        disposition), disposition
    assert response.text.startswith("# Activity report: general · team:eng")
    assert response.headers["content-security-policy"].startswith("default-src 'none'")


def test_invalid_requests_are_400(world):
    client = world["client"]
    sign_in(client, "root")
    for params in ({"scope": "team"}, {"scope": "personal", "team_id": "eng"}, {"period": "custom"},
                   {"tz": "Mars/Base"}, {"period": "custom", "since": "2026-09-10", "until": "2026-09-01"},
                   {"scope": "galaxy"}):
        response = client.get("/reports/api/report", params={"period": "all", **params})
        assert response.status_code == 400, (params, response.text)


def test_static_assets(world):
    client = world["client"]
    assert client.get("/reports/static/reports.js").status_code == 200
    assert client.get("/reports/static/reports.css").headers["content-type"].startswith("text/css")
    assert client.get("/reports/static/other.js").status_code == 404


def test_mcp_tool_is_in_the_team_catalog(world):
    client, registry = world["client"], world["registry"]
    assert "memory_report" in TOOLS and REPORT_TOOLS["memory_report"][0] is TeamReportRequest
    token = registry.issue_token("boss", "test")
    reply = client.post("/api/call", headers={"Authorization": "Bearer " + token},
                        json={"name": "memory_report", "arguments": {"scope": "team", "team_id": "eng", "period": "all"}})
    assert reply.status_code == 200 and reply.json()["markdown"].startswith("# Activity report: all projects · team:eng")
    dev = registry.issue_token("dev", "test")
    denied = client.post("/api/call", headers={"Authorization": "Bearer " + dev},
                         json={"name": "memory_report", "arguments": {"scope": "company"}})
    assert denied.status_code == 400 and denied.json()["code"] == "forbidden"
    structured = client.post("/api/call", headers={"Authorization": "Bearer " + dev},
                             json={"name": "memory_report", "arguments": {"format": "json", "period": "all"}})
    assert structured.json()["report"]["scope"] == "personal:dev"


def test_service_llm_summary_and_missing_workspaces(tmp_path):
    registry = Registry(tmp_path)
    registry.add_user("chief", "Chief")
    registry.set_org_role("chief", "company_viewer")
    registry.add_team("empty", "Empty")
    chief = registry.authenticate(registry.issue_token("chief", "test"))
    calls = []

    class LLM:
        def complete(self, text, **_kwargs):
            calls.append(text)
            return "Quiet week."

    service = TeamReportService(registry, summarizer=ReportSummarizer(lambda: LLM()))
    quiet = service.build(chief, TeamReportRequest(scope="company", period="all", include_llm_summary=True))
    assert quiet.empty and quiet.llm_summary_error and not calls
    workspace(registry, registry.team_workspace_key("empty"), [("chief", "fact", "First note", "", 0)])
    busy = service.build(chief, TeamReportRequest(scope="team", team_id="empty", period="day", tz="UTC",
                                                  include_llm_summary=True))
    assert busy.llm_summary == "Quiet week." and len(calls) == 1
    registry.add_user("pleb", "Pleb")
    pleb = registry.authenticate(registry.issue_token("pleb", "test"))
    with pytest.raises(Forbidden):
        service.build(pleb, TeamReportRequest(scope="team", team_id="empty"))


def test_report_skill_is_well_formed():
    text = (ROOT / "skills" / "report" / "SKILL.md").read_text()
    front = text.split("---")[1]
    assert "name: report" in front and "description:" in front and "argument-hint:" in front
    referenced = set(re.findall(r"`((?:memory)_[a-z_]+)", text))
    assert {"memory_report", "memory_get"} <= referenced <= set(TOOLS)
    assert (ROOT / "skills" / "report" / "agents" / "openai.yaml").is_file()

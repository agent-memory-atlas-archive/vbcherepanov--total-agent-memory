"""Gateway-side LLM features (onboarding, report summaries) use the dashboard provider settings.

Precedence matches the workers: web (dashboard) > gateway environment > default. Nothing is written into
os.environ, changes apply to the next call, and keys never reach logs or responses.
"""
import json
import logging
import os
import threading
import urllib.error

import httpx
import pytest
from starlette.testclient import TestClient

import llm_provider
from memory_core.telemetry import counters
from memory_reports.llm_summary import NO_LLM, ReportSummarizer
from team_memory.gateway_llm import GatewayLLM, resolve
from team_memory.learning.contracts import LLMGrade
from team_memory.learning.repository import LearningRepository
from team_memory.learning.service import LearningService
from team_memory.learning.sources import Caller
from team_memory.learning.tools import LEARNING_TOOLS
from team_memory.registry import Registry
from team_memory.reports.contracts import TeamReportRequest
from team_memory.reports.service import TeamReportService
from team_memory.service import MemoryService
from team_memory.settings import SettingsStore, load_cipher
from team_memory.worker import WorkerPool
from tests.test_team_learning import (
    Clock,
    FakeSource,
    publish,
    publish_quiz,
    study_module,
)
from tests.test_team_reports import workspace

WEB_KEY = "sk-web-dashboard-only-secret-9999WEBK"
ENV_KEY = "sk-env-gateway-secret-8888ENVK"
SUMMARY = "Busy week: blue-green deploys were adopted."


class FakeHTTP:
    """Stands in for llm_provider._http_post_json and records every call."""

    def __init__(self, fail: bool = False):
        self.calls: list[dict] = []
        self.fail = fail
        self.lock = threading.Lock()

    def __call__(self, url, body, headers, timeout):
        with self.lock:
            self.calls.append({"url": url, "body": body, "headers": dict(headers)})
        if self.fail:
            raise urllib.error.HTTPError(url, 401, "Unauthorized", None, None)
        grade = LLMGrade(score=0.5, comment="Partly right").model_dump()
        structured = "response_format" in body or "tools" in body or "format" in body
        if url.endswith("/messages"):
            if structured:
                return {"content": [{"type": "tool_use", "name": llm_provider.STRUCTURED_TOOL_NAME, "input": grade}]}
            return {"content": [{"type": "text", "text": SUMMARY}]}
        if url.endswith("/chat/completions"):
            content = json.dumps(grade) if structured else SUMMARY
            return {"choices": [{"message": {"content": content}}]}
        return {"response": json.dumps(grade) if structured else SUMMARY}

    def key(self, index: int = -1) -> str | None:
        headers = self.calls[index]["headers"]
        if "x-api-key" in headers:
            return headers["x-api-key"]
        auth = headers.get("Authorization", "")
        return auth.removeprefix("Bearer ") or None


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def http(monkeypatch):
    fake = FakeHTTP()
    monkeypatch.setattr(llm_provider, "_http_post_json", fake)
    llm_provider._clear_available_cache()
    return fake


@pytest.fixture
def registry(tmp_path):
    return Registry(tmp_path)


def store(registry: Registry, environ: dict[str, str]) -> SettingsStore:
    return SettingsStore(registry, load_cipher(registry.root, {}), environ)


def report_service(registry: Registry, llm: GatewayLLM) -> tuple[TeamReportService, object]:
    registry.add_user("chief", "Chief")
    registry.set_org_role("chief", "company_viewer")
    registry.add_team("eng", "Engineering")
    workspace(registry, registry.team_workspace_key("eng"),
              [("chief", "decision", "Deploy with blue-green on Tuesdays", "Rollbacks take one switch.", 0)])
    return TeamReportService(registry, ReportSummarizer(llm)), registry.authenticate(registry.issue_token("chief", "t"))


def summarize(service: TeamReportService, chief):
    return service.build(chief, TeamReportRequest(scope="team", team_id="eng", period="all", tz="UTC",
                                                  include_llm_summary=True))


@pytest.mark.anyio
async def test_dashboard_only_key_grades_onboarding_answers(registry, http, caplog):
    caplog.set_level(logging.DEBUG)
    settings = store(registry, {})
    settings.update({"MEMORY_LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": WEB_KEY})
    for user in ("boss", "vasya"):
        registry.add_user(user, user.title())
    registry.add_team("engineering", "Engineering")
    registry.membership("boss", "engineering", "manager")
    registry.membership("vasya", "engineering", "reader")
    source = FakeSource(registry)
    source.add("engineering", 1, "Deployments go through the blue-green pipeline every Tuesday.", project="deploy")
    source.add("engineering", 2, "We chose PostgreSQL for durable records.", project="deploy", kind="decision")
    source.add("engineering", 3, "Incident calls use the #war-room channel.")
    clock = Clock()
    service = LearningService(registry, LearningRepository(registry.root), source, GatewayLLM(settings), clock)
    callers = {user: Caller(registry.authenticate(registry.issue_token(user, "test")), "token-" + user)
               for user in ("boss", "vasya")}

    async def call(user, name, **arguments):
        return await service.call(callers[user], name, LEARNING_TOOLS[name][0].model_validate(arguments))

    world = {"call": call, "clock": clock}
    _, modules = await publish(world)
    await publish_quiz(world, modules[0])
    await study_module(world, "vasya", modules[0])
    ids = [q["id"] for q in (await call("vasya", "onboarding_quiz", module_id=modules[0]["id"]))["questions"]]
    result = await call("vasya", "onboarding_submit", module_id=modules[0]["id"],
                        answers=[{"question_id": ids[2], "text": "Records must be durable."}])

    assert result["results"][2]["grader"] == "llm" and result["results"][2]["earned"] == 1.0
    assert len(http.calls) == 1 and http.calls[0]["url"] == "https://api.anthropic.com/v1/messages"
    assert http.key() == WEB_KEY and http.calls[0]["body"]["model"] == "claude-haiku-4-5"
    assert WEB_KEY not in json.dumps(result) and WEB_KEY not in caplog.text
    assert WEB_KEY not in os.environ.values()


def test_dashboard_only_key_writes_report_summary(registry, http, caplog):
    caplog.set_level(logging.DEBUG)
    settings = store(registry, {})
    settings.update({"MEMORY_LLM_PROVIDER": "openai", "OPENAI_API_KEY": WEB_KEY, "MEMORY_LLM_ENABLED": "true",
                     "MEMORY_LLM_MODEL": "gpt-test"})
    service, chief = report_service(registry, GatewayLLM(settings))
    report = summarize(service, chief)
    assert report.llm_summary == SUMMARY and report.llm_summary_error is None
    assert http.calls[0]["url"] == "https://api.openai.com/v1/chat/completions"
    assert http.key() == WEB_KEY and http.calls[0]["body"]["model"] == "gpt-test"
    assert WEB_KEY not in report.model_dump_json() and WEB_KEY not in caplog.text


def test_env_only_key_still_works(registry, http):
    env = {"MEMORY_LLM_PROVIDER": "openai", "OPENAI_API_KEY": ENV_KEY, "MEMORY_LLM_ENABLED": "true"}
    service, chief = report_service(registry, GatewayLLM(store(registry, env)))
    assert summarize(service, chief).llm_summary == SUMMARY
    assert http.key() == ENV_KEY and http.calls[0]["body"]["model"] == "gpt-4o-mini"


def test_web_overrides_env_and_applies_without_restart(registry, http):
    env = {"MEMORY_LLM_PROVIDER": "openai", "OPENAI_API_KEY": ENV_KEY, "MEMORY_LLM_ENABLED": "true"}
    settings = store(registry, env)
    service, chief = report_service(registry, GatewayLLM(settings))
    before = dict(os.environ)

    summarize(service, chief)
    assert http.key() == ENV_KEY
    settings.update({"OPENAI_API_KEY": WEB_KEY})
    summarize(service, chief)
    assert http.key() == WEB_KEY
    settings.update({"MEMORY_LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": WEB_KEY})
    summarize(service, chief)
    assert http.calls[-1]["url"].startswith("https://api.anthropic.com/") and http.key() == WEB_KEY
    settings.update({"MEMORY_LLM_ENABLED": "false"})
    calls = len(http.calls)
    assert summarize(service, chief).llm_summary_error == NO_LLM and len(http.calls) == calls
    settings.update({"MEMORY_LLM_ENABLED": None, "MEMORY_LLM_PROVIDER": None, "OPENAI_API_KEY": None})
    summarize(service, chief)
    assert http.calls[-1]["url"].startswith("https://api.openai.com/") and http.key() == ENV_KEY
    assert dict(os.environ) == before


def test_concurrent_gateways_do_not_share_settings(tmp_path, http):
    first = GatewayLLM(store(Registry(tmp_path / "a"), {"MEMORY_LLM_PROVIDER": "openai", "OPENAI_API_KEY": ENV_KEY,
                                                        "MEMORY_LLM_ENABLED": "true"}))
    second_store = store(Registry(tmp_path / "b"), {})
    second_store.update({"MEMORY_LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": WEB_KEY})
    second = GatewayLLM(second_store)
    keys: dict[str, set[str]] = {"first": set(), "second": set()}

    def run(name, gateway):
        for _ in range(20):
            provider = gateway()
            keys[name].add(provider.api_key)

    threads = [threading.Thread(target=run, args=("first", first)), threading.Thread(target=run, args=("second", second))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert keys == {"first": {ENV_KEY}, "second": {WEB_KEY}}


def test_provider_failure_never_leaks_key(registry, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    failing = FakeHTTP(fail=True)
    monkeypatch.setattr(llm_provider, "_http_post_json", failing)
    settings = store(registry, {})
    settings.update({"MEMORY_LLM_PROVIDER": "openai", "OPENAI_API_KEY": WEB_KEY, "MEMORY_LLM_ENABLED": "true"})
    service, chief = report_service(registry, GatewayLLM(settings))
    report = summarize(service, chief)
    assert report.llm_summary is None and report.llm_summary_error == "LLM call failed (HTTPError)"
    assert failing.key() == WEB_KEY
    assert WEB_KEY not in report.model_dump_json() and WEB_KEY not in caplog.text


def test_resolved_config_hides_key():
    config = resolve({"MEMORY_LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": WEB_KEY})
    assert config.provider == "anthropic" and config.base == "https://api.anthropic.com/v1"
    assert config.api_key.get_secret_value() == WEB_KEY
    for text in (repr(config), str(config), config.model_dump_json(), json.dumps(config.log_fields())):
        assert WEB_KEY not in text


@pytest.mark.parametrize("env,expected", [
    ({}, ("ollama", "http://localhost:11434", "qwen2.5-coder:7b")),
    ({"OLLAMA_URL": "http://gpu:11434/"}, ("ollama", "http://gpu:11434", "qwen2.5-coder:7b")),
    ({"MEMORY_LLM_PROVIDER": "auto", "ANTHROPIC_API_KEY": "k"}, ("anthropic", "https://api.anthropic.com/v1",
                                                                  "claude-haiku-4-5")),
    ({"MEMORY_LLM_PROVIDER": "openai-compatible", "MEMORY_LLM_API_BASE": "http://lm:1234/v1",
      "MEMORY_LLM_MODEL": "local"}, ("openai-compatible", "http://lm:1234/v1", "local")),
    ({"MEMORY_LLM_PROVIDER": "bogus"}, ("ollama", "http://localhost:11434", "qwen2.5-coder:7b")),
])
def test_resolution_matches_worker_defaults(env, expected):
    config = resolve(env)
    assert (config.provider, config.base, config.model) == expected


@pytest.mark.parametrize("values", [
    {"MEMORY_LLM_ENABLED": "false", "MEMORY_LLM_PROVIDER": "openai", "OPENAI_API_KEY": WEB_KEY},
    {"MEMORY_LLM_PROVIDER": "openai", "MEMORY_LLM_ENABLED": "true"},
    {"MEMORY_LLM_PROVIDER": "openai-compatible", "MEMORY_LLM_API_BASE": "http://lm:1234/v1",
     "MEMORY_LLM_ENABLED": "true"},
])
def test_disabled_or_incomplete_settings_give_no_provider(registry, values):
    settings = store(registry, {})
    settings.update(values)
    before = counters.get("gateway_llm_unavailable")
    assert GatewayLLM(settings)() is None
    assert counters.get("gateway_llm_unavailable") == before + 1


def test_auto_mode_probes_ollama_with_ttl_cache(registry):
    probes: list[str] = []
    installed = {"models": [{"name": "qwen2.5-coder:7b"}]}

    def handler(request: httpx.Request) -> httpx.Response:
        probes.append(str(request.url))
        return httpx.Response(200, json=installed)

    now = [1000.0]
    settings = store(registry, {"OLLAMA_URL": "http://ollama.test:11434"})
    gateway = GatewayLLM(settings, transport=httpx.MockTransport(handler), clock=lambda: now[0], probe_ttl=30)
    provider = gateway()
    assert isinstance(provider, llm_provider.OllamaProvider)
    assert provider.api_base == "http://ollama.test:11434" and provider._default_model == "qwen2.5-coder:7b"
    assert gateway() is not None and probes == ["http://ollama.test:11434/api/tags"]
    settings.update({"MEMORY_LLM_MODEL": "llama3:8b"})
    assert gateway() is None and len(probes) == 2
    installed["models"].append({"name": "llama3:8b"})
    assert gateway() is None and len(probes) == 2
    now[0] += 31
    assert gateway() is not None and len(probes) == 3


def test_auto_mode_ollama_unreachable(registry, caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    gateway = GatewayLLM(store(registry, {}), transport=httpx.MockTransport(handler))
    assert gateway() is None
    assert "gateway_llm_probe_failed" in caplog.text


def test_memory_service_wires_one_gateway_into_learning_and_reports(registry):
    gateway = GatewayLLM(store(registry, {}))
    service = MemoryService(registry, WorkerPool(registry.root, maximum=1), llm=gateway)
    assert service.learning.llm_factory is gateway and service.reports.summarizer.factory is gateway
    default = MemoryService(registry, WorkerPool(registry.root, maximum=1))
    assert isinstance(default.learning.llm_factory, GatewayLLM)
    assert default.reports.summarizer.factory is default.learning.llm_factory


def test_key_saved_in_dashboard_reaches_report_summary_over_http(tmp_path, http, caplog):
    from team_memory.accounts import AccountPolicy, Accounts
    from team_memory.app import create_app
    from team_memory.dashboard_service import DashboardService
    from team_memory.metrics import Metrics
    from tests.test_team_dashboard import PASSWORD, sign_in

    caplog.set_level(logging.DEBUG)
    registry = Registry(tmp_path)
    accounts = Accounts(registry, AccountPolicy())
    settings = store(registry, {"MEMORY_LLM_PROVIDER": "openai", "OPENAI_API_KEY": ENV_KEY})
    pool = WorkerPool(registry.root, maximum=1, environment=settings.overrides)
    service = MemoryService(registry, pool, llm=GatewayLLM(settings))
    dashboard = DashboardService(registry, accounts, service, settings, Metrics())
    registry.add_user("root", "Root")
    registry.set_org_role("root", "superadmin")
    accounts.redeem_invite("root", accounts.issue_invite("root").code, PASSWORD, "127.0.0.1")
    registry.add_team("eng", "Engineering")
    workspace(registry, registry.team_workspace_key("eng"),
              [("root", "decision", "Deploy with blue-green on Tuesdays", "", 0)])
    params = {"scope": "team", "team_id": "eng", "period": "all", "tz": "UTC", "include_llm_summary": "true"}
    with TestClient(create_app(service, dashboard)) as client:
        headers = sign_in(client, "root")
        saved = client.post("/dashboard/api/admin/settings", headers=headers,
                            json={"values": {"OPENAI_API_KEY": WEB_KEY, "MEMORY_LLM_ENABLED": "true"}})
        assert saved.status_code == 200 and WEB_KEY not in saved.text
        reply = client.get("/reports/api/report", params=params)
        assert reply.status_code == 200, reply.text
        assert reply.json()["report"]["llm_summary"] == SUMMARY and http.key() == WEB_KEY
        view = client.get("/dashboard/api/admin/settings")
        assert WEB_KEY not in reply.text and WEB_KEY not in view.text
    assert WEB_KEY not in caplog.text and ENV_KEY not in caplog.text
    assert WEB_KEY not in os.environ.values()

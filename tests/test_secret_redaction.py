"""Credentials never reach disk: stored records, the raw call log, prompts, queued tool output."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from tests.pg_store_support import store_backend  # noqa: F401 — fixture

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from secret_redaction import REDACTED, redact_secrets, redact_value

# Key-shaped values are split in the source so secret scanners do not take them for real keys.
SECRETS = {
    "stripe": "sk_" + "live_" + "51HxYzAbCdEfGhIjKlMnOpQrStUvWx",
    "anthropic": "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
    "openai": "sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
    "github_fine_grained": "github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz",
    "github_classic": "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",
    "gitlab": "glpat-AbCdEfGhIjKlMnOpQrSt",
    "slack": "xox" + "b-1234567890-0987654321-AbCdEfGhIjKlMnOpQrSt",
    "google": "AIza" + "SyAbCdEfGhIjKlMnOpQrStUvWxYz0123456",
    "huggingface": "hf_AbCdEfGhIjKlMnOpQrStUvWxYz01234567",
    "aws_key_id": "AKIAIOSFODNN7EXAMPLE",
}
# Values that must disappear although their surrounding text is not a key format.
EMBEDDED = {
    "url_password": ("postgres://app:S3cretDsnPass@db:5432/shop", "S3cretDsnPass"),
    "pem_body": ("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAA\n-----END OPENSSH PRIVATE KEY-----",
                 "b3BlbnNzaC1rZXktdjEAAAA"),
    "aws_secret": ("aws_secret_access_key = wJalrXUtnFEMIK7MDENGbPxRfiCY", "wJalrXUtnFEMIK7MDENGbPxRfiCY"),
    "password": ("password=Hunter2Secret!", "Hunter2Secret"),
    "client_secret": ("CLIENT_SECRET: abcdef123456", "abcdef123456"),
}
PROBE = "creds: " + " ; ".join([*SECRETS.values(), *(text for text, _ in EMBEDDED.values())])
NEEDLES = [*SECRETS.values(), *(needle for _, needle in EMBEDDED.values())]


def leaked(blob: str) -> list[str]:
    return [needle for needle in NEEDLES if needle in blob]


@pytest.mark.parametrize("name", sorted(SECRETS))
def test_key_formats_are_redacted_whole(name):
    text, changed = redact_secrets(f"use {SECRETS[name]} here")
    assert changed
    assert text == f"use {REDACTED} here"


@pytest.mark.parametrize("name", sorted(EMBEDDED))
def test_embedded_secret_values_are_redacted(name):
    text, changed = redact_secrets(EMBEDDED[name][0])
    assert changed
    assert EMBEDDED[name][1] not in text


def test_url_keeps_host_after_redacting_credentials():
    assert redact_secrets("postgres://app:pw@db:5432/shop")[0] == f"postgres://{REDACTED}@db:5432/shop"


@pytest.mark.parametrize("text", [
    "max_tokens: 4096",
    "def f(token_count=3): pass",
    "We use PostgreSQL 18 on port 5433",
    "see https://github.com/vbcherepanov/total-agent-memory",
    "password reset flow: send a link",
    "Moved order events to Kafka; token budget 7k",
])
def test_ordinary_text_is_untouched(text):
    assert redact_secrets(text) == (text, False)


def test_redact_value_walks_nested_arguments_and_skips_paths():
    value, changed = redact_value({
        "content": f"key {SECRETS['stripe']}",
        "tags": [SECRETS["anthropic"]],
        "options": [{"pros": [f"pw {EMBEDDED['password'][0]}"]}],
        "path": "/Users/dev@corp.example/project",
        "limit": 5,
    })
    assert changed
    assert not leaked(json.dumps(value))
    assert value["path"] == "/Users/dev@corp.example/project"
    assert value["limit"] == 5


@pytest.fixture
def live_server(store_backend, monkeypatch, tmp_path):  # noqa: F811 — pytest fixture injection
    for sub in ("raw", "blobs", "chroma"):
        (tmp_path / sub).mkdir(exist_ok=True)
    import server
    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    store = server.Store(database=store_backend)
    monkeypatch.setattr(server, "store", store)
    monkeypatch.setattr(server, "recall", server.Recall(store))
    monkeypatch.setattr(server, "SID", "sess-secrets")
    monkeypatch.setattr(server, "BRANCH", "")
    monkeypatch.setattr(server, "_v5_modules", {})
    store.db.execute("INSERT INTO sessions (id, started_at, project, status) VALUES (?, ?, ?, ?)",
                     (server.SID, "2026-09-28T00:00:00Z", "p", "open"))
    store.db.commit()
    yield server, store, tmp_path
    store.db.close()


def call(server, tool, **args):
    content, is_error = asyncio.run(server._call_tool_impl(tool, args))
    return content[0].text, is_error


def test_every_write_tool_stores_no_secret(live_server):
    server, store, root = live_server
    writes = {
        "memory_save": {"type": "fact", "project": "p", "content": PROBE, "context": PROBE},
        "memory_save_fast": {"type": "fact", "project": "p", "content": "fast " + PROBE},
        "memory_observe": {"tool_name": "Bash", "summary": PROBE, "observation_type": "change", "project": "p"},
        "memory_episode_save": {"narrative": PROBE, "outcome": "routine", "project": "p",
                                "concepts": ["x"], "approaches_tried": [PROBE], "key_insight": PROBE},
        "session_end": {"session_id": "s1", "project": "p", "summary": PROBE, "highlights": [PROBE],
                        "pitfalls": [PROBE], "next_steps": [PROBE]},
        "kg_add_fact": {"subject": "p", "predicate": "uses_key", "object": PROBE, "context": PROBE, "project": "p"},
        "self_error_log": {"description": PROBE, "category": "config", "severity": "low", "fix": PROBE,
                           "context": PROBE, "project": "p"},
        "learn_error": {"file": "a.py", "error": PROBE, "root_cause": PROBE, "fix": PROBE,
                        "pattern": "p1", "project": "p"},
        "self_reflect": {"reflection": PROBE, "task_summary": PROBE, "outcome": "success", "project": "p"},
        "save_intent": {"prompt": PROBE, "project": "p", "session_id": "s1"},
        "workflow_learn": {"name": "w", "steps": [PROBE], "trigger_keywords": ["k"], "project": "p"},
        "self_insight": {"action": "add", "content": PROBE, "category": "config", "project": "p"},
    }
    for name, args in writes.items():
        text, is_error = call(server, name, **args)
        assert not is_error, (name, text)
        assert not leaked(text), name

    tables = [r[0] for r in store.db.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
        if store.is_postgres else "SELECT name FROM sqlite_master WHERE type='table'")]
    for table in tables:
        rows = store.db.execute(f'SELECT * FROM "{table}"').fetchall()
        assert not leaked(json.dumps([tuple(r) for r in rows], default=str)), table

    for log in (root / "raw").glob("*.jsonl"):
        assert not leaked(log.read_text(encoding="utf-8")), log.name


def test_intent_hook_path_redacts_the_prompt(tmp_path):
    import intents
    db = tmp_path / "memory.db"
    assert intents.save_intent(db, PROBE, "s1", "p")
    rows = sqlite3.connect(db).execute("SELECT * FROM intents").fetchall()
    assert rows
    assert not leaked(json.dumps(rows, default=str))


def test_tool_observation_queue_redacts_output(tmp_path):
    from auto_extract_active import capture_tool_observation
    path = capture_tool_observation("Bash", "cat .env\n" + PROBE, session_id="s1", project="p",
                                    queue_dir=tmp_path)
    assert path is not None
    assert not leaked(path.read_text(encoding="utf-8"))


def test_transcript_sanitize_uses_the_shared_patterns():
    from extract_transcript import sanitize
    assert not leaked(sanitize(PROBE))


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_team_gateway_redacts_before_the_workspace(team_backend, tmp_path, monkeypatch):
    from team_memory.contracts import DomainError
    from team_memory.registry import Registry
    from team_memory.service import MemoryService
    from team_memory.worker import WorkerPool
    monkeypatch.setenv("MEMORY_LLM_ENABLED", "false")
    monkeypatch.setenv("MEMORY_QUALITY_GATE_ENABLED", "false")
    monkeypatch.setenv("MEMORY_MODE", "fast")
    registry = Registry(tmp_path)
    registry.add_user("vasya", "Вася")
    token = registry.issue_token("vasya", "codex")
    service = MemoryService(registry, WorkerPool(registry.root, maximum=1))
    try:
        saved = await service.call(token, "memory_save", {"content": PROBE, "context": PROBE, "tags": ["t"]})
        assert not leaked(json.dumps(saved, ensure_ascii=False))
        found = await service.call(token, "memory_get", {"id": saved["data"]["id"]})
        assert not leaked(json.dumps(found, ensure_ascii=False))
        with pytest.raises((DomainError, ValueError)):
            await service.call(token, "memory_save", {"content": "   "})
    finally:
        service.pool.close()

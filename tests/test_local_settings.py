"""Personal settings saved from the local dashboard: encrypted secrets, precedence, startup overlay."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import erasure
import local_settings
from settings_catalog import SettingError

KEY = "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"


def test_secret_is_encrypted_at_rest_and_masked_in_the_view(tmp_path):
    store = local_settings.LocalSettings(tmp_path, {})
    assert store.update({"ANTHROPIC_API_KEY": KEY, "MEMORY_LLM_PROVIDER": "anthropic"}) == \
        ["ANTHROPIC_API_KEY", "MEMORY_LLM_PROVIDER"]
    raw = (tmp_path / "settings.json").read_text()
    assert KEY not in raw
    if os.name == "posix":
        assert stat.S_IMODE((tmp_path / "settings.json").stat().st_mode) == 0o600
        assert stat.S_IMODE((tmp_path / "master.key").stat().st_mode) == 0o600
    views = {v.key: v for v in store.view()}
    assert views["ANTHROPIC_API_KEY"].value is None
    assert views["ANTHROPIC_API_KEY"].hint == "••••6789"
    assert views["ANTHROPIC_API_KEY"].source == "web"
    assert views["MEMORY_LLM_PROVIDER"].value == "anthropic"
    assert store.overrides() == {"ANTHROPIC_API_KEY": KEY, "MEMORY_LLM_PROVIDER": "anthropic"}


def test_saved_value_beats_environment_and_clearing_restores_it(tmp_path):
    env = {"MEMORY_RECALL_MAX_RESULT_CHARS": "9000", "OPENAI_API_KEY": "sk-env-" + "x" * 30}
    store = local_settings.LocalSettings(tmp_path, env)
    views = {v.key: v for v in store.view()}
    assert (views["MEMORY_RECALL_MAX_RESULT_CHARS"].source, views["MEMORY_RECALL_MAX_RESULT_CHARS"].value) == ("env", "9000")
    assert views["OPENAI_API_KEY"].value is None and views["OPENAI_API_KEY"].hint.startswith("••••")
    store.update({"MEMORY_RECALL_MAX_RESULT_CHARS": "1500"})
    target = dict(env)
    assert local_settings.apply(target, tmp_path) == ["MEMORY_RECALL_MAX_RESULT_CHARS"]
    assert target["MEMORY_RECALL_MAX_RESULT_CHARS"] == "1500"
    store.update({"MEMORY_RECALL_MAX_RESULT_CHARS": None})
    assert {v.key: v.source for v in store.view()}["MEMORY_RECALL_MAX_RESULT_CHARS"] == "env"


@pytest.mark.parametrize(("key", "value"), [
    ("MEMORY_RECALL_MAX_RESULT_CHARS", "0"),
    ("MEMORY_FLAG_INSTRUCTIONS", "maybe"),
    ("MEMORY_LLM_API_BASE", "http://user:pw@host/v1"),
    ("TAM_MODEL_CACHE", "relative/dir"),
    ("NOT_A_SETTING", "1"),
])
def test_invalid_changes_are_refused_and_nothing_is_written(tmp_path, key, value):
    store = local_settings.LocalSettings(tmp_path, {})
    with pytest.raises(SettingError):
        store.update({"MEMORY_LLM_PROVIDER": "ollama", key: value})
    assert not (tmp_path / "settings.json").exists()


@pytest.mark.skipif(os.name != "posix", reason="Windows protects the profile with ACLs, not mode bits")
def test_a_readable_master_key_is_refused(tmp_path):
    store = local_settings.LocalSettings(tmp_path, {})
    store.update({"OPENAI_API_KEY": KEY})
    os.chmod(tmp_path / "master.key", 0o644)
    with pytest.raises(PermissionError):
        store.update({"ANTHROPIC_API_KEY": KEY})
    assert local_settings.apply({}, tmp_path) == []


def test_a_foreign_master_key_leaves_the_secret_unreadable(tmp_path):
    store = local_settings.LocalSettings(tmp_path, {})
    store.update({"OPENAI_API_KEY": KEY, "MEMORY_LLM_PROVIDER": "openai"})
    (tmp_path / "master.key").unlink()
    local_settings.LocalSettings(tmp_path, {}).update({"COHERE_API_KEY": "co-" + "y" * 30})
    views = {v.key: v for v in local_settings.LocalSettings(tmp_path, {}).view()}
    assert views["OPENAI_API_KEY"].readable is False
    env: dict[str, str] = {}
    assert local_settings.apply(env, tmp_path) == ["COHERE_API_KEY", "MEMORY_LLM_PROVIDER"]


def test_a_corrupt_file_never_blocks_startup(tmp_path):
    (tmp_path / "settings.json").write_text("{not json")
    assert local_settings.apply({}, tmp_path) == []
    (tmp_path / "settings.json").write_text(json.dumps({"version": 99}))
    assert local_settings.apply({}, tmp_path) == []


def test_raw_log_retention_removes_only_old_files(tmp_path):
    old, fresh = tmp_path / "old.jsonl", tmp_path / "fresh.jsonl"
    old.write_text("{}\n")
    fresh.write_text("{}\n")
    now = time.time()
    os.utime(old, (now - 40 * 86_400, now - 40 * 86_400))
    assert erasure.prune_raw_logs(tmp_path, None) == 0
    assert erasure.prune_raw_logs(tmp_path, "abc") == 0
    assert erasure.prune_raw_logs(tmp_path, "30", now=now) == 1
    assert not old.exists() and fresh.exists()


def test_a_new_server_session_runs_with_the_saved_settings(tmp_path):
    local_settings.LocalSettings(tmp_path, {}).update({"MEMORY_RECALL_MAX_RESULT_CHARS": "321",
                                                       "OPENAI_API_KEY": KEY})
    env = {**os.environ, "TAM_MEMORY_DIR": str(tmp_path), "MCP_TRANSPORT": "stdio"}
    env.pop("MEMORY_RECALL_MAX_RESULT_CHARS", None)
    env.pop("OPENAI_API_KEY", None)
    probe = ("import os, server; print(os.environ['MEMORY_RECALL_MAX_RESULT_CHARS'], "
             "os.environ['OPENAI_API_KEY'] == " + repr(KEY) + ")")
    out = subprocess.run([sys.executable, "-c", probe], cwd=Path(__file__).parent.parent / "src", env=env,
                         capture_output=True, text=True, timeout=180, check=True)
    assert out.stdout.split()[-2:] == ["321", "True"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_memory_dir_is_made_owner_only(tmp_path):
    from paths import restrict_permissions
    root = tmp_path / "mem"
    root.mkdir(mode=0o755)
    (root / "memory.db").write_bytes(b"x")
    os.chmod(root / "memory.db", 0o644)
    (root / "notes.txt").write_text("left alone")
    os.chmod(root / "notes.txt", 0o644)
    assert restrict_permissions(root) == []
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "memory.db").stat().st_mode) == 0o600
    assert stat.S_IMODE((root / "notes.txt").stat().st_mode) == 0o644


def test_first_secret_creates_the_memory_dir_and_key(tmp_path):
    root = tmp_path / "fresh" / ".tam"
    local_settings.LocalSettings(root, {}).update({"OPENAI_API_KEY": KEY})
    assert local_settings.LocalSettings(root, {}).overrides() == {"OPENAI_API_KEY": KEY}
    assert (root / "master.key").is_file()


def test_master_key_can_come_from_the_environment(tmp_path):
    from cryptography.fernet import Fernet
    env = {"TAM_MASTER_KEY": Fernet.generate_key().decode()}
    local_settings.LocalSettings(tmp_path, env).update({"OPENAI_API_KEY": KEY})
    assert not (tmp_path / "master.key").exists()
    assert local_settings.LocalSettings(tmp_path, env).overrides() == {"OPENAI_API_KEY": KEY}
    assert local_settings.LocalSettings(tmp_path, {}).overrides() == {}


def test_mode_bits_are_ignored_off_posix(tmp_path, monkeypatch):
    """Windows reports 0o666 for every file; the master key must still load there (CI regression)."""
    import paths
    key = tmp_path / "master.key"
    local_settings.LocalSettings(tmp_path, {}).update({"OPENAI_API_KEY": KEY})
    os.chmod(key, 0o666)
    monkeypatch.setattr(paths, "POSIX", False)
    assert paths.exposed_to_others(key) is False
    assert local_settings.LocalSettings(tmp_path, {}).overrides() == {"OPENAI_API_KEY": KEY}

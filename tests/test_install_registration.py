"""install.sh registers every client through setup_wizard.register and writes the files each client really reads.

Runs the real installer in INSTALL_TEST_MODE=1 with a sandbox HOME; python3 on PATH is this interpreter so the
registration module has its dependencies.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from setup_wizard import clients
from setup_wizard.register import main as register_main
from setup_wizard.upgrade import adopt_existing

ROOT = Path(__file__).resolve().parents[1]
SERVER = str(ROOT / "src" / "server.py")


def _host(home: Path) -> clients.Host:
    return clients.Host(home, platform.system(), {"XDG_CONFIG_HOME": str(home / ".config")}, lambda _b: None)


def _install(home: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    bin_dir = home / ".test-bin"
    bin_dir.mkdir(exist_ok=True)
    systemctl = bin_dir / "systemctl"
    systemctl.write_text("#!/usr/bin/env bash\nexit 1\n")
    systemctl.chmod(0o755)
    environ = {**os.environ, "HOME": str(home), "INSTALL_TEST_MODE": "1", "TAM_MEMORY_DIR": str(home / ".tam"),
               "XDG_CONFIG_HOME": str(home / ".config"),
               "PATH": f"{bin_dir}:{Path(sys.executable).parent}:{os.environ.get('PATH', '')}", **(env or {})}
    for name in ("CLAUDE_MEMORY_DIR", "CODEX_HOME", "APPDATA", "TAM_SETUP_FILE"):
        environ.pop(name, None)
    return subprocess.run(["bash", str(ROOT / "install.sh"), *args], env=environ, capture_output=True, text=True,
                          timeout=900, check=False)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


def _json(path: Path) -> dict:
    return json.loads(path.read_text())


def _assert_entry(entry: dict, home: Path) -> None:
    assert entry["args"] == [SERVER]
    assert entry["env"]["TAM_MEMORY_DIR"] == str((home / ".tam").resolve())


@pytest.mark.parametrize("ide", [c.id for c in clients.CLIENTS])
def test_install_writes_the_file_each_client_reads(home: Path, ide: str):
    result = _install(home, "--ide", ide)
    assert result.returncode == 0, result.stdout + result.stderr
    config = clients.BY_ID[ide].config(_host(home))
    assert config.is_file(), config
    if ide == "codex":
        table = tomllib.loads(config.read_text())["mcp_servers"]["memory"]
        _assert_entry(table, home)
        assert table["required"] is True and table["env"]["MEMORY_TRIPLE_MAX_PREDICT"] == "512"
        assert table["env"]["MEMORY_MODE"] == "fast"
        assert (home / ".codex" / "skills" / "memory-protocol" / "SKILL.md").is_file()
        assert (home / ".agents" / "skills" / "memory" / "SKILL.md").is_file()
        assert (home / ".agents" / "skills" / "onboard" / "SKILL.md").is_file()
    elif ide == "opencode":
        entry = _json(config)["mcp"]["memory"]
        assert entry["type"] == "local" and entry["command"][1:] == [SERVER]
        assert entry["environment"]["TAM_MEMORY_DIR"] == str((home / ".tam").resolve())
    elif ide == "continue":
        block = _json(config)
        assert block["schema"] == "v1"
        _assert_entry(block["mcpServers"][0], home)
        assert (home / ".continue" / "rules" / "memory-protocol.md").is_file()
        assert not (home / ".continue" / "config.json").exists()
    elif ide == "aider":
        text = config.read_text()
        assert "read:" in text and str(ROOT / "skills" / "memory-protocol" / "SKILL.md") in text
    else:
        _assert_entry(_json(config)["mcpServers"]["memory"], home)
    if ide == "claude-code":
        assert "mcpServers" not in _json(home / ".claude" / "settings.json")
        assert (home / ".claude" / "skills" / "memory-protocol" / "SKILL.md").is_file()
    else:
        assert not (home / ".claude" / "hooks").exists()
    if ide == "cline":
        assert not (_host(home).vscode_user() / "settings.json").exists()
    record = _json(home / ".tam" / "setup.json")
    assert record["mode"] == "personal" and record["source"] == "installer" and ide in record["personal"]["clients"]


def test_second_install_adds_the_client_to_the_record(home: Path):
    assert _install(home, "--ide", "claude-code").returncode == 0
    assert _install(home, "--ide", "cursor").returncode == 0
    record = _json(home / ".tam" / "setup.json")
    assert record["personal"]["clients"] == ["claude-code", "cursor"]
    assert "memory" in _json(home / ".claude.json")["mcpServers"]


def test_installer_keeps_keys_set_by_the_wizard_and_replaces_npm_entry(home: Path):
    cursor = home / ".cursor" / "mcp.json"
    cursor.parent.mkdir()
    cursor.write_text(json.dumps({"mcpServers": {
        "memory": {"command": "old", "args": [], "env": {"OPENAI_API_KEY": "sk-kept", "MEMORY_LLM_PROVIDER": "openai"}},
        "total-agent-memory": {"command": "/h/.tam/.venv/bin/total-agent-memory", "args": [], "env": {}},
        "github": {"command": "gh-mcp"}}}))
    result = _install(home, "--ide", "cursor")
    assert result.returncode == 0, result.stderr
    servers = _json(cursor)["mcpServers"]
    assert set(servers) == {"memory", "github"}
    assert servers["memory"]["env"]["OPENAI_API_KEY"] == "sk-kept" and servers["memory"]["args"] == [SERVER]
    assert "older 'total-agent-memory' entry" in result.stdout
    assert oct(cursor.stat().st_mode & 0o777) == "0o600"


def test_codex_npm_table_is_replaced_by_the_fenced_block(home: Path):
    config = home / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text('[other]\nx = 1\n\n[mcp_servers.total-agent-memory]\ncommand = "/h/.tam/.venv/bin/total-agent-memory"'
                      '\nargs = []\nenv = { MEMORY_MODE = "fast" }\n')
    assert _install(home, "--ide", "codex").returncode == 0
    data = tomllib.loads(config.read_text())
    assert set(data["mcp_servers"]) == {"memory"} and data["other"] == {"x": 1}


def test_broken_config_fails_the_install_and_is_left_alone(home: Path):
    cursor = home / ".cursor" / "mcp.json"
    cursor.parent.mkdir()
    cursor.write_text('{"mcpServers": {broken')
    result = _install(home, "--ide", "cursor")
    assert result.returncode != 0 and "not valid JSON" in result.stderr
    assert cursor.read_text() == '{"mcpServers": {broken'
    assert not (home / ".tam" / "setup.json").exists()


def test_aider_refuses_to_merge_into_an_existing_read_list(home: Path):
    conf = home / ".aider.conf.yml"
    conf.write_text("read:\n  - CONVENTIONS.md\n")
    result = _install(home, "--ide", "aider")
    assert result.returncode != 0 and "read:" in result.stderr
    assert conf.read_text() == "read:\n  - CONVENTIONS.md\n"
    conf.write_text("model: gpt-4.1\n")
    assert _install(home, "--ide", "aider").returncode == 0
    assert _install(home, "--ide", "aider").returncode == 0
    text = conf.read_text()
    assert text.startswith("model: gpt-4.1\n") and text.count("read:") == 1


def test_overwrite_hooks_env_is_passed_through(home: Path):
    assert _install(home, "--ide", "claude-code").returncode == 0
    hook = home / ".claude" / "hooks" / "session-start.sh"
    hook.write_text("#!/bin/sh\nexit 99\n")
    assert _install(home, "--ide", "claude-code").returncode == 0
    assert "exit 99" in hook.read_text()
    assert _install(home, "--ide", "claude-code", env={"INSTALL_OVERWRITE_HOOKS": "1"}).returncode == 0
    assert "exit 99" not in hook.read_text()


def test_unregister_removes_entry_and_hooks_but_keeps_user_hooks(home: Path):
    assert _install(home, "--ide", "claude-code").returncode == 0
    settings = home / ".claude" / "settings.json"
    data = _json(settings)
    data["hooks"]["SessionStart"].append({"matcher": "", "hooks": [{"type": "command", "command": "/usr/local/bin/mine"}]})
    settings.write_text(json.dumps(data))
    assert register_main(["--unregister", "--client", "claude-code"], environ={}, host=_host(home)) == 0
    assert "memory" not in _json(home / ".claude.json")["mcpServers"]
    commands = [h["command"] for blocks in _json(settings)["hooks"].values() for b in blocks for h in b["hooks"]]
    assert commands == ["/usr/local/bin/mine"]


def test_register_rejects_bad_arguments(home: Path):
    assert register_main(["--client", "emacs"], environ={}, host=_host(home)) == 64
    assert register_main(["--client", "cursor", "--env", "NOEQUALS"], environ={}, host=_host(home)) == 1
    assert not (home / ".cursor").exists()


def test_upgrade_hints_at_registrations_older_installers_misplaced(home: Path, caplog):
    host = _host(home)
    vscode = host.vscode_user() / "settings.json"
    vscode.parent.mkdir(parents=True)
    vscode.write_text('{"editor.fontSize": 13, "cline": {"mcpServers": {"memory": {"command": "py"}}}}')
    opencode = home / ".opencode" / "config.json"
    opencode.parent.mkdir()
    opencode.write_text('{"mcp": {"memory": {"command": "py"}}}')
    before = (vscode.read_text(), opencode.read_text())
    env = {"TAM_MEMORY_DIR": str(home / ".tam")}
    with caplog.at_level(logging.WARNING, logger="setup_wizard.upgrade"):
        record = adopt_existing(env, host)
    assert record is not None and record.mode == "personal"
    lines = [r.getMessage() for r in caplog.records if "older installer" in r.getMessage()]
    assert len(lines) == 1 and "\n" not in lines[0]
    assert "Cline" in lines[0] and "OpenCode" in lines[0] and "tam setup --reconfigure" in lines[0]
    assert (vscode.read_text(), opencode.read_text()) == before


NPM_DIR = Path(os.environ.get("TAM_NPM_DIR", Path.home() / "PROJECT" / "total-agent-memory" / "npm"))


@pytest.mark.skipif(not (NPM_DIR / "bin" / "cli.js").is_file() or shutil.which("node") is None,
                    reason="npm wrapper checkout or node not available")
def test_npm_connect_delegates_to_the_python_registration(home: Path):
    memory = home / ".tam"
    bin_dir = memory / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    venv = Path(sys.executable).parent
    for name in ("tam", "python", "total-agent-memory"):
        (bin_dir / name).symlink_to(venv / name)
    env = {**os.environ, "HOME": str(home), "TAM_MEMORY_DIR": str(memory), "NO_COLOR": "1",
           "XDG_CONFIG_HOME": str(home / ".config")}
    result = subprocess.run(["node", str(NPM_DIR / "bin" / "cli.js"), "connect", "opencode", "--memory-dir", str(memory)],
                            env=env, capture_output=True, text=True, timeout=300, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    entry = _json(home / ".config" / "opencode" / "opencode.json")["mcp"]["memory"]
    assert entry["command"] == [str(bin_dir / "total-agent-memory")]
    assert entry["environment"]["MEMORY_MODE"] == "fast"
    assert not (home / ".config" / "opencode" / "config.json").exists()

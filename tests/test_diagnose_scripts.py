"""Tests for cross-platform installation docs + diagnostic scripts.

Covers:
  - scripts/diagnose.sh  — exists, executable, test-mode runs cleanly
  - scripts/diagnose.ps1 — exists, parses as PowerShell
  - docs/installation.md — mentions every supported OS
  - examples/settings/claude-code-wsl.json — valid JSON with expected shape
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
DIAG_SH = ROOT / "scripts" / "diagnose.sh"
DIAG_PS1 = ROOT / "scripts" / "diagnose.ps1"
INSTALL_MD = ROOT / "docs" / "installation.md"
WSL_JSON = ROOT / "examples" / "settings" / "claude-code-wsl.json"


# ---------------------------------------------------------------------------
# diagnose.sh
# ---------------------------------------------------------------------------


def test_diagnose_sh_exists_and_executable():
    assert DIAG_SH.exists(), f"{DIAG_SH} must exist"
    mode = DIAG_SH.stat().st_mode
    # Owner-executable bit must be set; we may need to flip it if git stripped it.
    if not (mode & stat.S_IXUSR):
        DIAG_SH.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    assert DIAG_SH.stat().st_mode & stat.S_IXUSR, "diagnose.sh must be executable"


def test_diagnose_sh_has_shebang():
    first_line = DIAG_SH.read_text(encoding="utf-8").splitlines()[0]
    assert first_line.startswith("#!"), "diagnose.sh must start with a shebang"
    assert "bash" in first_line, "shebang should reference bash"


def test_diagnose_sh_runs_in_test_mode():
    """With DIAG_TEST_MODE=1, the script must exit 0 and emit a mock report."""
    env = {**os.environ, "DIAG_TEST_MODE": "1"}
    proc = subprocess.run(
        ["bash", str(DIAG_SH)],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, f"test mode should exit 0, got {proc.returncode}: {proc.stderr}"
    assert "TEST MODE" in proc.stdout
    assert "Result:" in proc.stdout
    assert "OK" in proc.stdout


def test_diagnose_sh_detects_wsl_logic_present():
    """Sanity: the script knows about WSL2 (so WSL users get a correct OS line)."""
    src = DIAG_SH.read_text(encoding="utf-8")
    assert "WSL" in src
    assert "/proc/version" in src
    assert "WSL_DISTRO_NAME" in src


# ---------------------------------------------------------------------------
# diagnose.ps1
# ---------------------------------------------------------------------------


def test_diagnose_ps1_exists():
    assert DIAG_PS1.exists(), f"{DIAG_PS1} must exist"


def test_diagnose_ps1_has_requires_and_mock():
    src = DIAG_PS1.read_text(encoding="utf-8")
    assert "#Requires -Version 5.1" in src
    assert "DIAG_TEST_MODE" in src, "PS1 must honor DIAG_TEST_MODE for test parity"
    assert "Register-ScheduledTask" not in src, "diagnose script must not mutate system"
    # Should probe the expected surfaces:
    assert "ScheduledTask" in src
    assert "memory.db" in src


# ---------------------------------------------------------------------------
# docs/installation.md
# ---------------------------------------------------------------------------


def test_installation_md_exists():
    assert INSTALL_MD.exists(), f"{INSTALL_MD} must exist"


def test_installation_md_covers_all_oses():
    text = INSTALL_MD.read_text(encoding="utf-8")
    for token in ("macOS", "Linux", "WSL2", "Windows"):
        assert token in text, f"installation.md must mention {token}"


def test_installation_md_mentions_service_managers():
    text = INSTALL_MD.read_text(encoding="utf-8")
    for token in ("LaunchAgent", "systemd", "Task Scheduler"):
        assert token in text, f"installation.md must mention {token}"


def test_installation_md_mentions_wsl_conf_systemd():
    text = INSTALL_MD.read_text(encoding="utf-8")
    assert "/etc/wsl.conf" in text, "WSL2 section must document /etc/wsl.conf"
    assert "systemd=true" in text
    assert "wsl -e" in text or "wsl -- " in text or '"wsl"' in text, \
        "WSL2 section must document the wsl prefix for MCP command"


# ---------------------------------------------------------------------------
# examples/settings/claude-code-wsl.json
# ---------------------------------------------------------------------------


def test_wsl2_example_json_exists():
    assert WSL_JSON.exists(), f"{WSL_JSON} must exist"


def test_wsl2_example_json_valid():
    data = json.loads(WSL_JSON.read_text(encoding="utf-8"))
    assert "mcpServers" in data
    assert "memory" in data["mcpServers"]
    memory = data["mcpServers"]["memory"]
    assert memory["command"] == "wsl", "Windows-host-facing example must call `wsl`"
    assert isinstance(memory["args"], list) and len(memory["args"]) >= 2
    # Must reference the Linux venv python inside WSL:
    joined = " ".join(memory["args"])
    assert "/.venv/bin/python" in joined
    assert "src/server.py" in joined
    # Placeholder expected so user knows to substitute:
    assert "USERNAME" in joined


@pytest.mark.parametrize("required_key", ["CLAUDE_MEMORY_DIR"])
def test_wsl2_example_env_has_required_keys(required_key):
    data = json.loads(WSL_JSON.read_text(encoding="utf-8"))
    env = data["mcpServers"]["memory"].get("env", {})
    assert required_key in env, f"env block must define {required_key}"


# ---------------------------------------------------------------------------
# README cross-link
# ---------------------------------------------------------------------------


def test_readme_links_to_installation_md():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/installation.md" in readme, \
        "README.md Install section must link to docs/installation.md"
    installation = (ROOT / "docs" / "installation.md").read_text(encoding="utf-8")
    assert "## Platform matrix" in installation, \
        "docs/installation.md must include a Platform matrix section"

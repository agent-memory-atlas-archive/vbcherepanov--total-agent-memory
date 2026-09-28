"""Detect MCP clients on this machine and register the local memory server with them.

This module is the only implementation of client registration: the wizard, ``install.sh``, ``install.ps1`` and the
npm wrapper all call it. Entries use the server key ``memory``. Existing config files are parsed strictly: a file
that does not parse is reported and left untouched, never replaced.
"""
import json
import os
import platform
import re
import shutil
import sys
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from setup_wizard.contracts import WizardError
from setup_wizard.files import PRIVATE_MODE, atomic_write
from setup_wizard.locations import install_root

SERVER_NAME = "memory"
NPM_SERVER_NAME = "total-agent-memory"
CODEX_BEGIN = "# --- total-agent-memory MCP Server ---"
CODEX_END = "# --- End total-agent-memory ---"
CODEX_BLOCK = re.compile(r"# --- (?:Claude Total Memory|total-agent-memory) MCP Server ---.*?"
                         r"# --- End (?:Claude Total Memory|total-agent-memory) ---\n?", re.DOTALL)
CODEX_TABLE = re.compile(r"^\[mcp_servers\.memory(?:\.[^\]\n]+)?\][^\n]*\n(?:(?!\[)[^\n]*\n?)*", re.MULTILINE)
CODEX_NPM_TABLE = re.compile(r"\n*\[mcp_servers\.total-agent-memory\]\ncommand = \"[^\"\n]*total-agent-memory[^\"\n]*\"\n"
                             r"args = \[\]\nenv = \{ MEMORY_MODE = \"fast\" \}\n")
CODEX_STARTUP_TIMEOUT_SEC = 15.0
CODEX_TOOL_TIMEOUT_SEC = 120.0
AIDER_BEGIN = "# --- total-agent-memory (memory bridge) ---"
AIDER_END = "# --- End total-agent-memory ---"
AIDER_BLOCK = re.compile(r"# --- total-agent-memory (?:v10\.5 )?\(memory bridge\) ---.*?"
                         r"# --- (?:end|End) total-agent-memory ---\n?", re.DOTALL)
AIDER_READ = re.compile(r"^read\s*:", re.MULTILINE)
CONTINUE_BLOCK_VERSION = "1.0.0"
Format = Literal["mcp-json", "codex-toml", "opencode-json", "continue-block", "aider-read"]


class ConfigError(WizardError):
    pass


@dataclass(frozen=True)
class Host:
    """The machine as the wizard sees it; tests pass a fake home and PATH lookup."""

    home: Path
    system: str
    environ: Mapping[str, str]
    which: Callable[[str], str | None]

    @classmethod
    def current(cls) -> "Host":
        return cls(Path.home(), platform.system(), os.environ, shutil.which)

    def config_home(self) -> Path:
        return Path(self.environ["XDG_CONFIG_HOME"]) if self.environ.get("XDG_CONFIG_HOME") else self.home / ".config"

    def app_support(self) -> Path:
        if self.system == "Darwin":
            return self.home / "Library" / "Application Support"
        if self.system == "Windows":
            return Path(self.environ.get("APPDATA") or self.home / "AppData" / "Roaming")
        return self.config_home()

    def codex_home(self) -> Path:
        return Path(self.environ["CODEX_HOME"]) if self.environ.get("CODEX_HOME") else self.home / ".codex"

    def vscode_user(self) -> Path:
        return self.app_support() / "Code" / "User"


@dataclass(frozen=True)
class ServerEntry:
    command: str
    args: tuple[str, ...]
    env: Mapping[str, str]


@dataclass(frozen=True)
class Client:
    id: str
    label: str
    format: Format
    config: Callable[[Host], Path]
    markers: Callable[[Host], tuple[Path, ...]]
    binaries: tuple[str, ...] = ()
    restart: str = ""
    needs_skill: bool = False

    def detected(self, host: Host) -> bool:
        return any(path.exists() for path in self.markers(host)) or any(host.which(b) for b in self.binaries)


def _cline_dir(host: Host) -> Path:
    return host.vscode_user() / "globalStorage" / "saoudrizwan.claude-dev"


CLIENTS: tuple[Client, ...] = (
    Client("claude-code", "Claude Code", "mcp-json", lambda h: h.home / ".claude.json",
           lambda h: (h.home / ".claude", h.home / ".claude.json"), ("claude",),
           "Restart Claude Code, then run /mcp: 'memory' should be connected."),
    Client("claude-desktop", "Claude Desktop", "mcp-json",
           lambda h: h.app_support() / "Claude" / "claude_desktop_config.json", lambda h: (h.app_support() / "Claude",),
           restart="Quit and reopen Claude Desktop."),
    Client("codex", "Codex CLI", "codex-toml", lambda h: h.codex_home() / "config.toml", lambda h: (h.codex_home(),),
           ("codex",), "Start codex and type /mcp to check the server."),
    Client("cursor", "Cursor", "mcp-json", lambda h: h.home / ".cursor" / "mcp.json", lambda h: (h.home / ".cursor",),
           ("cursor",), "Restart Cursor; the server appears under Settings → MCP."),
    Client("windsurf", "Windsurf", "mcp-json", lambda h: h.home / ".codeium" / "windsurf" / "mcp_config.json",
           lambda h: (h.home / ".codeium" / "windsurf",), ("windsurf",), "Restart Windsurf."),
    Client("gemini-cli", "Gemini CLI", "mcp-json", lambda h: h.home / ".gemini" / "settings.json",
           lambda h: (h.home / ".gemini",), ("gemini",), "Start gemini and run /mcp."),
    Client("cline", "Cline (VS Code)", "mcp-json", lambda h: _cline_dir(h) / "settings" / "cline_mcp_settings.json",
           lambda h: (_cline_dir(h),), restart="Reload the VS Code window."),
    Client("continue", "Continue", "continue-block", lambda h: h.home / ".continue" / "mcpServers" / "memory.yaml",
           lambda h: (h.home / ".continue",), restart="Reload your IDE; Continue reads ~/.continue/mcpServers/."),
    Client("opencode", "OpenCode", "opencode-json", lambda h: h.config_home() / "opencode" / "opencode.json",
           lambda h: (h.config_home() / "opencode",), ("opencode",), "Restart opencode."),
    Client("aider", "Aider (no MCP; reads the memory skill)", "aider-read", lambda h: h.home / ".aider.conf.yml",
           lambda h: (h.home / ".aider.conf.yml",), ("aider",),
           "Aider has no MCP: it now reads the memory-protocol skill; use `lookup-memory \"<query>\"` from its shell.",
           needs_skill=True),
)
BY_ID = {client.id: client for client in CLIENTS}


def skill_file(root: Path | None = None) -> Path:
    return (root or install_root()) / "skills" / "memory-protocol" / "SKILL.md"


def available(client: Client, root: Path | None = None) -> bool:
    return not client.needs_skill or skill_file(root).is_file()


def server_entry(env: Mapping[str, str]) -> ServerEntry:
    """Launch the server the same way from every client: the installed console script, else src/server.py."""
    scripts = Path(sys.executable).parent
    script = scripts / ("total-agent-memory.exe" if os.name == "nt" else "total-agent-memory")
    if script.is_file():
        return ServerEntry(str(script), (), dict(env))
    return ServerEntry(sys.executable, (str(Path(__file__).resolve().parents[1] / "server.py"),), dict(env))


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON (line {exc.lineno}); fix or move it, then run setup again") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a JSON object; fix or move it, then run setup again")
    return data


def _read_toml(path: Path) -> str:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML ({exc}); fix or move it, then run setup again") from exc
    return text


def _servers(data: dict, key: str, path: Path) -> dict:
    servers = data.setdefault(key, {})
    if not isinstance(servers, dict):
        raise ConfigError(f"{path}: '{key}' must be an object")
    return servers


def _quoted(value: str) -> str:
    """A JSON string is also a valid TOML basic string and YAML double-quoted scalar."""
    return json.dumps(value)


def _codex_block(entry: ServerEntry) -> str:
    lines = [CODEX_BEGIN, f"[mcp_servers.{SERVER_NAME}]", f"command = {_quoted(entry.command)}",
             "args = [" + ", ".join(_quoted(a) for a in entry.args) + "]", "required = true",
             f"startup_timeout_sec = {CODEX_STARTUP_TIMEOUT_SEC}", f"tool_timeout_sec = {CODEX_TOOL_TIMEOUT_SEC}", "",
             f"[mcp_servers.{SERVER_NAME}.env]"]
    lines += [f"{key} = {_quoted(value)}" for key, value in sorted(entry.env.items())]
    return "\n".join([*lines, CODEX_END]) + "\n"


def _aider_block(skill: Path) -> str:
    return "\n".join([AIDER_BEGIN, "read:", f"  - {_quoted(str(skill))}", AIDER_END]) + "\n"


def current_entry(client: Client, host: Host) -> dict | None:
    """The memory entry this client has today (command/args/env; for Aider the skill it reads), or None."""
    path = client.config(host)
    if not path.exists():
        return None
    if client.format == "codex-toml":
        table = tomllib.loads(_read_toml(path)).get("mcp_servers", {}).get(SERVER_NAME)
        return table if isinstance(table, dict) else None
    if client.format == "aider-read":
        match = AIDER_BLOCK.search(path.read_text(encoding="utf-8"))
        return {"read": match.group(0)} if match else None
    data = read_json(path)
    if client.format == "continue-block":
        servers = data.get("mcpServers")
        entry = next((s for s in servers if isinstance(s, dict) and s.get("name") == SERVER_NAME), None) \
            if isinstance(servers, list) else None
        return {k: entry.get(k) for k in ("command", "args", "env")} if entry else None
    if client.format == "opencode-json":
        entry = data.get("mcp", {}).get(SERVER_NAME) if isinstance(data.get("mcp"), dict) else None
        if not isinstance(entry, dict):
            return None
        command = entry.get("command") or []
        return {"command": command[0] if command else "", "args": command[1:], "env": entry.get("environment", {})}
    entry = data.get("mcpServers", {}).get(SERVER_NAME) if isinstance(data.get("mcpServers"), dict) else None
    return entry if isinstance(entry, dict) else None


@dataclass
class Change:
    client: Client
    path: Path
    text: str
    mode: int | None = None
    notes: list[str] = field(default_factory=list)


def _drop_npm_entry(servers: dict, path: Path, notes: list[str]) -> None:
    """The npm wrapper used to register under another name; one server must not appear twice."""
    old = servers.get(NPM_SERVER_NAME)
    if isinstance(old, dict) and NPM_SERVER_NAME in str(old.get("command", "")):
        del servers[NPM_SERVER_NAME]
        notes.append(f"replaced the older '{NPM_SERVER_NAME}' entry in {path}")


def plan(client: Client, entry: ServerEntry, host: Host, private: bool, root: Path | None = None) -> Change:
    """Compute the new config text without writing; raises ConfigError on files it cannot safely edit."""
    path = client.config(host)
    mode = PRIVATE_MODE if private else None
    notes: list[str] = []
    if client.format == "codex-toml":
        text = _read_toml(path)
        block = _codex_block(entry)
        if CODEX_NPM_TABLE.search(text):
            text = CODEX_NPM_TABLE.sub("\n", text).strip("\n") + "\n"
            notes.append(f"replaced the older '{NPM_SERVER_NAME}' table in {path}")
        if CODEX_BLOCK.search(text):
            text = CODEX_BLOCK.sub(lambda _match: block, text, count=1)
        else:
            if CODEX_TABLE.search(text):
                text = CODEX_TABLE.sub("", text)
                notes.append(f"replaced the unfenced [mcp_servers.{SERVER_NAME}] tables in {path}")
            text = (text.rstrip() + "\n\n" if text.strip() else "") + block
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path} would not be valid TOML after the update ({exc}); edit it by hand") from exc
        return Change(client, path, text, mode, notes)
    if client.format == "aider-read":
        skill = skill_file(root)
        if not skill.is_file():
            raise ConfigError("Aider needs the memory-protocol skill, which ships with the git checkout")
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        block = _aider_block(skill)
        if AIDER_BLOCK.search(text):
            text = AIDER_BLOCK.sub(lambda _match: block, text, count=1)
        elif AIDER_READ.search(text):
            raise ConfigError(f"{path} already has a 'read:' list; add {skill} to it yourself")
        else:
            text = (text.rstrip() + "\n\n" if text.strip() else "") + block
        return Change(client, path, text, None, notes)
    if client.format == "continue-block":
        read_json(path)
        block = {"name": "total-agent-memory", "version": CONTINUE_BLOCK_VERSION, "schema": "v1",
                 "mcpServers": [{"name": SERVER_NAME, "command": entry.command, "args": list(entry.args),
                                 "env": dict(entry.env)}]}
        return Change(client, path, json.dumps(block, indent=2, ensure_ascii=False) + "\n", mode, notes)
    data = read_json(path)
    if client.format == "opencode-json":
        data.setdefault("$schema", "https://opencode.ai/config.json")
        _servers(data, "mcp", path)[SERVER_NAME] = {"type": "local", "command": [entry.command, *entry.args],
                                                    "environment": dict(entry.env), "enabled": True}
    else:
        servers = _servers(data, "mcpServers", path)
        _drop_npm_entry(servers, path, notes)
        servers[SERVER_NAME] = {"command": entry.command, "args": list(entry.args), "env": dict(entry.env)}
    return Change(client, path, json.dumps(data, indent=2, ensure_ascii=False) + "\n", mode, notes)


def plan_removal(client: Client, host: Host) -> Change | None:
    """The config without the memory entry, or None when there is nothing to remove."""
    path = client.config(host)
    if current_entry(client, host) is None:
        return None
    if client.format == "codex-toml":
        text = _read_toml(path)
        return Change(client, path, CODEX_TABLE.sub("", CODEX_BLOCK.sub("", text)).strip("\n") + "\n")
    if client.format == "aider-read":
        return Change(client, path, AIDER_BLOCK.sub("", path.read_text(encoding="utf-8")).strip("\n") + "\n")
    if client.format == "continue-block":
        return Change(client, path, "")
    data = read_json(path)
    del data["mcp" if client.format == "opencode-json" else "mcpServers"][SERVER_NAME]
    return Change(client, path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def write(change: Change) -> None:
    if change.client.format == "continue-block" and not change.text:
        change.path.unlink()
        return
    atomic_write(change.path, change.text, change.mode)


@dataclass(frozen=True)
class LegacyLocation:
    client: str
    path: Callable[[Host], Path]
    keys: tuple[tuple[str, ...], ...]


LEGACY_LOCATIONS: tuple[LegacyLocation, ...] = (
    LegacyLocation("Claude Code", lambda h: h.home / ".claude" / "settings.json", (("mcpServers", SERVER_NAME),)),
    LegacyLocation("Cline", lambda h: h.vscode_user() / "settings.json",
                   (("cline", "mcpServers", SERVER_NAME), ("cline.mcpServers", SERVER_NAME))),
    LegacyLocation("Cline", lambda h: h.home / ".cline" / "mcp.json", (("mcpServers", NPM_SERVER_NAME),)),
    LegacyLocation("OpenCode", lambda h: h.home / ".opencode" / "config.json", (("mcp", SERVER_NAME),)),
    LegacyLocation("OpenCode", lambda h: h.config_home() / "opencode" / "config.json",
                   (("mcpServers", NPM_SERVER_NAME),)),
    LegacyLocation("Continue", lambda h: h.home / ".continue" / "config.json",
                   (("mcpServers", SERVER_NAME), ("mcpServers", NPM_SERVER_NAME))),
)


def _has_key(data: object, keys: tuple[str, ...]) -> bool:
    for key in keys:
        if isinstance(data, list):
            data = next((item for item in data if isinstance(item, dict) and item.get("name") == key), None)
        elif isinstance(data, dict):
            data = data.get(key)
        else:
            return False
        if data is None:
            return False
    return True


def legacy_registrations(host: Host) -> list[tuple[str, Path]]:
    """Entries older installers wrote to files their client never reads for MCP servers."""
    found = []
    for location in LEGACY_LOCATIONS:
        path = location.path(host)
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            if any(f'"{keys[-2]}"' in text and f'"{keys[-1]}"' in text for keys in location.keys):
                found.append((location.client, path))
            continue
        if any(_has_key(data, keys) for keys in location.keys):
            found.append((location.client, path))
    return found

# Installation Guide

total-agent-memory v8.0 runs on macOS, Linux, and Windows — including Windows
Subsystem for Linux 2 (WSL2). The installer auto-detects the platform and wires
up the correct background services (LaunchAgents, systemd --user, or Windows
Task Scheduler), MCP-server registration path, and dashboard autostart.

> **TL;DR** — if you just want the command for your platform, jump to the
> [Platform matrix](#platform-matrix). For WSL2 nuances (Claude Code running
> on the Windows host talking to MCP inside the Linux VM), jump to
> [WSL2](#wsl2-windows-11--ubuntudebian-inside-wsl).

---

## Table of contents

- [Platform matrix](#platform-matrix)
- [Prerequisites](#prerequisites)
- [macOS (10.15+, Apple Silicon or Intel)](#macos-1015-apple-silicon-or-intel)
- [Linux (Ubuntu 22.04+, Debian 12+, Fedora 38+)](#linux-ubuntu-2204-debian-12-fedora-38)
- [Windows 10/11 native (without WSL)](#windows-1011-native-without-wsl)
- [WSL2 (Windows 11 + Ubuntu/Debian inside WSL)](#wsl2-windows-11--ubuntudebian-inside-wsl)
- [IDE coverage matrix](#ide-coverage-matrix)
- [Background services comparison](#background-services-comparison)
- [Post-install verification](#post-install-verification)
- [Uninstall](#uninstall)
- [Troubleshooting](#troubleshooting)

---

## Platform matrix

| OS | Command | Background services | MCP registration target |
|---|---|---|---|
| macOS | `./install.sh --ide claude-code` | LaunchAgents (`launchctl`) | `~/.claude.json` + `~/.claude/settings.json` |
| Linux | `./install.sh --ide claude-code` | systemd `--user` | `~/.claude.json` + `~/.claude/settings.json` |
| WSL2 (systemd on) | `./install.sh --ide claude-code` | systemd `--user` | depends on where Claude Code runs — see [WSL2](#wsl2-windows-11--ubuntudebian-inside-wsl) |
| WSL2 (systemd off) | `./install.sh --ide claude-code` | shell-loop / cron fallback | same as above |
| Windows native | `.\install.ps1 -Ide claude-code` | Task Scheduler | `%USERPROFILE%\.claude\settings.json` |

All flavors share the same MCP tool surface, the same `~/.claude-memory/memory.db`
schema, and the same dashboard (`http://127.0.0.1:37737`). The only differences
are *where* background jobs live and *which* path Claude Code reads the MCP
config from.

---

## Prerequisites

- **Python 3.11+** (3.13 recommended)
- **Git** for cloning the repo
- **~2 GB disk** (code + venv + MiniLM model + DB)
- **Claude Code** (or Codex CLI / Cursor / Gemini CLI / OpenCode — any MCP client)
- Optional but strongly recommended: **[Ollama](https://ollama.com)** running
  locally (`ollama serve`) with `qwen2.5-coder:7b` + `nomic-embed-text` pulled

Clone the repo first (same on every platform):

```bash
git clone https://github.com/vbcherepanov/claude-total-memory.git ~/claude-memory-server
cd ~/claude-memory-server
```

On Windows native the equivalent is:

```powershell
git clone https://github.com/vbcherepanov/claude-total-memory.git $env:USERPROFILE\claude-memory-server
cd $env:USERPROFILE\claude-memory-server
```

---

## macOS (10.15+, Apple Silicon or Intel)

```bash
./install.sh --ide claude-code
```

What happens:

1. Creates `~/claude-memory-server/.venv/` and installs `requirements.txt`
   + `requirements-dev.txt`.
2. Pre-downloads the FastEmbed multilingual MiniLM model (~90 MB, one-time).
3. Registers the MCP server via `claude mcp add-json memory ...` — stored in
   `~/.claude.json`, which is the canonical store Claude Code reads.
4. Writes hook entries into `~/.claude/settings.json` and drops the helper
   scripts into `~/.claude/hooks/`.
5. Substitutes `__HOME__` in the three LaunchAgent templates under
   `launchagents/` and installs them to `~/Library/LaunchAgents/`, then
   `launchctl bootstrap`s each one:
   - `com.claude.memory.reflection.plist` — `WatchPaths`-triggered drainer
     of `triple_extraction_queue` / `deep_enrichment_queue` /
     `representations_queue`.
   - `com.claude.memory.orphan-backfill.plist` — runs 4× daily
     (`StartCalendarInterval` at 00:00, 06:00, 12:00, 18:00).
   - `com.claude.memory.check-updates.plist` — Monday 09:00 weekly check.
6. Installs the dashboard LaunchAgent
   (`com.claude-total-memory.dashboard.plist`) with `KeepAlive` and
   `RunAtLoad=true` so the dashboard survives reboots.
7. Applies migrations to a fresh `~/.claude-memory/memory.db`.

Verify:

```bash
launchctl list | grep claude.memory
curl -sI http://127.0.0.1:37737 | head -1   # HTTP/1.1 200 OK
```

Restart Claude Code → `/mcp` → `memory` should show **Connected**.

---

## Linux (Ubuntu 22.04+, Debian 12+, Fedora 38+)

```bash
./install.sh --ide claude-code
```

What happens (differences from macOS):

1. Same Python venv + MCP registration flow.
2. Instead of LaunchAgents, the installer renders the systemd unit templates
   from `systemd/` into `~/.config/systemd/user/`:
   - `claude-memory-reflection.path` — `PathModified=%h/.claude-memory/.reflect-pending`
     trigger.
   - `claude-memory-reflection.service` — `Type=oneshot`, runs the drainer.
   - `claude-total-memory-dashboard.service` — `Type=simple`,
     `Restart=on-failure`.
3. `systemctl --user daemon-reload` + `systemctl --user enable --now` on each
   unit.
4. Uses **XDG paths**: logs land in `~/.claude-memory/logs/`, config in
   `~/.config/systemd/user/`.

### No systemd? (containers, minimal images, WSL1)

If `systemctl --user show-environment` fails at install time, the installer
falls back to a shell-loop drop-in (`~/.claude-memory/dashboard-autostart.sh`)
registered in `~/.profile`. Reflection then runs on a loose poll instead of
inotify. This is automatic — no flag needed — but you lose real-time
`WatchPaths`-equivalent behavior. To enable systemd properly on Ubuntu, make
sure you booted under `systemd` (default on Ubuntu 22.04+ server/desktop).

Verify:

```bash
systemctl --user status claude-memory-reflection.path
systemctl --user status claude-total-memory-dashboard
curl -sI http://127.0.0.1:37737 | head -1
```

---

## Windows 10/11 native (without WSL)

PowerShell 5.1+ required (ships with Windows 10/11). Run from an **elevated**
shell if you want the dashboard Scheduled Task to register cleanly:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1 -Ide claude-code
```

What happens:

1. Creates `.venv\` via `python -m venv`, activates with
   `.\.venv\Scripts\python.exe`.
2. Installs deps (`pip install -r requirements.txt -r requirements-dev.txt`).
3. Writes MCP config into `%USERPROFILE%\.claude\settings.json` under
   `mcpServers.memory`. The `command` is the venv Python path with forward
   slashes normalized by the installer.
4. Copies hook `.ps1` shims into `%USERPROFILE%\.claude\hooks\`.
5. Registers a Windows Scheduled Task `ClaudeTotalMemoryDashboard`:
   - Trigger: `AtLogon`
   - Action: `.\.venv\Scripts\python.exe src\dashboard.py`
   - Settings: `AllowStartIfOnBatteries`, `RestartCount=3`
6. Uses `%USERPROFILE%\.claude-memory\` as the DB/state dir.

### Known Windows-native quirks

- **File-watch granularity.** The reflection trigger uses a Scheduled-Task
  `FileSystemWatcher` shim rather than `WatchPaths` / `systemd.path`. Debounce
  is the same (5 s) but NTFS file-change latency is a touch higher than
  macOS/Linux.
- **Long paths.** Enable `LongPathsEnabled` in the registry if your
  `%USERPROFILE%` is deep — some `site-packages` wheels ship paths >260 chars.
- **Antivirus.** Some AV vendors flag the FastEmbed model download. Allow
  `%USERPROFILE%\.cache\fastembed` if you see quarantined artifacts.

---

## WSL2 (Windows 11 + Ubuntu/Debian inside WSL)

This is the most nuanced configuration because there are **two possible
places Claude Code can run from**, and the MCP server must be reachable from
whichever one you use.

### Scenario A — Claude Code runs on Windows (most common)

Claude Code is installed via its Windows installer and shows up in the
Windows Start menu. The MCP server lives inside WSL2 at
`\\wsl$\Ubuntu\home\<user>\claude-memory-server`.

MCP registration must point to the **Linux** Python binary, but use `wsl` as
the command from the Windows host's perspective:

```json
{
  "mcpServers": {
    "memory": {
      "command": "wsl",
      "args": [
        "-e",
        "/home/USERNAME/claude-memory-server/.venv/bin/python",
        "/home/USERNAME/claude-memory-server/src/server.py"
      ],
      "env": {
        "CLAUDE_MEMORY_DIR": "/home/USERNAME/.claude-memory"
      }
    }
  }
}
```

A ready-to-edit copy lives at
[`examples/settings/claude-code-wsl.json`](../examples/settings/claude-code-wsl.json).
Replace `USERNAME` with your WSL username (check with `wsl -- whoami` from
PowerShell) and merge the `mcpServers.memory` block into
`%USERPROFILE%\.claude\settings.json`.

### Scenario B — Claude Code runs inside WSL (VS Code Remote-WSL)

If you launch Claude Code from inside the WSL shell (e.g. via the VS Code
Remote-WSL extension that injects its CLI into the Linux environment), the
configuration is identical to the [Linux](#linux-ubuntu-2204-debian-12-fedora-38)
install — `install.sh` writes the MCP command as the native Linux Python
path, no `wsl` prefix needed.

**Rule of thumb:** whichever side runs the Claude Code binary is the side
that must be able to resolve the MCP `command`. If Claude Code runs on
Windows, MCP must be callable from Windows (so prefix with `wsl -e`). If
Claude Code runs inside WSL, MCP is local.

### Enabling systemd inside WSL2

`install.sh` detects WSL2 via `grep -qi microsoft /proc/version` and/or the
`WSL_DISTRO_NAME` env var. It then tries `systemctl --user
show-environment`. If WSL was booted without systemd, that check fails and
the installer falls back to the shell-loop autostart path
(`~/.claude-memory/dashboard-autostart.sh` wired through `~/.profile`).

To get real systemd `--user` support inside WSL2, add or edit
`/etc/wsl.conf`:

```ini
[boot]
systemd=true
```

Then from PowerShell:

```powershell
wsl --shutdown
wsl
```

Re-run `./install.sh --ide claude-code` and the installer will pick up
systemd and install the `.path` + `.service` units like on native Linux.

### WSL2 gotchas

- **Clock drift.** WSL2 virtual-time can drift after a Windows suspend/resume;
  if you see reflection timestamps jumping, run `sudo hwclock -s` inside WSL.
- **Port forwarding.** The dashboard binds to `127.0.0.1:37737` inside WSL;
  Windows auto-forwards localhost ports for WSL2 since Windows 11 22H2, so
  `http://localhost:37737` in a Windows browser works out of the box.
- **Filesystem cross-access.** Do **not** put `~/.claude-memory/` on
  `/mnt/c/...` — SQLite performance across the 9P bridge is catastrophic.
  Keep the DB in the Linux native filesystem (default).
- **Ollama.** Install Ollama either fully inside WSL (`curl -fsSL
  https://ollama.com/install.sh | sh`) or use Windows Ollama and point
  `OLLAMA_HOST=http://host.docker.internal:11434` in the MCP env.

---

## IDE coverage matrix

| IDE | macOS | Linux | WSL2 | Windows native |
|---|:---:|:---:|:---:|:---:|
| Claude Code | ✅ `~/.claude.json` (MCP) + `~/.claude/settings.json` (hooks) | ✅ | ✅\* | ✅ `%USERPROFILE%\.claude.json` |
| Cursor | ✅ `~/.cursor/mcp.json` | ✅ | ✅\* | ✅ `%USERPROFILE%\.cursor\mcp.json` |
| Gemini CLI | ✅ `~/.gemini/settings.json` | ✅ | ✅\* | ✅ |
| OpenCode | ✅ `~/.config/opencode/opencode.json` | ✅ | ✅\* | ✅ |
| Claude Desktop | ✅ `~/Library/Application Support/Claude/claude_desktop_config.json` | ✅ `~/.config/Claude/…` | ✅\* | ✅ `%APPDATA%\Claude\…` |
| Cline | ✅ VS Code global storage `saoudrizwan.claude-dev/settings/cline_mcp_settings.json` | ✅ | ✅\* | ✅ |
| Continue | ✅ `~/.continue/mcpServers/memory.yaml` | ✅ | ✅\* | ✅ |
| Codex CLI | ✅ `~/.codex/config.toml` | ✅ | ✅\* | ✅ |

\*WSL2: if the IDE runs on the Windows host, wrap the MCP `command` with
`wsl -e` and use Linux paths in `args` (see Scenario A above). If the IDE
runs inside WSL, use native Linux paths exactly like the Linux row.

Switch IDEs by passing `--ide <name>` to `install.sh` / `install.ps1`:

```bash
./install.sh --ide cursor        # or: gemini-cli / opencode / codex
```

Running the installer twice with different `--ide` values is safe — each
target IDE has its own config file, and the shared `memory.db` and venv are
reused.

---

## Background services comparison

The same three jobs run everywhere, just under different orchestrators:

| Task | macOS (LaunchAgent) | Linux & WSL2 (systemd `--user`) | Windows native (Task Scheduler) |
|---|---|---|---|
| Reflection on save | `com.claude.memory.reflection` + `WatchPaths` on `.reflect-pending` | `claude-memory-reflection.path` + `claude-memory-reflection.service` | FileSystemWatcher-backed Scheduled Task |
| Orphan backfill, 4× daily | `com.claude.memory.orphan-backfill` + `StartCalendarInterval` @ 00/06/12/18 | `claude-memory-orphan-backfill.timer` + `OnCalendar=*-*-* 00,06,12,18:00:00` | Scheduled Task with four `DailyTrigger`s |
| Update check, Monday 09:00 | `com.claude.memory.check-updates` weekly plist | `claude-memory-check-updates.timer` with `OnCalendar=Mon 09:00` | Scheduled Task `WeeklyTrigger -DaysOfWeek Monday -At 09:00` |
| Dashboard (keepalive) | `com.claude-total-memory.dashboard` `KeepAlive=true` | `claude-total-memory-dashboard.service` `Restart=on-failure` | Scheduled Task `AtLogon` + `RestartCount=3` |

State locations:

| Artifact | macOS | Linux / WSL2 | Windows native |
|---|---|---|---|
| DB | `~/.claude-memory/memory.db` | `~/.claude-memory/memory.db` | `%USERPROFILE%\.claude-memory\memory.db` |
| Logs | `/tmp/claude-memory-*.log` + `~/.claude-memory/logs/` | `~/.claude-memory/logs/` + `journalctl --user` | `%USERPROFILE%\.claude-memory\logs\` |
| Service manifests | `~/Library/LaunchAgents/` | `~/.config/systemd/user/` | Task Scheduler (`\memory*`) |

---

## Post-install verification

The quickest way to confirm everything is wired up is the diagnostic script:

```bash
# macOS / Linux / WSL2
bash scripts/diagnose.sh

# Windows native
powershell -ExecutionPolicy Bypass -File scripts\diagnose.ps1
```

Expected output includes lines like:

```
  OK OS: Darwin 25.4.0
  OK Python 3.13 venv present
  OK MCP server module importable
  OK LaunchAgents loaded: 3/3
  OK Dashboard HTTP 200
  OK Ollama detected: qwen2.5-coder:7b
  OK DB migrations at head
```

Exit code `0` = healthy; `1` = one or more checks failed (details in the
report).

You can also run the smoke test in any MCP client:

```
memory_save(content="install works", type="fact")
memory_stats()
```

---

## Uninstall

- **macOS / Linux / WSL2:** `./install.sh --uninstall`
- **Windows native:** `.\install.ps1 -Uninstall`

Both preserve `~/.claude-memory/memory.db` (or
`%USERPROFILE%\.claude-memory\memory.db`) by default. Pass `--purge` / `-Purge`
to drop the database as well.

What gets removed:

- Registered MCP server entry in the IDE's settings file.
- Hook scripts and their registrations in the IDE settings.
- LaunchAgents / systemd units / Scheduled Tasks and their log paths in
  `/tmp/` (macOS only — Linux keeps them under `~/.claude-memory/logs/`).
- The `.venv/` inside the clone (the clone itself is left alone — delete it
  manually if you want).

---

## Troubleshooting

### "memory" MCP server shows Disconnected

1. Run the diagnostic script and look for failing checks.
2. On macOS: `launchctl list | grep claude.memory` — should list 3 loaded
   agents. If empty, re-run `./install.sh`.
3. On Linux / WSL2: `systemctl --user status claude-memory-reflection.path`
   — look for `Active: active (waiting)`.
4. On Windows: `Get-ScheduledTask -TaskPath "\" -TaskName "ClaudeTotal*"`.
5. Tail the dashboard log: `tail -f ~/.claude-memory/logs/dashboard.log`
   (or the Windows equivalent).

### Dashboard port 37737 is busy

The installer honors `DASHBOARD_PORT`:

```bash
DASHBOARD_PORT=37800 ./install.sh --ide claude-code
```

Re-run — the old service is replaced and the new port is written into the
unit file or Scheduled Task.

### Am I really on WSL2?

```bash
grep -qi microsoft /proc/version && echo "WSL detected" || echo "native Linux"
echo "$WSL_DISTRO_NAME"        # populated on WSL only
wsl.exe --list --verbose       # from PowerShell, shows VERSION column
```

If `VERSION` column shows `1`, you are on WSL1 — systemd `--user` is
unavailable and reflection falls back to the shell-loop path. Migrate with
`wsl --set-version <distro> 2` from PowerShell.

### systemd `--user` is available but units don't start

```bash
systemctl --user daemon-reload
systemctl --user --no-pager status claude-memory-reflection.service
journalctl --user -u claude-memory-reflection.service -n 100 --no-pager
```

Common causes: Python binary path moved (re-run the installer), or
`~/.claude-memory/.reflect-pending` was deleted and `PathExists` never
re-armed — `touch ~/.claude-memory/.reflect-pending` to nudge it.

### Ollama integration

`memory_recall` / reflection are fully functional without Ollama (FastEmbed
covers retrieval), but richer triples / deep enrichment need it:

```bash
curl http://127.0.0.1:11434/api/tags   # should return JSON with models list
ollama pull qwen2.5-coder:7b
ollama pull nomic-embed-text
```

On WSL2 with Windows-host Ollama, export
`OLLAMA_HOST=http://host.docker.internal:11434` in the MCP env block.

---

See also:

- [examples/README.md](../examples/README.md) — hook and rules wiring
- [systemd/README.md](../systemd/README.md) — Linux unit details
- [docs/MANUAL_QA.md](MANUAL_QA.md) — end-to-end smoke checklist

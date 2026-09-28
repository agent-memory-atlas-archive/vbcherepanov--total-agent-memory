"""Claude Code hooks and agent skills for every installer. They ship with the git checkout, not with the wheel.

Hook scripts are copied to ~/.claude/hooks and registered in ~/.claude/settings.json. Existing scripts are kept
unless ``overwrite`` is set, and hook entries are only ever added, so user hooks survive re-installs.
"""
import json
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path

from setup_wizard.clients import ConfigError, Host, read_json
from setup_wizard.files import atomic_write

EXECUTABLE_MODE = 0o755
SKILLS = ("memory-protocol", "onboard", "onboard-report", "report")
ONBOARDING = ("onboard", "onboard-report")
AGENT_SKILLS = (*ONBOARDING, "report")
POWERSHELL = 'powershell -ExecutionPolicy Bypass -NoProfile -File "{path}"'
HOOK_GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("SessionStart", "", ("session-start",)),
    ("SessionEnd", "", ("session-end",)),
    ("Stop", "", ("on-stop",)),
    ("UserPromptSubmit", "", ("user-prompt-submit",)),
    ("PreToolUse", "Write|Edit", ("pre-edit",)),
    ("PostToolUse", "Bash", ("memory-trigger", "on-bash-error")),
    ("PostToolUse", "Write|Edit", ("auto-capture",)),
    ("PostToolUse", "", ("post-tool-use",)),
)

def _suffix(host: Host) -> str:
    return ".ps1" if host.system == "Windows" else ".sh"


def hooks_available(root: Path, host: Host) -> bool:
    return (root / "hooks" / ("session-start" + _suffix(host))).is_file()


def skills_available(root: Path) -> bool:
    return (root / "skills" / "memory-protocol" / "SKILL.md").is_file()


def _command(host: Host, script: Path) -> str:
    # Hooks run through a shell, so a home path with spaces must stay one word.
    return POWERSHELL.format(path=script) if host.system == "Windows" else shlex.quote(str(script))


@dataclass(frozen=True)
class HookPlan:
    scripts: tuple[tuple[Path, Path], ...]
    settings: Path
    text: str
    added: tuple[str, ...]
    overwrite: bool


def _settings(path: Path) -> dict:
    data = read_json(path)
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ConfigError(f"{path}: 'hooks' must be an object")
    return data


def plan_hooks(root: Path, host: Host, overwrite: bool = False) -> HookPlan:
    target = host.home / ".claude" / "hooks"
    suffix = _suffix(host)
    sources = sorted((root / "hooks").glob("*" + suffix))
    if suffix == ".sh":
        sources += sorted((root / "hooks" / "lib").glob("*.sh"))
    scripts = tuple((source, target / source.relative_to(root / "hooks")) for source in sources)
    settings = host.home / ".claude" / "settings.json"
    data = _settings(settings)
    added = []
    for event, matcher, names in HOOK_GROUPS:
        entries = data["hooks"].setdefault(event, [])
        if not isinstance(entries, list):
            raise ConfigError(f"{settings}: hooks.{event} must be a list")
        present = {hook.get("command") for block in entries if isinstance(block, dict)
                   for hook in block.get("hooks", []) if isinstance(hook, dict)}
        missing = [_command(host, target / (name + suffix)) for name in names
                   if (root / "hooks" / (name + suffix)).is_file()]
        missing = [command for command in missing if command not in present]
        if missing:
            entries.append({"matcher": matcher, "hooks": [{"type": "command", "command": c} for c in missing]})
            added.append(event)
    return HookPlan(scripts, settings, json.dumps(data, indent=2, ensure_ascii=False) + "\n", tuple(added), overwrite)


def apply_hooks(hook_plan: HookPlan) -> tuple[int, int]:
    copied = skipped = 0
    for source, destination in hook_plan.scripts:
        if destination.exists() and not hook_plan.overwrite:
            skipped += 1
            continue
        atomic_write(destination, source.read_text(encoding="utf-8"), EXECUTABLE_MODE)
        copied += 1
    atomic_write(hook_plan.settings, hook_plan.text)
    return copied, skipped


def remove_hooks(host: Host) -> Path | None:
    """Drop the hook entries this module registers; the scripts stay in ~/.claude/hooks."""
    settings = host.home / ".claude" / "settings.json"
    if not settings.is_file():
        return None
    data = _settings(settings)
    target = host.home / ".claude" / "hooks"
    ours = {_command(host, target / (name + _suffix(host))) for _event, _matcher, names in HOOK_GROUPS for name in names}
    changed = False
    for event in list(data["hooks"]):
        blocks = data["hooks"][event]
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if isinstance(block, dict) and isinstance(block.get("hooks"), list):
                kept = [h for h in block["hooks"] if not (isinstance(h, dict) and h.get("command") in ours)]
                changed |= len(kept) != len(block["hooks"])
                block["hooks"] = kept
        data["hooks"][event] = [b for b in blocks if not (isinstance(b, dict) and b.get("hooks") == [])]
        if not data["hooks"][event]:
            del data["hooks"][event]
    if not changed:
        return None
    atomic_write(settings, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return settings


def skill_copies(root: Path, host: Host, clients: list[str]) -> list[tuple[Path, Path]]:
    """(source, target) pairs; a source directory is copied as a tree, a file as a file."""
    skills = root / "skills"
    copies: list[tuple[Path, Path]] = []

    def tree(base: Path, names: tuple[str, ...]) -> None:
        copies.extend((skills / name, base / name) for name in names if (skills / name / "SKILL.md").is_file())

    if not skills_available(root):
        return copies
    if "claude-code" in clients:
        tree(host.home / ".claude" / "skills", SKILLS)
    if "codex" in clients:
        tree(host.codex_home() / "skills", SKILLS)
        tree(host.home / ".agents" / "skills", AGENT_SKILLS)
        if (root / "codex-skill" / "SKILL.md").is_file():
            copies.append((root / "codex-skill", host.home / ".agents" / "skills" / "memory"))
    if "opencode" in clients:
        tree(host.home / ".opencode" / "skills", SKILLS)
    if "continue" in clients:
        copies.append((skills / "memory-protocol" / "SKILL.md", host.home / ".continue" / "rules" / "memory-protocol.md"))
    return copies


def apply_skills(copies: list[tuple[Path, Path]]) -> list[Path]:
    for source, target in copies:
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            atomic_write(target, source.read_text(encoding="utf-8"))
    return [target for _source, target in copies]

"""Client registration for installers: `python -m setup_wizard.register --client cursor ...` or `tam setup register`.

install.sh, install.ps1 and the npm wrapper call this instead of writing client configs themselves. Every config is
parsed before anything is written; a file that does not parse stops the run with nothing changed.
"""
import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TextIO

from setup_wizard import clients, extras, upgrade
from setup_wizard.contracts import WizardError
from setup_wizard.files import deferred_interrupts
from setup_wizard.locations import install_root, locate_memory_dir

USAGE_ERROR = 64
FAILED = 1
SECRET_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD")


class NotRegistered(Exception):
    """A config file was written but does not read back with the memory entry."""


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tam setup register",
                                description="Register the local memory server with MCP clients (for installers).")
    p.add_argument("--client", action="append", required=True, choices=[c.id for c in clients.CLIENTS])
    p.add_argument("--memory-dir", type=Path, help="default: TAM_MEMORY_DIR or ~/.tam")
    p.add_argument("--command", help="server command; default: this installation's total-agent-memory")
    p.add_argument("--arg", action="append", default=[], help="server argument (repeatable)")
    p.add_argument("--env", action="append", default=[], metavar="KEY=VALUE", help="extra server env (repeatable)")
    p.add_argument("--hooks", action=argparse.BooleanOptionalAction, default=None,
                   help="Claude Code hooks (default: on when claude-code is registered)")
    p.add_argument("--overwrite-hooks", action="store_true", help="replace hook scripts that already exist")
    p.add_argument("--skills", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--unregister", action="store_true", help="remove the memory entry (and hooks) instead")
    return p


def _env(pairs: list[str]) -> dict[str, str]:
    env = {}
    for pair in pairs:
        key, separator, value = pair.partition("=")
        if not separator or not key.strip():
            raise WizardError(f"--env {pair!r}: expected KEY=VALUE")
        env[key.strip()] = value
    return env


def _entry(client: clients.Client, host: clients.Host, args: argparse.Namespace, memory_dir: Path) -> clients.ServerEntry:
    existing = clients.current_entry(client, host) or {}
    env = {str(k): str(v) for k, v in (existing.get("env") or {}).items()}
    env.update({"TAM_MEMORY_DIR": str(memory_dir), **_env(args.env)})
    if args.command:
        return clients.ServerEntry(args.command, tuple(args.arg), env)
    return clients.server_entry(env)


def register(args: argparse.Namespace, environ: Mapping[str, str], host: clients.Host, out: TextIO) -> None:
    root = install_root()
    memory_dir = (args.memory_dir.expanduser() if args.memory_dir else locate_memory_dir(environ)).resolve()
    ids = list(dict.fromkeys(args.client))
    changes = []
    for client_id in ids:
        client = clients.BY_ID[client_id]
        entry = _entry(client, host, args, memory_dir)
        private = any(hint in key.upper() for key in entry.env for hint in SECRET_HINTS)
        changes.append(clients.plan(client, entry, host, private, root))
    hooks = args.hooks if args.hooks is not None else "claude-code" in ids
    hook_plan = extras.plan_hooks(root, host, args.overwrite_hooks) \
        if hooks and "claude-code" in ids and extras.hooks_available(root, host) else None
    copies = extras.skill_copies(root, host, ids) if args.skills else []
    with deferred_interrupts():
        for change in changes:
            clients.write(change)
            out.write(f"  OK: MCP memory registered for {change.client.label} in {change.path}\n")
            out.writelines(f"  OK: {note}\n" for note in change.notes)
        if hook_plan is not None:
            copied, skipped = extras.apply_hooks(hook_plan)
            out.write(f"  OK: Hooks synced to {host.home / '.claude' / 'hooks'} (copied={copied}, skipped={skipped})\n")
            out.write(f"  OK: Hooks registered in {hook_plan.settings}: "
                      f"{', '.join(sorted(set(hook_plan.added))) or 'already present'}\n")
        out.writelines(f"  OK: skill installed to {target}\n" for target in extras.apply_skills(copies))
        record = upgrade.record_registration(environ, host, memory_dir, ids)
    missing = [str(change.path) for change in changes if clients.current_entry(change.client, host) is None]
    if missing:
        raise NotRegistered(", ".join(missing))
    out.write(f"  OK: setup record {record} (personal mode)\n")
    for change in changes:
        if change.client.restart:
            out.write(f"  Next: {change.client.restart}\n")


def unregister(args: argparse.Namespace, host: clients.Host, out: TextIO) -> None:
    removals = [clients.plan_removal(clients.BY_ID[c], host) for c in dict.fromkeys(args.client)]
    with deferred_interrupts():
        for change in removals:
            if change is not None:
                clients.write(change)
                out.write(f"  OK: removed the memory entry from {change.path}\n")
        if "claude-code" in args.client:
            settings = extras.remove_hooks(host)
            if settings is not None:
                out.write(f"  OK: removed memory hooks from {settings}\n")


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None,
         host: clients.Host | None = None, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    env = os.environ if environ is None else environ
    try:
        args = parser().parse_args(list(argv if argv is not None else sys.argv[1:]))
    except SystemExit as exc:
        return USAGE_ERROR if exc.code else 0
    host = host or clients.Host.current()
    try:
        if args.unregister:
            unregister(args, host, out)
        else:
            register(args, env, host, out)
    except WizardError as exc:
        sys.stderr.write(f"  ERROR: {exc}\n" + ("" if args.unregister else "  Nothing was changed.\n"))
        return FAILED
    except ValueError as exc:
        sys.stderr.write(f"  ERROR: {exc}\n")
        return FAILED
    except NotRegistered as exc:
        sys.stderr.write(f"  ERROR: the memory entry is missing after writing {exc}\n")
        return FAILED
    except OSError as exc:
        sys.stderr.write(f"  ERROR: could not write {exc.filename or ''}: {exc.strerror}\n")
        return FAILED
    return 0


if __name__ == "__main__":
    sys.exit(main())

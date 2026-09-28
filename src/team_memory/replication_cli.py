"""`tam-team replication ...`: generate the Litestream config, check it, restore a server from the replica, drop a purged workspace."""
import argparse
import os
import sys
from pathlib import Path

from team_memory import replication
from team_memory.contracts import DomainError
from team_memory.registry import Registry
from team_memory.replica_store import ObjectStore
from team_memory.settings import MASTER_KEY_ENV, MASTER_KEY_FILE

DOCS = "docs/TEAM_BACKUP.md"


def add_commands(commands) -> None:
    parser = commands.add_parser("replication", help="Continuous backup with Litestream (see " + DOCS + ")")
    sub = parser.add_subparsers(dest="replication_command", required=True)
    config = sub.add_parser("config", help="Write litestream.yml for this data directory")
    config.add_argument("--out", type=Path, help="Destination file (mode 0600); prints to stdout when omitted")
    sub.add_parser("prepare", help="Switch every database to WAL mode (server stopped)")
    state = sub.add_parser("status", help="Compare local databases with the replica; exit 1 on problems")
    state.add_argument("--json", action="store_true")
    recovery = sub.add_parser("restore", help="Rebuild a server data directory from the replica")
    recovery.add_argument("--to", dest="destination", type=Path, required=True, help="New directory to create")
    recovery.add_argument("--timestamp", help="Point in time, ISO 8601 with zone (e.g. 2026-09-25T14:30:00Z)")
    removal = sub.add_parser("drop", help="Delete the replica of a purged workspace")
    target = removal.add_mutually_exclusive_group(required=True)
    target.add_argument("--user", help="User whose personal area was purged")
    target.add_argument("--team", help="Department whose workspace was deleted")
    target.add_argument("--workspace", help="Workspace key, as `replication status` lists it")
    removal.add_argument("--confirm", required=True, help="Repeat the --user, --team or --workspace value")
    sub.add_parser("init-bucket", help="Create the bucket (or directory) the replica URL names")


def _settings(parser: argparse.ArgumentParser) -> replication.ReplicationSettings:
    settings = replication.ReplicationSettings.from_env()
    if settings is None:
        parser.exit(1, f"Continuous backup is off: set {replication.URL_ENV} (see {DOCS})\n")
    return settings


def _print_status(result: replication.ReplicationStatus) -> None:
    out = sys.stdout
    width = max((len(row.path) for row in result.databases), default=0)
    out.write(f"Replica: {result.replica}\nLitestream: {result.litestream or 'not installed here'}\n")
    for row in result.databases:
        uploaded = (f"{row.replica.files} files, last upload {row.replica.last_upload.isoformat(timespec='seconds')}"
                    if row.replica else "no replica")
        out.write(f"  {row.path:<{width}}  {row.state:<10}  "
                  f"{row.journal_mode or '-':<6}  {uploaded}\n")
    for problem in result.problems:
        out.write(f"PROBLEM: {problem}\n")
    for warning in result.warnings:
        out.write(f"WARNING: {warning}\n")


def run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    root = args.root.expanduser().resolve()
    settings = _settings(parser)
    command = args.replication_command
    try:
        if command != "restore":
            replication.require_sqlite(root)
        if command == "config":
            if args.out is None:
                sys.stdout.write(replication.render_config(root, settings))
            else:
                replication.write_config(root, settings, args.out)
                sys.stdout.write(f"Wrote {args.out}. Start: litestream replicate -config {args.out}\n")
        elif command == "prepare":
            before = replication.prepare(root)
            switched = sorted(path for path, mode in before.items() if mode != "wal")
            sys.stdout.write(f"{len(before)} databases in WAL mode; switched now: {', '.join(switched) or 'none'}\n")
        elif command == "init-bucket":
            sys.stdout.write(replication.init_bucket(settings) + "\n")
        else:
            store = replication.open_store(settings)
            try:
                _run_with_store(args, parser, root, settings, store)
            finally:
                store.close()
    except (DomainError, FileExistsError) as exc:
        parser.exit(1, f"{exc}\n")


def _run_with_store(args: argparse.Namespace, parser: argparse.ArgumentParser, root: Path,
                    settings: replication.ReplicationSettings, store: ObjectStore) -> None:
    command = args.replication_command
    if command == "status":
        result = replication.status(root, settings, store, replication.Litestream.from_env())
        if args.json:
            sys.stdout.write(result.model_dump_json(indent=2) + "\n")
        else:
            _print_status(result)
        if result.problems:
            parser.exit(1)
    elif command == "restore":
        result = replication.restore(settings, args.destination, store, replication.Litestream.from_env(),
                                     args.timestamp)
        sys.stdout.write(f"Restored {len(result.databases)} databases to {result.destination} "
                         f"(as of {result.timestamp or 'the latest replicated transaction'}).\n")
        if result.skipped:
            sys.stdout.write(f"Not present at that time: {', '.join(result.skipped)}\n")
        if not os.environ.get(MASTER_KEY_ENV):
            sys.stdout.write(f"Copy {MASTER_KEY_FILE} from the old data directory (or set {MASTER_KEY_ENV}) "
                             "before starting, or saved provider keys cannot be decrypted.\n")
    elif command == "drop":
        registry = Registry(root)
        if args.confirm != (args.user or args.team or args.workspace):
            parser.exit(1, "--confirm must repeat the --user, --team or --workspace value\n")
        key = ("personal_" + Registry.digest(args.user) if args.user else
               registry.team_workspace_key(args.team) if args.team else args.workspace)
        removed = replication.drop(registry, settings, store, key)
        sys.stdout.write(f"Deleted {removed} replica objects of {key}.\n")

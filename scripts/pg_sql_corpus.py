#!/usr/bin/env python3
"""Record the SQL the team server issues on SQLite, for the PostgreSQL translation corpus.

Runs the team test suite on the SQLite backend with a recorder installed in every Python
process it starts (spawned team workers included, via a generated ``sitecustomize``), and
writes tests/fixtures/pg_sql_corpus.jsonl: one JSON object per distinct statement text

    {"target": "workspace" | "identity" | "learning", "method": "execute" | "executemany"
     | "executescript", "sql": "<SQL before parameter substitution>", "sites": ["<file>:<function>", ...]}

``target`` comes from the database file (memory.db, identity.db, learning.db); other
databases are not recorded. ``sites`` name the function under src/ that issued the SQL and
its caller under src/ ("server.py:Store.q < server.py:Store.get"); SQL issued directly by
test code is not recorded. tests/test_pg_sql_corpus.py translates
every statement and prepares it against a provisioned PostgreSQL schema.

Usage:
    python scripts/pg_sql_corpus.py                    # default team suite -> fixture
    python scripts/pg_sql_corpus.py --output x.jsonl -- tests/test_team_memory.py -k save
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src"
DEFAULT_OUTPUT = ROOT / "tests" / "fixtures" / "pg_sql_corpus.jsonl"
DIRECTORY_ENV = "TAM_SQL_CORPUS_DIR"
SITE_DEPTH = 2
TARGETS = {"memory.db": "workspace", "identity.db": "identity", "learning.db": "learning"}
TEAM_SUITE_GLOB = "tests/test_team_*.py"
# SQLite-only features of the team server: file replication and file-level backups have
# their own PostgreSQL implementation (pg_backup), so their SQL is not corpus material.
EXCLUDED_TEST_FILES = frozenset({
    "tests/test_team_replication.py",
    "tests/test_team_replication_litestream.py",
    "tests/test_team_lifecycle.py",
    "tests/test_team_worker_pg.py",
})
SITECUSTOMIZE = """\
import sys
sys.path.insert(0, {scripts!r})
import pg_sql_corpus
pg_sql_corpus.install()
"""


# ── recorder (runs inside every test and worker process) ──

class _Recorder:
    def __init__(self, directory: Path):
        self.path = directory / f"{os.getpid()}.jsonl"
        self.seen: set[tuple[str, str, str, str]] = set()
        self.lock = threading.Lock()

    def record(self, target: str, method: str, sql: object) -> None:
        if not isinstance(sql, str):
            return
        site = _call_site()
        if site is None:
            return
        key = (target, method, sql, site)
        with self.lock:
            if key in self.seen:
                return
            self.seen.add(key)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"target": target, "method": method, "sql": sql, "site": site}) + "\n")


_RECORDER: _Recorder | None = None


def _call_site() -> str | None:
    """Nearest function under src/ that issued the SQL, then its caller under src/ when there is one:
    ``<path>:<function>`` or ``<path>:<function> < <path>:<caller>``. None when test code issued it."""
    frame = sys._getframe(2)
    this_file = str(Path(__file__).resolve())
    sites: list[str] = []
    while frame is not None and len(sites) < SITE_DEPTH:
        filename = frame.f_code.co_filename
        if filename != this_file and "sqlite3" not in Path(filename).parts:
            path = Path(filename).resolve()
            if path.is_relative_to(SOURCE_ROOT):
                sites.append(f"{path.relative_to(SOURCE_ROOT).as_posix()}:{frame.f_code.co_qualname}")
            elif not sites and path.is_relative_to(ROOT / "tests"):
                return None
            elif sites:
                break
        frame = frame.f_back
    return " < ".join(sites) if sites else None


def _target(database: object) -> str | None:
    if isinstance(database, bytes):
        database = database.decode("utf-8", "replace")
    if not isinstance(database, (str, os.PathLike)):
        return None
    text = os.fspath(database)
    if text.startswith("file:"):
        text = text[len("file:"):].split("?", 1)[0]
    return TARGETS.get(Path(text).name)


_CURSOR_CLASSES: dict[tuple[type, str], type] = {}
_CONNECTION_CLASSES: dict[tuple[type, str], type] = {}


def _recording_cursor(base: type, target: str) -> type:
    key = (base, target)
    if key not in _CURSOR_CLASSES:
        class RecordingCursor(base):
            def execute(self, sql, *args, **kwargs):
                _RECORDER.record(target, "execute", sql)
                return super().execute(sql, *args, **kwargs)

            def executemany(self, sql, *args, **kwargs):
                _RECORDER.record(target, "executemany", sql)
                return super().executemany(sql, *args, **kwargs)

            def executescript(self, sql, *args, **kwargs):
                _RECORDER.record(target, "executescript", sql)
                return super().executescript(sql, *args, **kwargs)

        _CURSOR_CLASSES[key] = RecordingCursor
    return _CURSOR_CLASSES[key]


def _recording_connection(base: type, target: str) -> type:
    key = (base, target)
    if key not in _CONNECTION_CLASSES:
        class RecordingConnection(base):
            def execute(self, sql, *args, **kwargs):
                _RECORDER.record(target, "execute", sql)
                return super().execute(sql, *args, **kwargs)

            def executemany(self, sql, *args, **kwargs):
                _RECORDER.record(target, "executemany", sql)
                return super().executemany(sql, *args, **kwargs)

            def executescript(self, sql, *args, **kwargs):
                _RECORDER.record(target, "executescript", sql)
                return super().executescript(sql, *args, **kwargs)

            def cursor(self, factory=sqlite3.Cursor):
                return super().cursor(_recording_cursor(factory, target))

        _CONNECTION_CLASSES[key] = RecordingConnection
    return _CONNECTION_CLASSES[key]


def install() -> None:
    """Patch sqlite3.connect in this process when TAM_SQL_CORPUS_DIR is set."""
    global _RECORDER
    directory = os.environ.get(DIRECTORY_ENV)
    if not directory or _RECORDER is not None:
        return
    _RECORDER = _Recorder(Path(directory))
    original = sqlite3.connect

    def connect(database, *args, **kwargs):
        target = _target(database)
        if target is None or len(args) >= 5:
            return original(database, *args, **kwargs)
        factory = kwargs.pop("factory", sqlite3.Connection)
        return original(database, factory=_recording_connection(factory, target), **kwargs)

    sqlite3.connect = connect


# ── driver ──

def merge(directory: Path) -> list[dict]:
    """Distinct (target, method, sql) entries with their sorted call sites."""
    merged: dict[tuple[str, str, str], set[str]] = {}
    for path in sorted(directory.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            entry = json.loads(line)
            merged.setdefault((entry["target"], entry["method"], entry["sql"]), set()).add(entry["site"])
    return [{"target": target, "method": method, "sql": sql, "sites": sorted(sites)}
            for (target, method, sql), sites in sorted(merged.items())]


def default_tests() -> list[str]:
    return [path.relative_to(ROOT).as_posix() for path in sorted(ROOT.glob(TEAM_SUITE_GLOB))
            if path.relative_to(ROOT).as_posix() not in EXCLUDED_TEST_FILES]


def run(output: Path, pytest_args: Iterable[str]) -> int:
    with tempfile.TemporaryDirectory(prefix="tam-sql-corpus-") as scratch:
        scratch_path = Path(scratch)
        hook = scratch_path / "hook"
        records = scratch_path / "records"
        home = scratch_path / "home"
        for directory in (hook, records, home):
            directory.mkdir()
        (hook / "sitecustomize.py").write_text(SITECUSTOMIZE.format(scripts=str(ROOT / "scripts")), encoding="utf-8")
        environment = dict(os.environ)
        environment[DIRECTORY_ENV] = str(records)
        environment["PYTHONPATH"] = os.pathsep.join(filter(None, [str(hook), environment.get("PYTHONPATH")]))
        # The suite must never see the real home directory (LaunchAgents, ~/.tam).
        environment["HOME"] = str(home)
        environment.pop("TAM_TEAM_DATABASE_URL", None)
        command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--backend=sqlite",
                   *(list(pytest_args) or default_tests())]
        completed = subprocess.run(command, cwd=ROOT, env=environment, check=False)
        entries = merge(records)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    targets = {target: sum(1 for entry in entries if entry["target"] == target) for target in TARGETS.values()}
    print(json.dumps({"event": "sql_corpus_written", "path": str(output), "statements": len(entries),
                      "by_target": targets, "pytest_exit_code": completed.returncode}))
    return completed.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("pytest_args", nargs="*", help="pytest arguments (default: the team suite)")
    arguments = parser.parse_args(argv)
    return run(arguments.output, arguments.pytest_args)


if __name__ == "__main__":
    sys.exit(main())

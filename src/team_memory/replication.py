"""Continuous replication of the team server's SQLite files with Litestream 0.5 (directory watch, v0.5.4+).

Litestream runs next to the server (a sidecar container or its own service) and streams every committed
transaction of `identity.db`, `learning.db` and each `workspaces/<key>/memory.db` to an S3-compatible bucket
or a directory. Two watched directory entries follow departments and people as their workspaces appear
and disappear, so the generated config never needs regenerating when a workspace is added.

Replica key layout (as written by Litestream): `<prefix>/identity.db/...`, `<prefix>/learning.db/...`,
`<prefix>/workspaces/<key>/memory.db/...`. Litestream keeps the replica of a deleted database forever;
`drop` removes it after `tam-team user-purge`.
"""
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from contextlib import ExitStack, closing
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Literal
from urllib.parse import unquote, urlsplit

from pydantic import ValidationError, model_validator

from team_memory.contracts import DTO, Conflict, DomainError
from team_memory.lifecycle import DATABASE_PATH, ServerLease, verify_database
from team_memory.registry import Registry
from team_memory.replica_store import Credentials, FileStore, ObjectStore, S3Store
from version import VERSION

LOGGER = logging.getLogger(__name__)

URL_ENV = "TAM_TEAM_REPLICA_URL"
ENDPOINT_ENV = "TAM_TEAM_REPLICA_ENDPOINT"
REGION_ENV = "TAM_TEAM_REPLICA_REGION"
PATH_STYLE_ENV = "TAM_TEAM_REPLICA_FORCE_PATH_STYLE"
SYNC_INTERVAL_ENV = "TAM_TEAM_REPLICA_SYNC_INTERVAL"
SNAPSHOT_INTERVAL_ENV = "TAM_TEAM_REPLICA_SNAPSHOT_INTERVAL"
RETENTION_ENV = "TAM_TEAM_REPLICA_RETENTION"
METRICS_ADDR_ENV = "TAM_TEAM_REPLICA_METRICS_ADDR"
LITESTREAM_ENV = "TAM_TEAM_LITESTREAM_BIN"

DEFAULT_REGION = "us-east-1"
DEFAULT_SYNC_INTERVAL = "1s"
DEFAULT_SNAPSHOT_INTERVAL = "24h"
DEFAULT_RETENTION = "168h"
DEFAULT_LITESTREAM = "litestream"
COMPOSE_DATA_DIR = "/team-data"
WORKSPACES = "workspaces"
IDENTITY = "identity.db"
CONTROL_PATTERN = "*.db"
WORKSPACE_PATTERN = "memory.db"
RESTORE_TIMEOUT_SECONDS = 3600
VERSION_TIMEOUT_SECONDS = 30
SQLITE_TIMEOUT_SECONDS = 10
STDERR_TAIL_CHARS = 500
SQLITE_BACKEND = "sqlite"
StorageBackend = Literal["sqlite", "postgres"]

TRUE_VALUES = ("1", "true", "yes", "on")
FALSE_VALUES = ("0", "false", "no", "off")
DURATION = re.compile(r"(?:\d+(?:\.\d+)?(?:h|ms|m|s))+")
DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(h|ms|m|s)")
DURATION_SECONDS = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}
BUCKET = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
PREFIX = re.compile(r"[A-Za-z0-9._=+-]+(?:/[A-Za-z0-9._=+-]+)*")
REGION = re.compile(r"[a-z0-9-]{1,32}")
LISTEN_ADDR = re.compile(r"[A-Za-z0-9.\[\]:-]{0,253}:\d{1,5}")
WORKSPACE_KEY = re.compile(r"shared|(?:personal|team)_[a-f0-9]{64}")
LTX_KEY = re.compile(r"(?P<db>.+)/(?:ltx/\d+|[0-9a-f]{4})/(?P<min>[0-9a-f]{16})-(?P<max>[0-9a-f]{16})\.ltx")


def duration_seconds(value: str) -> float:
    if not DURATION.fullmatch(value):
        raise ValueError(f"{value!r} is not a duration such as 1s, 30m or 168h")
    return sum(float(number) * DURATION_SECONDS[unit] for number, unit in DURATION_PART.findall(value))


def _flag(name: str, raw: str) -> bool | None:
    value = raw.strip().lower()
    if not value:
        return None
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    raise DomainError(f"{name} must be true or false")


class ReplicationSettings(DTO):
    """Where and how often Litestream replicates. Never holds credentials: those stay in AWS_* variables."""

    url: str
    endpoint: str | None = None
    region: str = DEFAULT_REGION
    force_path_style: bool | None = None
    sync_interval: str = DEFAULT_SYNC_INTERVAL
    snapshot_interval: str = DEFAULT_SNAPSHOT_INTERVAL
    retention: str = DEFAULT_RETENTION
    metrics_addr: str | None = None

    @model_validator(mode="after")
    def validate_all(self):
        parts = urlsplit(self.url)
        if parts.username or parts.password or parts.query or parts.fragment or parts.port:
            raise ValueError("Replica URL must not carry credentials, a port, a query or a fragment; "
                             "use AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY and TAM_TEAM_REPLICA_ENDPOINT")
        if parts.scheme == "s3":
            if not BUCKET.fullmatch(parts.hostname or ""):
                raise ValueError("Replica URL must name a bucket: s3://bucket/path")
            prefix = parts.path.strip("/")
            if prefix and (not PREFIX.fullmatch(prefix) or ".." in prefix.split("/")):
                raise ValueError("Replica path may hold letters, digits and . _ = + - separated by /")
        elif parts.scheme == "file":
            path = unquote(parts.path)
            if parts.netloc or not PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts \
                    or PurePosixPath(path) == PurePosixPath("/"):
                raise ValueError("A directory replica must be an absolute path: file:///srv/tam-replica")
            if self.endpoint:
                raise ValueError("An endpoint applies only to s3:// replicas")
        else:
            raise ValueError("Replica URL must start with s3:// or file://")
        if self.endpoint is not None:
            endpoint = urlsplit(self.endpoint)
            if endpoint.scheme not in ("http", "https") or not endpoint.hostname or endpoint.username \
                    or endpoint.password or endpoint.query or endpoint.fragment or endpoint.path not in ("", "/"):
                raise ValueError("Replica endpoint must be an http(s) origin such as https://s3.example.com")
        if not REGION.fullmatch(self.region):
            raise ValueError("Replica region must look like us-east-1")
        for name, value in (("sync interval", self.sync_interval), ("snapshot interval", self.snapshot_interval),
                            ("retention", self.retention)):
            if duration_seconds(value) <= 0:
                raise ValueError(f"Replica {name} must be positive")
        if duration_seconds(self.snapshot_interval) > duration_seconds(self.retention):
            raise ValueError("Replica retention must be at least the snapshot interval")
        if self.metrics_addr is not None and not LISTEN_ADDR.fullmatch(self.metrics_addr):
            raise ValueError("Metrics address must look like :9090 or 127.0.0.1:9090")
        return self

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "ReplicationSettings | None":
        env = os.environ if environ is None else environ
        url = env.get(URL_ENV, "").strip()
        if not url:
            return None
        try:
            return cls(url=url.rstrip("/"), endpoint=env.get(ENDPOINT_ENV, "").strip().rstrip("/") or None,
                       region=env.get(REGION_ENV, "").strip() or DEFAULT_REGION,
                       force_path_style=_flag(PATH_STYLE_ENV, env.get(PATH_STYLE_ENV, "")),
                       sync_interval=env.get(SYNC_INTERVAL_ENV, "").strip() or DEFAULT_SYNC_INTERVAL,
                       snapshot_interval=env.get(SNAPSHOT_INTERVAL_ENV, "").strip() or DEFAULT_SNAPSHOT_INTERVAL,
                       retention=env.get(RETENTION_ENV, "").strip() or DEFAULT_RETENTION,
                       metrics_addr=env.get(METRICS_ADDR_ENV, "").strip() or None)
        except ValidationError as exc:
            raise DomainError(exc.errors()[0]["msg"].removeprefix("Value error, ")) from exc

    @property
    def scheme(self) -> Literal["s3", "file"]:
        return "s3" if self.url.startswith("s3://") else "file"

    @property
    def bucket(self) -> str:
        return urlsplit(self.url).hostname or ""

    @property
    def prefix(self) -> str:
        return "" if self.scheme == "file" else urlsplit(self.url).path.strip("/")

    @property
    def directory(self) -> Path:
        return Path(unquote(urlsplit(self.url).path))

    @property
    def path_style(self) -> bool:
        return self.force_path_style if self.force_path_style is not None else self.endpoint is not None

    def replica_url(self, relative: str = "") -> str:
        return self.url.rstrip("/") + ("/" + relative if relative else "")

    def key(self, relative: str) -> str:
        return "/".join(part for part in (self.prefix, relative) if part)

    def environment(self) -> dict[str, str]:
        """The TAM_TEAM_REPLICA_* variables that reproduce these settings (for env files; no secrets)."""
        values = {URL_ENV: self.url, REGION_ENV: self.region, SYNC_INTERVAL_ENV: self.sync_interval,
                  SNAPSHOT_INTERVAL_ENV: self.snapshot_interval, RETENTION_ENV: self.retention,
                  PATH_STYLE_ENV: str(self.path_style).lower()}
        if self.endpoint:
            values[ENDPOINT_ENV] = self.endpoint
        if self.metrics_addr:
            values[METRICS_ADDR_ENV] = self.metrics_addr
        return values


def open_store(settings: ReplicationSettings, environ: Mapping[str, str] | None = None,
               transport=None) -> ObjectStore:
    if settings.scheme == "file":
        return FileStore(settings.directory)
    return S3Store(settings.bucket, settings.region, settings.endpoint, settings.path_style,
                   Credentials.from_env(os.environ if environ is None else environ), transport)


# Storage backend

POSTGRES_NOTE = ("Continuous backup with Litestream covers the SQLite storage backend only; this server stores its "
                 "data in PostgreSQL, so back it up with PostgreSQL tooling (pg_dump, WAL archiving, managed backups)")


def storage_backend(root: Path) -> StorageBackend:
    """The storage backend of the team server in `root` ("sqlite" or "postgres").

    The single place replication and the setup wizard ask: the dashboard's database setting
    (database.json), else TAM_TEAM_DATABASE_URL, else SQLite (lifecycle.configured_backend).
    """
    from team_memory.lifecycle import configured_backend

    return configured_backend(root).value


def require_sqlite(root: Path, backend: Callable[[Path], str] | None = None) -> None:
    if (backend or storage_backend)(root) != SQLITE_BACKEND:
        raise Conflict(POSTGRES_NOTE)


# Config rendering

def _q(value: str) -> str:
    return json.dumps(value)


def _replica_fields(settings: ReplicationSettings, url: str) -> list[tuple[str, str]]:
    fields = [("url", _q(url))]
    if settings.scheme == "s3":
        if settings.endpoint:
            fields.append(("endpoint", _q(settings.endpoint)))
        fields += [("region", _q(settings.region)), ("force-path-style", str(settings.path_style).lower())]
    return fields


def _document(header: list[str], top: list[str], entries: list[tuple[str, str, bool, list[tuple[str, str]]]]) -> str:
    lines = [*(f"# {line}" for line in header), *top, "dbs:"]
    for directory, pattern, recursive, replica in entries:
        lines += [f"  - dir: {_q(directory)}", f"    pattern: {_q(pattern)}",
                  f"    recursive: {str(recursive).lower()}", "    watch: true", "    replica:",
                  *(f"      {name}: {value}" for name, value in replica)]
    return "\n".join(lines) + "\n"


def _top(metrics_addr: str | None, snapshot_interval: str, retention: str) -> list[str]:
    lines = ["logging:", "  level: info", "  type: json"]
    if metrics_addr is not None:
        lines.append(f"addr: {_q(metrics_addr)}")
    return lines + ["snapshot:", f"  interval: {_q(snapshot_interval)}", f"  retention: {_q(retention)}"]


def render_config(root: Path, settings: ReplicationSettings) -> str:
    """litestream.yml for a server whose data directory is `root` on the machine that runs Litestream."""
    root = Path(root)

    def replica(suffix: str) -> list[tuple[str, str]]:
        return [*_replica_fields(settings, settings.replica_url(suffix)), ("sync-interval", _q(settings.sync_interval))]

    return _document(
        ["Generated by `tam-team replication config`; regenerate it instead of editing.",
         "Credentials are read from AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY in Litestream's environment."],
        _top(settings.metrics_addr, settings.snapshot_interval, settings.retention),
        [(str(root), CONTROL_PATTERN, False, replica("")),
         (str(root / WORKSPACES), WORKSPACE_PATTERN, True, replica(WORKSPACES))])


def render_compose_template(data_dir: str = COMPOSE_DATA_DIR) -> str:
    """The sidecar config shipped as docker/litestream.team.yml; Litestream expands ${VAR} at start."""

    def replica(suffix: str) -> list[tuple[str, str]]:
        return [("url", _q("${" + URL_ENV + "}" + ("/" + suffix if suffix else ""))),
                ("endpoint", _q("${" + ENDPOINT_ENV + "}")), ("region", _q("${" + REGION_ENV + "}")),
                ("force-path-style", "${" + PATH_STYLE_ENV + "}"),
                ("sync-interval", _q("${" + SYNC_INTERVAL_ENV + "}"))]

    return _document(
        ["Litestream sidecar config for docker-compose.team.yml (profile `litestream`).",
         "Rendered by team_memory.replication.render_compose_template(); a test keeps them identical.",
         "Litestream expands the ${TAM_TEAM_REPLICA_*} and AWS_* variables from the container environment."],
        _top("${" + METRICS_ADDR_ENV + "}", "${" + SNAPSHOT_INTERVAL_ENV + "}", "${" + RETENTION_ENV + "}"),
        [(data_dir, CONTROL_PATTERN, False, replica("")),
         (str(PurePosixPath(data_dir) / WORKSPACES), WORKSPACE_PATTERN, True, replica(WORKSPACES))])


def render_restore_config(settings: ReplicationSettings, targets: Mapping[str, Path]) -> str:
    lines = ["dbs:"]
    for relative, path in targets.items():
        lines += [f"  - path: {_q(str(path))}", "    replica:",
                  *(f"      {name}: {value}" for name, value in _replica_fields(settings, settings.replica_url(relative)))]
    return "\n".join(lines) + "\n"


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def write_config(root: Path, settings: ReplicationSettings, out: Path) -> None:
    root = root.resolve()
    if not (root / IDENTITY).is_file():
        raise Conflict("Server identity database does not exist; start the server once or run setup first")
    (root / WORKSPACES).mkdir(exist_ok=True, mode=0o700)
    _write_private(out, render_config(root, settings))
    LOGGER.info(json.dumps({"event": "replication_config_written", "path": str(out), "replica": settings.url}))


# Local databases

def local_databases(root: Path) -> list[str]:
    found = [name for name in (IDENTITY, "learning.db") if (root / name).is_file()]
    found += [path.relative_to(root).as_posix() for path in sorted((root / WORKSPACES).glob("*/memory.db"))]
    return [relative for relative in found if DATABASE_PATH.fullmatch(relative)]


def journal_mode(path: Path) -> str:
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=SQLITE_TIMEOUT_SECONDS)) as db:
        return str(db.execute("PRAGMA journal_mode").fetchone()[0]).lower()


def prepare(root: Path) -> dict[str, str]:
    """Switch every database to WAL with the server stopped; returns each database's previous journal mode.

    Litestream does this itself on start; running it ahead makes the first start of a fresh sidecar
    independent of whether the server holds a connection at that moment.
    """
    root = root.resolve()
    if not (root / IDENTITY).is_file():
        raise Conflict("Server identity database does not exist")
    with ServerLease(root), ExitStack() as leases:
        (root / WORKSPACES).mkdir(exist_ok=True, mode=0o700)
        databases = local_databases(root)
        for relative in databases:
            if relative.startswith(WORKSPACES + "/"):
                leases.enter_context(ServerLease((root / relative).parent))
        previous = {}
        for relative in databases:
            with closing(sqlite3.connect(root / relative, timeout=SQLITE_TIMEOUT_SECONDS)) as db:
                previous[relative] = str(db.execute("PRAGMA journal_mode").fetchone()[0]).lower()
                if db.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() != "wal":
                    raise Conflict(f"{relative} could not switch to WAL mode")
    LOGGER.info(json.dumps({"event": "replication_prepared", "databases": len(previous),
                            "switched": sorted(k for k, v in previous.items() if v != "wal")}))
    return previous


# Replica contents

class ReplicaDatabase(DTO):
    path: str
    files: int
    bytes: int
    last_upload: datetime
    max_txid: str


def replica_databases(settings: ReplicationSettings, store: ObjectStore) -> dict[str, ReplicaDatabase]:
    base = settings.key("")
    prefix = base + "/" if base else ""
    grouped: dict[str, list] = {}
    for item in store.list(prefix):
        match = LTX_KEY.fullmatch(item.key[len(prefix):])
        if match and DATABASE_PATH.fullmatch(match["db"]):
            grouped.setdefault(match["db"], []).append((item, match["max"]))
    return {path: ReplicaDatabase(path=path, files=len(items), bytes=sum(item.size for item, _ in items),
                                  last_upload=max(item.modified for item, _ in items),
                                  max_txid=max(txid for _, txid in items))
            for path, items in sorted(grouped.items())}


# Litestream binary

class Litestream:
    def __init__(self, binary: str = DEFAULT_LITESTREAM, environ: Mapping[str, str] | None = None,
                 run: Callable[..., subprocess.CompletedProcess] = subprocess.run):
        self.binary, self.environ, self._run = binary, dict(os.environ if environ is None else environ), run

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Litestream":
        env = os.environ if environ is None else environ
        return cls(env.get(LITESTREAM_ENV, "").strip() or DEFAULT_LITESTREAM, env)

    def executable(self) -> str | None:
        return shutil.which(self.binary)

    def version(self) -> str | None:
        executable = self.executable()
        if executable is None:
            return None
        result = self._run([executable, "version"], capture_output=True, text=True, env=self.environ,
                           timeout=VERSION_TIMEOUT_SECONDS, check=False)
        if result.returncode != 0:
            LOGGER.warning(json.dumps({"event": "litestream_version_failed", "code": result.returncode,
                                       "stderr": result.stderr[-STDERR_TAIL_CHARS:]}))
            return None
        return result.stdout.strip()

    def restore(self, config: Path, database: Path, output: Path, timestamp: str | None, optional: bool) -> None:
        executable = self.executable()
        if executable is None:
            raise Conflict(f"Litestream is not installed: {self.binary} not found (set {LITESTREAM_ENV})")
        command = [executable, "restore", "-config", str(config), "-o", str(output)]
        if timestamp:
            command += ["-timestamp", timestamp]
        if optional:
            command.append("-if-replica-exists")
        try:
            result = self._run([*command, str(database)], capture_output=True, text=True, env=self.environ,
                               timeout=RESTORE_TIMEOUT_SECONDS, check=False)
        except subprocess.TimeoutExpired as exc:
            raise Conflict(f"Litestream restore of {database.name} timed out") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[-STDERR_TAIL_CHARS:]
            raise Conflict(f"Litestream restore of {database} failed: {detail}")


# Status

class DatabaseStatus(DTO):
    path: str
    journal_mode: str | None
    replica: ReplicaDatabase | None
    state: Literal["replicated", "pending", "orphaned"]


class ReplicationStatus(DTO):
    replica: str
    litestream: str | None
    databases: list[DatabaseStatus]
    problems: list[str]
    warnings: list[str]


def status(root: Path, settings: ReplicationSettings, store: ObjectStore, litestream: Litestream) -> ReplicationStatus:
    root = root.resolve()
    local = local_databases(root)
    remote = replica_databases(settings, store)
    problems, warnings, rows = [], [], []
    if IDENTITY not in local:
        problems.append(f"{root} holds no identity.db; is --root the server data directory?")
    for path in sorted({*local, *remote}, key=lambda p: (p != IDENTITY, p)):
        mode = journal_mode(root / path) if path in local else None
        state = "replicated" if path in local and path in remote else "pending" if path in local else "orphaned"
        if state == "pending":
            problems.append(f"{path} has no replica yet; is Litestream running with this configuration?")
        if mode is not None and mode != "wal":
            problems.append(f"{path} is in {mode} journal mode; Litestream needs WAL "
                            "(run `tam-team replication prepare` with the server stopped)")
        if state == "orphaned":
            key = path.split("/")[1] if path.startswith(WORKSPACES + "/") else path
            warnings.append(f"{path} exists only in the replica; after a purge remove it with "
                            f"`tam-team replication drop --workspace {key} --confirm {key}`")
        rows.append(DatabaseStatus(path=path, journal_mode=mode, replica=remote.get(path), state=state))
    version = litestream.version()
    if version is None:
        warnings.append(f"{litestream.binary} is not available here; `replication restore` needs it "
                        "(a sidecar container may still be replicating)")
    return ReplicationStatus(replica=settings.url, litestream=version, databases=rows, problems=problems,
                             warnings=warnings)


# Restore

def normalize_timestamp(value: str) -> str:
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise DomainError("--timestamp must be ISO 8601, e.g. 2026-09-25T14:30:00Z") from exc
    if moment.tzinfo is None:
        raise DomainError("--timestamp needs a time zone, e.g. 2026-09-25T14:30:00Z")
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


class RestoreResult(DTO):
    destination: str
    timestamp: str | None
    databases: list[str]
    skipped: list[str]


def restore(settings: ReplicationSettings, destination: Path, store: ObjectStore, litestream: Litestream,
            timestamp: str | None = None) -> RestoreResult:
    """Rebuild a whole server data directory from the replica, optionally as of `timestamp`.

    Every database is restored into a staging directory next to `destination`, checked like a
    `tam-team restore` snapshot, and published with one rename. A database created after `timestamp`
    did not exist then and is skipped; identity.db must exist.
    """
    destination = destination.resolve()
    moment = normalize_timestamp(timestamp) if timestamp else None
    if destination.exists():
        raise FileExistsError(destination)
    available = replica_databases(settings, store)
    if IDENTITY not in available:
        raise Conflict(f"The replica at {settings.url} holds no identity.db; check {URL_ENV}")
    order = sorted(available, key=lambda p: (p != IDENTITY, p))
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    restored, skipped = [], []
    with TemporaryDirectory(prefix=".tam-restore-", dir=destination.parent) as temporary:
        staging = Path(temporary) / "data"
        staging.mkdir(mode=0o700)
        config = Path(temporary) / "litestream.yml"
        _write_private(config, render_restore_config(settings, {path: destination / path for path in order}))
        for path in order:
            target = staging / path
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            litestream.restore(config, destination / path, target, moment, optional=path != IDENTITY)
            if not target.is_file():
                if path == IDENTITY:
                    raise Conflict("identity.db did not exist at that time; choose a later --timestamp")
                skipped.append(path)
                continue
            target.chmod(0o600)
            verify_database(target)
            restored.append(path)
        for directory in sorted({(staging / path).parent for path in skipped}, reverse=True):
            if directory != staging and not any(directory.iterdir()):
                directory.rmdir()
        (staging / "restored-from.json").write_text(json.dumps({
            "replica": settings.url, "timestamp": moment or "latest", "version": VERSION,
            "restored_at": datetime.now(UTC).isoformat(), "databases": restored}, indent=2))
        if destination.exists():
            raise FileExistsError(destination)
        staging.rename(destination)
    LOGGER.info(json.dumps({"event": "replica_restored", "destination": str(destination), "timestamp": moment,
                            "databases": len(restored), "skipped": len(skipped)}))
    return RestoreResult(destination=str(destination), timestamp=moment, databases=restored, skipped=skipped)


# Drop

def drop(registry: Registry, settings: ReplicationSettings, store: ObjectStore, workspace_key: str) -> int:
    """Delete the replica of a workspace that no longer exists locally; returns the number of objects removed."""
    if not WORKSPACE_KEY.fullmatch(workspace_key):
        raise DomainError("Workspace key must be shared, personal_<sha256> or team_<sha256>")
    if (registry.root / WORKSPACES / workspace_key).exists():
        raise Conflict("The workspace still exists on the server and Litestream would upload it again; "
                       "delete it first (tam-team user-purge) and retry")
    prefix = settings.key(f"{WORKSPACES}/{workspace_key}/{WORKSPACE_PATTERN}") + "/"
    removed = store.delete_prefix(prefix)
    if store.list(prefix):
        raise Conflict("Litestream is still uploading this workspace; restart Litestream and run drop again")
    registry.record_event("replica_dropped", workspace_key, f"objects={removed}")
    LOGGER.info(json.dumps({"event": "replica_dropped", "workspace": workspace_key, "objects": removed}))
    return removed


def init_bucket(settings: ReplicationSettings, environ: Mapping[str, str] | None = None, transport=None) -> str:
    if settings.scheme == "file":
        settings.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return f"Replica directory ready: {settings.directory}"
    store = S3Store(settings.bucket, settings.region, settings.endpoint, settings.path_style,
                    Credentials.from_env(os.environ if environ is None else environ), transport)
    try:
        store.create_bucket()
    finally:
        store.close()
    return f"Bucket ready: {settings.bucket}"

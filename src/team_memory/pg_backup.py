"""Backup and restore of a team server that runs on PostgreSQL (plan 5, "Backup/restore on PG").

Backup: one ``pg_dump -Fc --no-owner --no-privileges`` of the TAM schemas (tam_control, tam_learning,
tam_compat and every ws_* workspace schema), taken from a snapshot exported by a REPEATABLE READ
transaction that also counts every table, so the manifest's row counts describe exactly the dumped
data. Manifest format_version 2 records the dump's SHA-256, the schemas, the workspace roles and
the counts.

Restore: only into an empty database that meets the prerequisites (extensions installed); the dump
is checked with ``pg_restore --list`` and its checksum, restored in a single transaction, counted
against the manifest, and then every workspace role and grant is provisioned again, because roles
are global to the cluster and never part of a dump.

The password never appears on a command line: pg_dump and pg_restore get the DSN without it and a
temporary 0600 passfile (PGPASSFILE) that is deleted afterwards.
"""

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import psycopg
from psycopg import sql
from pydantic import Field

from tam_db.contracts import (
    COMPAT_SCHEMA,
    CONTROL_SCHEMA,
    LEARNING_SCHEMA,
    WorkspaceProvisioner,
    is_workspace_schema,
)
from team_memory.contracts import DTO, Conflict
from team_memory.database_contracts import DatabaseDsn
from version import VERSION

MANIFEST_FILE = "manifest.json"
DUMP_FILE = "tam.dump"
BACKUP_FORMAT_VERSION = 2
POSTGRES_BACKEND = "postgres"
PG_DUMP_ENV = "TAM_TEAM_PG_DUMP"
PG_RESTORE_ENV = "TAM_TEAM_PG_RESTORE"
PG_DUMP = "pg_dump"
PG_RESTORE = "pg_restore"
WORKSPACE_SCHEMA_GLOB = "ws_*"
FIXED_SCHEMAS = (CONTROL_SCHEMA, LEARNING_SCHEMA, COMPAT_SCHEMA)
WORKSPACE_MAP_TABLE = "workspace_schemas"
TOOL_TIMEOUT_ENV = "TAM_TEAM_PG_DUMP_TIMEOUT_SECONDS"
DEFAULT_TOOL_TIMEOUT_SECONDS = 6 * 60 * 60
VERSION_TIMEOUT_SECONDS = 30
HASH_CHUNK_BYTES = 1024 * 1024
MAX_TOOL_ERROR_CHARS = 2000
TOOL_VERSION = re.compile(r"\(PostgreSQL\)\s+(\d+)")
SERVER_VERSION_DIVISOR = 10000
DUMP_SHA256_PATTERN = r"^[a-f0-9]{64}$"

LOGGER = logging.getLogger(__name__)

ProvisionerFactory = Callable[[], WorkspaceProvisioner]


class TableCount(DTO):
    schema_name: str = Field(min_length=1, max_length=63)
    table: str = Field(min_length=1, max_length=63)
    rows: int = Field(ge=0)


class DumpFile(DTO):
    path: Literal["tam.dump"] = DUMP_FILE
    sha256: str = Field(pattern=DUMP_SHA256_PATTERN)
    bytes: int = Field(ge=0)


class PostgresSnapshot(DTO):
    """``manifest.json`` of a PostgreSQL backup."""

    format_version: Literal[2] = BACKUP_FORMAT_VERSION
    backend: Literal["postgres"] = POSTGRES_BACKEND
    package_version: str
    created_at: str
    server_version_num: int = Field(ge=0)
    instance_id: str | None = None
    dump: DumpFile
    schemas: tuple[str, ...]
    roles: tuple[str, ...]
    workspaces: tuple[str, ...] = Field(description="Workspace keys re-provisioned on restore")
    counts: tuple[TableCount, ...]


def _tool(name: str, variable: str, environ: Mapping[str, str]) -> str:
    configured = environ.get(variable, "").strip()
    path = configured or shutil.which(name)
    if not path or not Path(path).is_file():
        raise Conflict(f"{name} was not found; install the PostgreSQL client tools or set {variable}")
    return path


def _tool_major(path: str) -> int:
    result = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=VERSION_TIMEOUT_SECONDS,
                            check=False)
    match = TOOL_VERSION.search(result.stdout)
    if result.returncode != 0 or match is None:
        raise Conflict(f"Could not read the version of {Path(path).name}")
    return int(match.group(1))


def _timeout(environ: Mapping[str, str]) -> int:
    raw = environ.get(TOOL_TIMEOUT_ENV, "").strip()
    if not raw:
        return DEFAULT_TOOL_TIMEOUT_SECONDS
    if not raw.isdigit() or int(raw) <= 0:
        raise Conflict(f"{TOOL_TIMEOUT_ENV} must be a positive number of seconds")
    return int(raw)


def _passfile_field(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:")


@contextmanager
def _client_environment(dsn: DatabaseDsn, environ: Mapping[str, str]) -> Iterator[dict[str, str]]:
    """Environment for pg_dump/pg_restore: the password only in a temporary 0600 passfile."""
    env = {key: value for key, value in environ.items() if not key.startswith("PG")}
    if dsn.password is None:
        yield env
        return
    password = _passfile_field(dsn.password.get_secret_value())
    lines = [f"*:{host.port}:{_passfile_field(dsn.database)}:{_passfile_field(dsn.user)}:{password}"
             for host in dsn.hosts]
    descriptor, name = tempfile.mkstemp(prefix=".tam-pgpass-")
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        env["PGPASSFILE"] = name
        yield env
    finally:
        Path(name).unlink(missing_ok=True)


def _uri_without_password(dsn: DatabaseDsn) -> str:
    return dsn.model_copy(update={"password": None}).to_uri()


def _run(command: list[str], env: dict[str, str], timeout: int, what: str) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, env=env, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise Conflict(f"{what} did not finish within {timeout} seconds") from exc
    if result.returncode != 0:
        detail = result.stderr.strip()[-MAX_TOOL_ERROR_CHARS:]
        raise Conflict(f"{what} failed (exit {result.returncode}): {detail}")
    return result


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(HASH_CHUNK_BYTES):
            result.update(chunk)
    return result.hexdigest()


def _tam_schemas(connection: psycopg.Connection) -> list[str]:
    names = [row[0] for row in connection.execute("SELECT nspname FROM pg_namespace ORDER BY nspname")]
    return [name for name in names if name in FIXED_SCHEMAS or is_workspace_schema(name)]


def _counts(connection: psycopg.Connection, schemas: list[str]) -> list[TableCount]:
    """Exact row counts. Workspace tables belong to workspace roles: a row-level security policy one of
    them added must never run as the admin, so row_security is off and such a table fails the count."""
    with connection.transaction():
        connection.execute("SET LOCAL row_security = off")
        tables = connection.execute("""
            SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r', 'p') AND n.nspname = ANY(%s) ORDER BY n.nspname, c.relname""",
                                    (schemas,)).fetchall()
        counts = []
        for schema, table in tables:
            try:
                with connection.transaction():
                    rows = connection.execute(sql.SQL("SELECT count(*) FROM {}").format(
                        sql.Identifier(schema, table))).fetchone()[0]
            except psycopg.errors.InsufficientPrivilege as exc:
                raise Conflict(f"Table {schema}.{table} has row-level security policies; TAM does not run them "
                               "as the admin role, drop the policies to back up or restore") from exc
            counts.append(TableCount(schema_name=schema, table=table, rows=rows))
    return counts


def _roles_in_use(connection: psycopg.Connection, roles: tuple[str, ...]) -> list[str]:
    """Workspace roles of the backup that already own objects in another database of this cluster.

    Roles are cluster-global and named after the installation id: restoring next to the original
    database would hand both databases to the same roles."""
    rows = connection.execute("""
        SELECT DISTINCT r.rolname FROM pg_roles r
        JOIN pg_shdepend d ON d.refclassid = 'pg_authid'::regclass AND d.refobjid = r.oid
        WHERE r.rolname = ANY(%s) AND d.dbid NOT IN (0, (SELECT oid FROM pg_database
                                                         WHERE datname = current_database()))
        ORDER BY 1""", (list(roles),)).fetchall()
    return [row[0] for row in rows]


def _workspaces(connection: psycopg.Connection) -> tuple[list[str], list[str], str | None]:
    """Workspace keys and roles from tam_control.workspace_schemas, and the installation's instance_id."""
    exists = connection.execute("SELECT to_regclass(%s)", (f"{CONTROL_SCHEMA}.{WORKSPACE_MAP_TABLE}",)).fetchone()[0]
    if exists is None:
        return [], [], None
    rows = connection.execute(sql.SQL("SELECT key, role FROM {} ORDER BY key").format(
        sql.Identifier(CONTROL_SCHEMA, WORKSPACE_MAP_TABLE))).fetchall()
    meta = connection.execute("SELECT to_regclass(%s)", (f"{CONTROL_SCHEMA}.meta",)).fetchone()[0]
    instance_id = None
    if meta is not None:
        row = connection.execute(sql.SQL("SELECT value FROM {} WHERE key = 'instance_id'").format(
            sql.Identifier(CONTROL_SCHEMA, "meta"))).fetchone()
        instance_id = row[0] if row else None
    return [row[0] for row in rows], [row[1] for row in rows], instance_id


def _server_major(connection: psycopg.Connection) -> tuple[int, int]:
    number = int(connection.execute("SHOW server_version_num").fetchone()[0])
    return number, number // SERVER_VERSION_DIVISOR


def backup(dsn: DatabaseDsn, destination: Path, environ: Mapping[str, str] | None = None) -> PostgresSnapshot:
    """Dump the TAM schemas of ``dsn`` into a new directory ``destination`` (mode 0700)."""
    environ = os.environ if environ is None else environ
    pg_dump = _tool(PG_DUMP, PG_DUMP_ENV, environ)
    destination = destination.resolve()
    if destination.exists():
        raise FileExistsError(destination)
    timeout = _timeout(environ)
    with psycopg.connect(dsn.to_uri(), autocommit=True) as connection:
        server_version, major = _server_major(connection)
        if _tool_major(pg_dump) < major:
            raise Conflict(f"pg_dump is older than the server (PostgreSQL {major}); install a matching client")
        with connection.transaction():
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            snapshot = connection.execute("SELECT pg_export_snapshot()").fetchone()[0]
            schemas = _tam_schemas(connection)
            if CONTROL_SCHEMA not in schemas:
                raise Conflict("The database holds no TAM installation to back up")
            counts = _counts(connection, schemas)
            keys, roles, instance_id = _workspaces(connection)
            destination.mkdir(parents=True, mode=0o700)
            dump = destination / DUMP_FILE
            command = [pg_dump, "--format=custom", "--no-owner", "--no-privileges", f"--snapshot={snapshot}",
                       f"--file={dump}", f"--dbname={_uri_without_password(dsn)}"]
            for schema in FIXED_SCHEMAS:
                command.append(f"--schema={schema}")
            command.append(f"--schema={WORKSPACE_SCHEMA_GLOB}")
            try:
                with _client_environment(dsn, environ) as env:
                    _run(command, env, timeout, "pg_dump")
            except BaseException:
                shutil.rmtree(destination, ignore_errors=True)
                raise
    dump.chmod(0o600)
    manifest = PostgresSnapshot(
        package_version=VERSION, created_at=datetime.now(UTC).isoformat(), server_version_num=server_version,
        instance_id=instance_id, dump=DumpFile(sha256=digest(dump), bytes=dump.stat().st_size),
        schemas=tuple(schemas), roles=tuple(roles), workspaces=tuple(keys), counts=tuple(counts))
    manifest_path = destination / MANIFEST_FILE
    manifest_path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    manifest_path.chmod(0o600)
    LOGGER.info(json.dumps({"event": "postgres_backup_created", "schemas": len(schemas), "workspaces": len(keys),
                            "bytes": manifest.dump.bytes}))
    return manifest


def read_manifest(snapshot: Path) -> PostgresSnapshot:
    path = snapshot / MANIFEST_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Conflict("The backup has no readable manifest.json") from exc
    if raw.get("format_version") != BACKUP_FORMAT_VERSION or raw.get("backend") != POSTGRES_BACKEND:
        raise Conflict("This is not a PostgreSQL backup (manifest format_version 2)")
    return PostgresSnapshot.model_validate(raw)


def is_postgres_backup(snapshot: Path) -> bool:
    try:
        raw = json.loads((snapshot / MANIFEST_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(raw, dict) and raw.get("backend") == POSTGRES_BACKEND


def verify(snapshot: Path, environ: Mapping[str, str] | None = None) -> PostgresSnapshot:
    """Checksum and ``pg_restore --list``: the archive is complete and readable."""
    environ = os.environ if environ is None else environ
    snapshot = snapshot.resolve()
    manifest = read_manifest(snapshot)
    dump = snapshot / manifest.dump.path
    if dump.is_symlink() or snapshot not in dump.resolve().parents or not dump.is_file():
        raise Conflict("Backup dump path validation failed")
    if dump.stat().st_size != manifest.dump.bytes or digest(dump) != manifest.dump.sha256:
        raise Conflict("Backup dump checksum mismatch")
    pg_restore = _tool(PG_RESTORE, PG_RESTORE_ENV, environ)
    listing = _run([pg_restore, "--list", str(dump)], dict(environ), _timeout(environ), "pg_restore --list").stdout
    for schema in manifest.schemas:
        if f" SCHEMA - {schema} " not in listing:
            raise Conflict(f"Backup dump does not contain schema {schema}")
    return manifest


def _require_empty(connection: psycopg.Connection) -> None:
    present = _tam_schemas(connection)
    if present:
        raise Conflict("Restore needs an empty database; it already holds TAM schemas "
                       f"({', '.join(present[:3])}{', ...' if len(present) > 3 else ''})")


def restore(snapshot: Path, dsn: DatabaseDsn, provisioner: ProvisionerFactory,
            environ: Mapping[str, str] | None = None) -> PostgresSnapshot:
    """Restore a PostgreSQL backup into the empty database ``dsn``, then re-provision workspace roles.

    ``provisioner`` builds the target's WorkspaceProvisioner once the control schemas exist; its
    ``ensure`` recreates each workspace role, its password, grants and search_path.
    """
    environ = os.environ if environ is None else environ
    manifest = verify(snapshot, environ)
    pg_restore = _tool(PG_RESTORE, PG_RESTORE_ENV, environ)
    with psycopg.connect(dsn.to_uri(), autocommit=True) as connection:
        _, major = _server_major(connection)
        if _tool_major(pg_restore) < major:
            raise Conflict(f"pg_restore is older than the server (PostgreSQL {major}); install a matching client")
        _require_empty(connection)
        shared = _roles_in_use(connection, manifest.roles)
        if shared:
            raise Conflict(f"{len(shared)} workspace roles of this backup are in use by another database of this "
                           "PostgreSQL cluster (the original installation); restore into a separate cluster, or "
                           "drop the original database and its roles first")
    command = [pg_restore, "--no-owner", "--no-privileges", "--exit-on-error", "--single-transaction",
               f"--dbname={_uri_without_password(dsn)}", str(snapshot.resolve() / manifest.dump.path)]
    with _client_environment(dsn, environ) as env:
        _run(command, env, _timeout(environ), "pg_restore")
    with psycopg.connect(dsn.to_uri(), autocommit=True) as connection:
        actual = {(item.schema_name, item.table): item.rows for item in _counts(connection, list(manifest.schemas))}
        expected = {(item.schema_name, item.table): item.rows for item in manifest.counts}
        if actual != expected:
            wrong = sorted(f"{schema}.{table}" for schema, table in set(actual) | set(expected)
                           if actual.get((schema, table)) != expected.get((schema, table)))
            raise Conflict("Restored row counts differ from the backup manifest: " + ", ".join(wrong[:5]))
        connection.execute("ANALYZE")
    if manifest.instance_id is not None:
        # Re-creates tam_compat if the dump's ledger lists it and re-grants USAGE on the shared schemas.
        from team_memory.pg_provision import PgProvisioner

        if PgProvisioner(dsn.to_uri()).bootstrap(manifest.instance_id) != manifest.instance_id:
            raise Conflict("The restored database names another installation id than the backup")
    workspaces = provisioner()
    for key in manifest.workspaces:
        workspaces.ensure(key)
    LOGGER.info(json.dumps({"event": "postgres_backup_restored", "schemas": len(manifest.schemas),
                            "workspaces": len(manifest.workspaces)}))
    return manifest

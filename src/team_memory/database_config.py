"""Which database the team server uses, and the guard that refuses a wrong one (plan 4.1-4.3, 4.6-4.8).

``<root>/database.json`` (mode 0600, atomic replace) is written from the dashboard and wins over
``TAM_TEAM_DATABASE_URL``, which wins over the SQLite default. The DSN inside is one Fernet token
under the same master key as provider secrets. The split-brain guard compares installation ids
before anything is served: a DSN of another installation, an empty PostgreSQL next to SQLite data,
or an undecryptable DSN stop the server instead of falling back silently.
"""
import json
import logging
import os
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from cryptography.fernet import InvalidToken
from pydantic import ValidationError

from paths import exposed_to_others
from tam_db.contracts import ActiveDatabase, Backend, ControlKind, DatabaseSettings
from team_memory.contracts import Conflict
from team_memory.database import (
    SwitchableControlPlane,
    sqlite_has_data,
    sqlite_instance_id,
)
from team_memory.database_contracts import (
    ARCHIVE_DIR,
    DATABASE_CONFIG_FILE,
    DATABASE_URL_ENV,
    POSTGRES_MARKER_FILE,
    CheckId,
    CheckReport,
    CheckStatus,
    ConfigSource,
    DatabaseChecker,
    DatabaseConfig,
    DatabaseConfigSnapshot,
    DatabaseConfigView,
    DatabaseDsn,
    DatabaseStartupRefused,
    DsnOrigin,
    EffectiveDatabase,
    InvalidDsn,
    MigrationRunner,
    PostgresMarker,
    SqliteArchive,
    StartupRefusal,
    TargetState,
)
from team_memory.pg_provision import empty_passfile
from team_memory.settings import load_cipher, load_master_key

LOGGER = logging.getLogger(__name__)
TEMP_SUFFIX = ".tmp"
AUDIT_SUBJECT = "database"
SYSTEM_ACTOR = "system"

DATABASE_TESTED = "database_tested"
DATABASE_REPOINTED = "database_repointed"
DATABASE_ACTIVATED = "database_activated"
DATABASE_ROLLED_BACK = "database_rolled_back"
DATABASE_MIGRATION_STARTED = "database_migration_started"
DATABASE_MIGRATION_FINISHED = "database_migration_finished"
DATABASE_MIGRATION_FAILED = "database_migration_failed"
DATABASE_EVENTS = (DATABASE_TESTED, DATABASE_REPOINTED, DATABASE_ACTIVATED, DATABASE_ROLLED_BACK,
                   DATABASE_MIGRATION_STARTED, DATABASE_MIGRATION_FINISHED, DATABASE_MIGRATION_FAILED)


def _environ(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


class FileDatabaseConfigStore:
    """DatabaseConfigStore (database_contracts) over ``<root>/database.json``."""

    def __init__(self, root: Path, environ: Mapping[str, str] | None = None):
        self.root = root.resolve()
        self.path = self.root / DATABASE_CONFIG_FILE
        self.environ = _environ(environ)

    def load(self) -> DatabaseConfig | None:
        if not self.path.is_file():
            return None
        if exposed_to_others(self.path):
            raise DatabaseStartupRefused(StartupRefusal.CONFIG_UNREADABLE,
                                         f"{DATABASE_CONFIG_FILE} must not be readable by group or others (chmod 600)")
        try:
            return DatabaseConfig.model_validate_json(self.path.read_bytes())
        except (OSError, ValidationError, ValueError) as exc:
            raise DatabaseStartupRefused(StartupRefusal.CONFIG_UNREADABLE,
                                         f"{DATABASE_CONFIG_FILE} is unreadable or invalid; restore it from a backup") from exc

    def save(self, config: DatabaseConfig) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_name(f".{DATABASE_CONFIG_FILE}.{uuid.uuid4().hex}{TEMP_SUFFIX}")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(config.model_dump_json(indent=2))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        self._sync_directory()

    def _sync_directory(self) -> None:
        if not hasattr(os, "O_DIRECTORY"):
            return
        descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def seal(self, dsn: DatabaseDsn) -> str:
        return load_cipher(self.root, self.environ).encrypt(dsn.to_uri().encode("utf-8")).decode("ascii")

    def unseal(self, config: DatabaseConfigSnapshot) -> DatabaseDsn | None:
        if config.dsn_token is None:
            return None
        try:
            cipher = load_cipher(self.root, self.environ, create=False)
            plain = cipher.decrypt(config.dsn_token.encode("ascii")).decode("utf-8")
        except (FileNotFoundError, PermissionError, ValueError, InvalidToken) as exc:
            raise DatabaseStartupRefused(
                StartupRefusal.KEY_UNAVAILABLE,
                "The database DSN cannot be decrypted: the master key (master.key or TAM_TEAM_MASTER_KEY) "
                "is missing or different; restore it from the key backup") from exc
        try:
            # Validated when it was saved; web-origin DSNs are held to the web rules again.
            return DatabaseDsn.parse(plain, DsnOrigin.ENV).validate_for(config.origin)
        except InvalidDsn as exc:
            raise DatabaseStartupRefused(StartupRefusal.CONFIG_UNREADABLE,
                                         f"The stored database DSN is invalid: {exc}") from exc

    def effective(self) -> EffectiveDatabase:
        config = self.load()
        if config is not None:
            return EffectiveDatabase(backend=config.backend, source=ConfigSource.WEB, dsn=self.unseal(config),
                                     instance_id=config.instance_id, generation=config.generation,
                                     origin=config.origin if config.backend is Backend.POSTGRES else DsnOrigin.ENV)
        raw = self.environ.get(DATABASE_URL_ENV, "").strip()
        if raw:
            try:
                dsn = DatabaseDsn.parse(raw, DsnOrigin.ENV)
            except InvalidDsn as exc:
                raise DatabaseStartupRefused(StartupRefusal.CONFIG_UNREADABLE,
                                             f"{DATABASE_URL_ENV} is not a usable DSN: {exc}") from exc
            return EffectiveDatabase(backend=Backend.POSTGRES, source=ConfigSource.ENV, dsn=dsn)
        return EffectiveDatabase(backend=Backend.SQLITE, source=ConfigSource.DEFAULT)


def checker_for(origin: DsnOrigin) -> DatabaseChecker:
    """A DSN typed into the web UI is checked in an isolated process (no PG* env, no ~/.pgpass, empty
    passfile); the operator's own DSN (env, CLI) keeps normal libpq behaviour (sockets, cert files)."""
    from team_memory.db_check import PgDatabaseChecker

    return PgDatabaseChecker(isolated=origin is DsnOrigin.WEB)


def _require_prerequisites(report: CheckReport) -> None:
    from team_memory.pg_provision import PrerequisitesMissing

    failed = [check for check in report.checks
              if check.id is not CheckId.TARGET_STATE and check.status is CheckStatus.FAILED]
    if failed:
        raise PrerequisitesMissing("PostgreSQL is not ready for TAM: " + " ".join(check.message for check in failed),
                                   report=report)


def resolve_instance(root: Path, effective: EffectiveDatabase, found: UUID | None, state: TargetState) -> UUID:
    """The split-brain guard (plan 4.3) for a PostgreSQL target; returns the installation id to use."""
    local_text = sqlite_instance_id(root, create=False)
    local = UUID(local_text) if local_text else None
    if state is TargetState.UNKNOWN:
        raise DatabaseStartupRefused(StartupRefusal.FOREIGN_INSTALLATION,
                                     "The PostgreSQL database holds TAM control data without a readable "
                                     "installation id; refusing to use it")
    if found is not None:
        if effective.instance_id is not None and effective.instance_id != found:
            raise DatabaseStartupRefused(StartupRefusal.FOREIGN_INSTALLATION,
                                         "The configured PostgreSQL database belongs to another TAM installation")
        if local is not None and local != found and sqlite_has_data(root):
            raise DatabaseStartupRefused(StartupRefusal.FOREIGN_INSTALLATION,
                                         "The PostgreSQL database belongs to another TAM installation than the "
                                         "SQLite data in this directory; check " + DATABASE_URL_ENV)
        return found
    if effective.instance_id is not None:
        raise DatabaseStartupRefused(StartupRefusal.FOREIGN_INSTALLATION,
                                     "The configured PostgreSQL database holds no TAM installation, but this "
                                     "installation was moved there; check the DSN")
    if sqlite_has_data(root):
        raise DatabaseStartupRefused(StartupRefusal.EMPTY_TARGET_WITH_SQLITE_DATA,
                                     "The PostgreSQL database is empty but this directory holds SQLite data; "
                                     "migrate it from the dashboard (Settings, Database) or with tam-team db-migrate, "
                                     f"or unset {DATABASE_URL_ENV} to keep using SQLite")
    return local or uuid.uuid4()


def read_postgres_marker(root: Path) -> PostgresMarker | None:
    path = root / POSTGRES_MARKER_FILE
    if not path.exists():
        return None
    try:
        return PostgresMarker.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise DatabaseStartupRefused(StartupRefusal.CONFIG_UNREADABLE,
                                     f"{POSTGRES_MARKER_FILE} is unreadable: {exc}") from exc


def write_postgres_marker(root: Path, marker: PostgresMarker) -> None:
    """Atomic 0600 write, skipped when the stored marker is already this one."""
    path = root / POSTGRES_MARKER_FILE
    if path.exists() and read_postgres_marker(root) == marker:
        return
    temporary = path.with_name(f".{POSTGRES_MARKER_FILE}.{uuid.uuid4().hex}{TEMP_SUFFIX}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(marker.model_dump_json(indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _check_postgres_marker(root: Path, source: ConfigSource) -> None:
    """A SQLite start in a directory that last ran on PostgreSQL: refused when nothing chose SQLite
    (a forgotten DSN), accepted and unmarked when database.json did (a rollback)."""
    marker = read_postgres_marker(root)
    if marker is None:
        return
    if source is ConfigSource.DEFAULT:
        raise DatabaseStartupRefused(
            StartupRefusal.POSTGRES_DSN_MISSING,
            f"This directory belongs to a TAM installation on PostgreSQL ({marker.target}); set "
            f"{DATABASE_URL_ENV} to its DSN. Starting without it would open a new, empty SQLite installation.")
    (root / POSTGRES_MARKER_FILE).unlink(missing_ok=True)


def open_control_plane(root: Path, environ: Mapping[str, str] | None = None,
                       checker: DatabaseChecker | None = None) -> SwitchableControlPlane:
    """Resolve the effective database, run the guard and the startup checks, provision the control
    schemas on PostgreSQL, and return the control plane. Raises DatabaseStartupRefused,
    PrerequisitesMissing or Unavailable instead of falling back."""
    root = root.resolve()
    env = _environ(environ)
    effective = FileDatabaseConfigStore(root, env).effective()
    settings = DatabaseSettings.from_environ(env)
    if effective.backend is Backend.SQLITE:
        _check_postgres_marker(root, effective.source)
        local = UUID(sqlite_instance_id(root, create=True))
        return SwitchableControlPlane(root, effective.active(local, settings), master_key=lambda: load_master_key(root, env))
    from team_memory.pg_provision import PgProvisioner

    report = (checker or checker_for(effective.origin)).check(effective.dsn, instance_id=effective.instance_id)
    _require_prerequisites(report)
    instance = resolve_instance(root, effective, report.target_instance_id, report.target_state)
    passfile = empty_passfile(root) if effective.origin is DsnOrigin.WEB else None
    target = effective.active(instance, settings, passfile)
    stored = PgProvisioner(target.url, settings, connect_overrides=target.connect_kwargs()).bootstrap(str(instance))
    if stored != str(instance):
        raise DatabaseStartupRefused(StartupRefusal.FOREIGN_INSTALLATION,
                                     "Another TAM installation claimed the PostgreSQL database meanwhile")
    write_postgres_marker(root, PostgresMarker(instance_id=instance, target=effective.dsn.host_db()))
    LOGGER.info(json.dumps({"event": "database_selected", "backend": target.backend.value,
                            "source": effective.source.value, "target": effective.dsn.host_db(),
                            "generation": target.generation}))
    return SwitchableControlPlane(root, target, master_key=lambda: load_master_key(root, env))


def record_database_event(plane: SwitchableControlPlane, action: str, actor: str,
                          dsn: DatabaseDsn | None = None, **fields: str | int | bool | None) -> None:
    """Audit a database operation in admin_events of the active control plane plus a JSON log line.

    Only host, database and sslmode of a DSN are recorded (plan 4.8)."""
    if action not in DATABASE_EVENTS:
        raise ValueError(f"unknown database audit action {action!r}")
    detail = {**(dsn.audit().model_dump(mode="json") if dsn is not None else {}), **fields}
    text = json.dumps(detail, sort_keys=True)
    with plane.connect(ControlKind.IDENTITY, write=True) as db:
        db.execute("INSERT INTO admin_events(action,subject,actor,detail) VALUES (?,?,?,?)",
                   (action, AUDIT_SUBJECT, actor, text))
    LOGGER.info(json.dumps({"event": action, "actor": actor, **detail}))


def archive_info(root: Path, config: DatabaseConfig | None) -> SqliteArchive | None:
    if config is None or config.archive is None:
        return None
    path = root / config.archive
    if not path.is_dir() or not config.archive.startswith(ARCHIVE_DIR + "/"):
        return None
    files = [item for item in path.rglob("*") if item.is_file()]
    created = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    return SqliteArchive(path=config.archive, created_at=created, bytes=sum(item.stat().st_size for item in files))


class DatabaseSettingsService:
    """DatabaseConfigService (database_contracts): the dashboard's view, Test button and repoint.

    ``on_activated`` runs after the plane switched (WorkerPool.recycle, plan 4.4)."""

    def __init__(self, root: Path, plane: SwitchableControlPlane, store: FileDatabaseConfigStore | None = None,
                 checker: DatabaseChecker | None = None, on_activated: Callable[[], object] | None = None,
                 lease_handover: Callable[[ActiveDatabase], None] | None = None,
                 migration: MigrationRunner | None = None, environ: Mapping[str, str] | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self.root = root.resolve()
        self.plane = plane
        self.environ = _environ(environ)
        self.store = store or FileDatabaseConfigStore(self.root, self.environ)
        self.checker = checker or checker_for(DsnOrigin.WEB)
        self.on_activated = on_activated
        # Moves this server's advisory lease to the new DSN before the switch (plan 1.5).
        self.lease_handover = lease_handover
        self.migration = migration
        self.clock = clock
        self.last_check: CheckReport | None = None

    def view(self) -> DatabaseConfigView:
        current = self.plane.current()
        effective = self.store.effective()
        config = self.store.load()
        dsn = effective.dsn
        archive = archive_info(self.root, config)
        return DatabaseConfigView(
            backend=current.backend, source=effective.source,
            dsn_masked=dsn.masked() if dsn else None, host_db=dsn.host_db() if dsn else None,
            sslmode=dsn.effective_sslmode if dsn else None, warnings=dsn.warnings() if dsn else (),
            instance_id=UUID(current.instance_id), generation=current.generation,
            updated_at=config.updated_at if config else None, updated_by=config.updated_by if config else None,
            env_configured=bool(self.environ.get(DATABASE_URL_ENV, "").strip()), last_check=self.last_check,
            migration=self.migration.progress() if self.migration is not None else None, archive=archive,
            rollback_available=bool(archive and config and config.previous is not None
                                    and config.previous.backend is Backend.SQLITE))

    def test(self, dsn: DatabaseDsn, actor: str) -> CheckReport:
        dsn.validate_for(DsnOrigin.WEB)
        report = self.checker.check(dsn, instance_id=UUID(self.plane.current().instance_id))
        self.last_check = report
        record_database_event(self.plane, DATABASE_TESTED, actor, dsn, ok=report.ok,
                              target_state=report.target_state.value)
        return report

    def repoint(self, dsn: DatabaseDsn, actor: str) -> DatabaseConfigView:
        """Same installation, new connection (password rotation, new host); no data is copied."""
        dsn.validate_for(DsnOrigin.WEB)
        current = self.plane.current()
        if current.backend is not Backend.POSTGRES:
            raise Conflict("Repoint changes the connection of a PostgreSQL installation; migrate to PostgreSQL first")
        report = self.checker.check(dsn, instance_id=UUID(current.instance_id))
        self.last_check = report
        if report.target_state is not TargetState.SAME_INSTALLATION:
            raise Conflict("The new DSN does not reach this installation's database")
        if not report.ok:
            raise Conflict("The new DSN fails the checks: " +
                           " ".join(check.message for check in report.checks if check.status is CheckStatus.FAILED))
        config = self.store.load()
        generation = max(current.generation, config.generation if config else 0) + 1
        # Repoint keeps the rollback anchor (the SQLite config before migration) instead of
        # chaining the replaced PostgreSQL config.
        anchor = config.previous if config is not None and config.previous is not None else (
            config.snapshot() if config is not None else None)
        updated = DatabaseConfig(backend=Backend.POSTGRES, dsn_token=self.store.seal(dsn),
                                 instance_id=UUID(current.instance_id), generation=generation,
                                 updated_at=self.clock(), updated_by=actor,
                                 archive=config.archive if config is not None else None, previous=anchor,
                                 origin=DsnOrigin.WEB)
        overrides = dsn.connect_overrides(DsnOrigin.WEB, empty_passfile(self.root))
        target = ActiveDatabase(backend=Backend.POSTGRES, instance_id=current.instance_id, generation=generation,
                                url=dsn.to_uri(), settings=current.settings,
                                connect_options=tuple(sorted(overrides.items())))
        if self.lease_handover is not None:
            self.lease_handover(target)
        self.store.save(updated)
        try:
            self.plane.activate(target)
        except BaseException:
            self._restore(config)
            if self.lease_handover is not None:
                self.lease_handover(current)
            raise
        if self.on_activated is not None:
            self.on_activated()
        record_database_event(self.plane, DATABASE_REPOINTED, actor, dsn, generation=generation)
        return self.view()

    def _restore(self, config: DatabaseConfig | None) -> None:
        if config is None:
            self.store.path.unlink(missing_ok=True)
        else:
            self.store.save(config)

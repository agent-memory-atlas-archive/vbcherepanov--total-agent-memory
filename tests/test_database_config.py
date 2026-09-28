"""database.json, DSN precedence, the split-brain guard (plan 4.1-4.3, 4.6, 4.8) and the control plane."""
import json
import os
import sqlite3
import stat
import threading
import time
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tam_db.contracts import (
    ActiveDatabase,
    Backend,
    ControlKind,
    PgSerializationFailure,
)
from team_memory.contracts import Conflict, Unauthorized
from team_memory.database import (
    ReadWriteLock,
    SwitchableControlPlane,
    retrying,
    serializable,
    sqlite_instance_id,
)
from team_memory.database_config import (
    DATABASE_REPOINTED,
    DATABASE_TESTED,
    DatabaseSettingsService,
    FileDatabaseConfigStore,
    checker_for,
    open_control_plane,
    record_database_event,
)
from team_memory.database_contracts import (
    DATABASE_CONFIG_FILE,
    DATABASE_URL_ENV,
    POSTGRES_MARKER_FILE,
    CheckId,
    CheckReport,
    CheckStatus,
    ConfigSource,
    DatabaseCheck,
    DatabaseConfig,
    DatabaseDsn,
    DatabaseStartupRefused,
    DsnOrigin,
    InvalidDsn,
    PostgresMarker,
    StartupRefusal,
    TargetState,
)
from team_memory.pg_provision import PgProvisioner, PrerequisitesMissing, server_lease
from team_memory.registry import Registry
from team_memory.settings import MASTER_KEY_FILE
from tests.team_db_helpers import pg_admin

PASSWORD = "S3cret-" + uuid.uuid4().hex
REMOTE_DSN = f"postgresql://tam:{PASSWORD}@db.internal:5432/tam?sslmode=require"
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
RACE_WAIT_SECONDS = 0.5


def web_config(store: FileDatabaseConfigStore, dsn: DatabaseDsn | None, instance_id: uuid.UUID,
               generation: int = 1) -> DatabaseConfig:
    return DatabaseConfig(backend=Backend.POSTGRES if dsn else Backend.SQLITE,
                          dsn_token=store.seal(dsn) if dsn else None, instance_id=instance_id,
                          generation=generation, updated_at=NOW, updated_by="root")


def refusal(call) -> StartupRefusal:
    with pytest.raises(DatabaseStartupRefused) as caught:
        call()
    assert PASSWORD not in str(caught.value)
    return caught.value.reason


# database.json


def test_config_is_encrypted_atomic_and_private(tmp_path):
    store = FileDatabaseConfigStore(tmp_path, {})
    config = web_config(store, DatabaseDsn.parse(REMOTE_DSN), uuid.uuid4())
    store.save(config)
    raw = (tmp_path / DATABASE_CONFIG_FILE).read_bytes()
    assert PASSWORD.encode() not in raw and b"db.internal" not in raw
    assert oct((tmp_path / DATABASE_CONFIG_FILE).stat().st_mode & 0o777) == "0o600"
    assert [path.name for path in tmp_path.iterdir() if path.name.startswith(".")] == []
    assert store.load() == config
    assert store.unseal(config).to_uri() == DatabaseDsn.parse(REMOTE_DSN).to_uri()
    store.save(config.model_copy(update={"generation": 2}))
    assert store.load().generation == 2


def test_group_readable_or_corrupt_config_is_refused(tmp_path):
    store = FileDatabaseConfigStore(tmp_path, {})
    store.save(web_config(store, None, uuid.uuid4()))
    (tmp_path / DATABASE_CONFIG_FILE).chmod(0o644)
    assert refusal(store.load) is StartupRefusal.CONFIG_UNREADABLE
    (tmp_path / DATABASE_CONFIG_FILE).chmod(0o600)
    (tmp_path / DATABASE_CONFIG_FILE).write_text("{not json")
    assert refusal(store.load) is StartupRefusal.CONFIG_UNREADABLE


def test_lost_master_key_is_refused_without_creating_a_new_one(tmp_path):
    store = FileDatabaseConfigStore(tmp_path, {DATABASE_URL_ENV: "postgresql://other@elsewhere/tam"})
    store.save(web_config(store, DatabaseDsn.parse(REMOTE_DSN), uuid.uuid4()))
    (tmp_path / MASTER_KEY_FILE).unlink()
    assert refusal(store.effective) is StartupRefusal.KEY_UNAVAILABLE
    assert not (tmp_path / MASTER_KEY_FILE).exists()


def test_different_master_key_is_refused(tmp_path):
    store = FileDatabaseConfigStore(tmp_path, {})
    store.save(web_config(store, DatabaseDsn.parse(REMOTE_DSN), uuid.uuid4()))
    (tmp_path / MASTER_KEY_FILE).unlink()
    FileDatabaseConfigStore(tmp_path, {}).seal(DatabaseDsn.parse(REMOTE_DSN))
    assert refusal(store.effective) is StartupRefusal.KEY_UNAVAILABLE


def test_precedence_web_over_env_over_default(tmp_path):
    env = {DATABASE_URL_ENV: REMOTE_DSN}
    assert FileDatabaseConfigStore(tmp_path, {}).effective().source is ConfigSource.DEFAULT
    from_env = FileDatabaseConfigStore(tmp_path, env).effective()
    assert (from_env.source, from_env.backend, from_env.instance_id) == (ConfigSource.ENV, Backend.POSTGRES, None)
    store = FileDatabaseConfigStore(tmp_path, env)
    instance = uuid.uuid4()
    store.save(web_config(store, None, instance))
    from_web = store.effective()
    assert (from_web.source, from_web.backend, from_web.instance_id) == (ConfigSource.WEB, Backend.SQLITE, instance)


@pytest.mark.parametrize("raw", [
    REMOTE_DSN + "&options=-csearch_path%3Dws_other",
    f"host=db.internal user=tam password={PASSWORD}",
    f"mysql://tam:{PASSWORD}@db/tam",
])
def test_unusable_env_dsn_is_refused_without_echoing_it(tmp_path, raw):
    assert refusal(FileDatabaseConfigStore(tmp_path, {DATABASE_URL_ENV: raw}).effective) is StartupRefusal.CONFIG_UNREADABLE


# Guard on SQLite


def test_sqlite_default_keeps_one_instance_id(tmp_path):
    plane = open_control_plane(tmp_path, {})
    assert plane.backend is Backend.SQLITE and plane.current().generation == 0
    assert plane.current().instance_id == sqlite_instance_id(tmp_path, create=False)
    assert open_control_plane(tmp_path, {}).current().instance_id == plane.current().instance_id
    assert oct((tmp_path / "identity.db").stat().st_mode & 0o777) == "0o600"


def test_web_sqlite_config_of_another_installation_is_refused(tmp_path):
    open_control_plane(tmp_path, {})
    store = FileDatabaseConfigStore(tmp_path, {})
    store.save(web_config(store, None, uuid.uuid4()))
    assert refusal(lambda: open_control_plane(tmp_path, {})) is StartupRefusal.FOREIGN_INSTALLATION


def test_registry_keeps_legacy_identity_and_adds_instance_meta(tmp_path, monkeypatch):
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    registry = Registry(tmp_path)
    registry.add_user("root", "Root")
    with closing(sqlite3.connect(tmp_path / "identity.db")) as db:
        assert db.execute("SELECT value FROM meta WHERE key='instance_id'").fetchone()[0] == \
            registry.plane.current().instance_id


# Control plane


def test_keep_superadmin_is_race_free_on_sqlite(tmp_path, monkeypatch):
    """Regression: count-then-update in a deferred transaction let two concurrent demotions both see
    another superadmin; BEGIN IMMEDIATE serializes them so exactly one succeeds."""
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    registry = Registry(tmp_path)
    for user_id in ("alice", "bob"):
        registry.add_user(user_id, user_id.title())
        registry.set_org_role(user_id, "superadmin")
    barrier = threading.Barrier(2)
    original = Registry._keep_superadmin

    def slow_check(db, user_id):
        original(db, user_id)
        try:
            barrier.wait(RACE_WAIT_SECONDS)
        except threading.BrokenBarrierError:
            pass

    monkeypatch.setattr(Registry, "_keep_superadmin", staticmethod(slow_check))
    outcomes = {}

    def demote(user_id):
        try:
            registry.set_org_role(user_id, "member")
            outcomes[user_id] = "ok"
        except Conflict:
            outcomes[user_id] = "conflict"
        except sqlite3.OperationalError as exc:
            outcomes[user_id] = f"error: {exc}"

    threads = [threading.Thread(target=demote, args=(user_id,)) for user_id in ("alice", "bob")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes.values()) == ["conflict", "ok"], outcomes
    assert registry.has_superadmin()
    assert sum(user["org_role"] == "superadmin" for user in registry.list_users()) == 1


def test_disable_keeps_last_superadmin_under_concurrency(tmp_path, monkeypatch):
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    registry = Registry(tmp_path)
    for user_id in ("alice", "bob"):
        registry.add_user(user_id, user_id.title())
        registry.set_org_role(user_id, "superadmin")
    errors = []

    def disable(user_id):
        try:
            registry.set_active(user_id, False)
        except Conflict as exc:
            errors.append(str(exc))

    threads = [threading.Thread(target=disable, args=(user_id,)) for user_id in ("alice", "bob")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert registry.has_superadmin() and len(errors) == 1


def test_read_write_lock_waits_for_readers_and_is_reentrant():
    lock = ReadWriteLock()
    order = []

    def write():
        with lock.write():
            order.append("write")

    writer = threading.Thread(target=write)
    with lock.read(), lock.read():
        writer.start()
        time.sleep(0.05)
        order.append("read")
        with pytest.raises(RuntimeError), lock.write():
            order.append("nested write")
    writer.join()
    assert order == ["read", "write"]


def test_retry_on_serialization_failure_then_conflict():
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise PgSerializationFailure("could not serialize", sqlstate="40001")
        return "done"

    assert retrying(3, flaky) == "done" and len(calls) == 3
    calls.clear()
    with pytest.raises(Conflict):
        retrying(2, flaky)
    assert len(calls) == 2


def test_serializable_decorator_uses_the_plane_attempts(tmp_path):
    class Repository:
        def __init__(self, plane):
            self.plane, self.calls = plane, 0

        @serializable
        def work(self):
            self.calls += 1
            if self.calls == 1:
                raise PgSerializationFailure("retry me", sqlstate="40001")
            return self.calls

    assert Repository(SwitchableControlPlane.sqlite(tmp_path)).work() == 2


def test_activate_refuses_another_installation(tmp_path):
    plane = SwitchableControlPlane.sqlite(tmp_path)
    other = ActiveDatabase(backend=Backend.SQLITE, instance_id=str(uuid.uuid4()), generation=1)
    with pytest.raises(Conflict):
        plane.activate(other)
    same = ActiveDatabase(backend=Backend.SQLITE, instance_id=plane.current().instance_id, generation=3)
    plane.activate(same)
    assert plane.current().generation == 3


def test_sqlite_workspaces_and_readers(tmp_path):
    plane = SwitchableControlPlane.sqlite(tmp_path)
    assert not plane.workspaces.exists("shared")
    with plane.workspace_reader("shared") as db:
        assert db is None
    plane.workspaces.ensure("shared")
    with closing(sqlite3.connect(tmp_path / "workspaces" / "shared" / "memory.db")) as db:
        db.execute("CREATE TABLE knowledge (id INTEGER PRIMARY KEY)")
    with plane.workspace_reader("shared") as db:
        assert db.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0] == 0
        with pytest.raises(sqlite3.OperationalError):
            db.execute("INSERT INTO knowledge VALUES (1)")
    plane.workspaces.drop("shared")
    assert not plane.workspaces.exists("shared")


# Audit and dashboard service


class FakeChecker:
    def __init__(self, state=TargetState.SAME_INSTALLATION, ok=True):
        self.state, self.ok, self.calls = state, ok, []

    def check(self, dsn, *, instance_id):
        self.calls.append(instance_id)
        status = CheckStatus.PASSED if self.ok else CheckStatus.FAILED
        checks = tuple(DatabaseCheck(id=check_id, status=status if check_id is CheckId.CONNECT else CheckStatus.PASSED,
                                     message="fake") for check_id in CheckId)
        found = instance_id if self.state is TargetState.SAME_INSTALLATION else None
        return CheckReport(dsn_masked=dsn.masked(), checks=checks, target_state=self.state, target_instance_id=found,
                           checked_at=NOW, duration_ms=1)


def events(plane, action):
    with plane.connect(ControlKind.IDENTITY) as db:
        return [dict(row) for row in db.execute("SELECT * FROM admin_events WHERE action=?", (action,))]


def test_database_events_record_only_host_database_and_sslmode(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    registry = Registry(tmp_path)
    caplog.set_level("INFO")
    record_database_event(registry.plane, DATABASE_TESTED, "root", DatabaseDsn.parse(REMOTE_DSN), ok=True)
    [event] = events(registry.plane, DATABASE_TESTED)
    assert json.loads(event["detail"]) == {"database": "tam", "host": "db.internal:5432", "ok": True,
                                           "sslmode": "require"}
    assert event["subject"] == "database" and event["actor"] == "root"
    assert PASSWORD not in caplog.text and PASSWORD not in event["detail"]
    with pytest.raises(ValueError):
        record_database_event(registry.plane, "database_dropped", "root")


def test_service_view_test_and_repoint_on_sqlite(tmp_path, monkeypatch):
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    registry = Registry(tmp_path)
    checker = FakeChecker()
    service = DatabaseSettingsService(tmp_path, registry.plane, checker=checker, environ={DATABASE_URL_ENV: " "})
    view = service.view()
    assert (view.backend, view.source, view.dsn_masked, view.env_configured) == (
        Backend.SQLITE, ConfigSource.DEFAULT, None, False)
    report = service.test(DatabaseDsn.parse(REMOTE_DSN), "root")
    assert report.ok and checker.calls == [uuid.UUID(registry.plane.current().instance_id)]
    assert service.view().last_check == report
    assert PASSWORD not in service.view().model_dump_json()
    with pytest.raises(Conflict):
        service.repoint(DatabaseDsn.parse(REMOTE_DSN), "root")
    assert not (tmp_path / DATABASE_CONFIG_FILE).exists()


# Guard and provisioning on PostgreSQL


def seed_sqlite_data(root: Path) -> str:
    registry = Registry(root, SwitchableControlPlane.sqlite(root))
    registry.add_user("root", "Root")
    return registry.plane.current().instance_id


@pytest.mark.postgres
def test_env_dsn_on_empty_postgres_starts_a_new_installation(tmp_path, pg_database):
    env = {DATABASE_URL_ENV: pg_database.url}
    plane = open_control_plane(tmp_path, env)
    try:
        assert plane.backend is Backend.POSTGRES
        instance = plane.current().instance_id
        assert PgProvisioner(pg_database.url).state().instance_id == instance
    finally:
        plane.close()
    again = open_control_plane(tmp_path, env)
    again.close()
    assert again.current().instance_id == instance
    assert not (tmp_path / "identity.db").exists()


@pytest.mark.postgres
def test_a_forgotten_dsn_is_refused_instead_of_opening_an_empty_sqlite(tmp_path, pg_database):
    plane = open_control_plane(tmp_path, {DATABASE_URL_ENV: pg_database.url})
    plane.close()
    marker = PostgresMarker.model_validate_json((tmp_path / POSTGRES_MARKER_FILE).read_text())
    assert marker == PostgresMarker(instance_id=plane.current().instance_id,
                                    target=DatabaseDsn.parse(pg_database.url).host_db())
    assert stat.S_IMODE((tmp_path / POSTGRES_MARKER_FILE).stat().st_mode) == 0o600
    assert PASSWORD not in (tmp_path / POSTGRES_MARKER_FILE).read_text()
    assert refusal(lambda: open_control_plane(tmp_path, {})) is StartupRefusal.POSTGRES_DSN_MISSING
    assert not (tmp_path / "identity.db").exists()


def test_a_rollback_to_sqlite_starts_and_drops_the_postgres_marker(tmp_path):
    instance = uuid.UUID(SwitchableControlPlane.sqlite(tmp_path).current().instance_id)
    store = FileDatabaseConfigStore(tmp_path, {})
    store.save(web_config(store, None, instance, generation=2))
    (tmp_path / POSTGRES_MARKER_FILE).write_text(
        PostgresMarker(instance_id=instance, target="db.internal:5432/tam").model_dump_json())
    plane = open_control_plane(tmp_path, {})
    assert plane.backend is Backend.SQLITE
    assert not (tmp_path / POSTGRES_MARKER_FILE).exists()


def test_an_unreadable_postgres_marker_stops_startup(tmp_path):
    (tmp_path / POSTGRES_MARKER_FILE).write_text("{not json")
    assert refusal(lambda: open_control_plane(tmp_path, {})) is StartupRefusal.CONFIG_UNREADABLE
    assert not (tmp_path / "identity.db").exists()


@pytest.mark.postgres
def test_empty_postgres_next_to_sqlite_data_is_refused(tmp_path, pg_database):
    seed_sqlite_data(tmp_path)
    assert refusal(lambda: open_control_plane(tmp_path, {DATABASE_URL_ENV: pg_database.url})) is \
        StartupRefusal.EMPTY_TARGET_WITH_SQLITE_DATA
    assert PgProvisioner(pg_database.url).state().control_schema is False


@pytest.mark.postgres
def test_near_empty_identity_db_keeps_its_instance_id_on_postgres(tmp_path, pg_database):
    local = SwitchableControlPlane.sqlite(tmp_path).current().instance_id
    plane = open_control_plane(tmp_path, {DATABASE_URL_ENV: pg_database.url})
    plane.close()
    assert plane.current().instance_id == local


@pytest.mark.postgres
def test_postgres_of_another_installation_is_refused(tmp_path, pg_database):
    PgProvisioner(pg_database.url).bootstrap(str(uuid.uuid4()))
    seed_sqlite_data(tmp_path)
    assert refusal(lambda: open_control_plane(tmp_path, {DATABASE_URL_ENV: pg_database.url})) is \
        StartupRefusal.FOREIGN_INSTALLATION


@pytest.mark.postgres
def test_web_config_must_match_the_target_installation(tmp_path, pg_database):
    PgProvisioner(pg_database.url).bootstrap(str(uuid.uuid4()))
    store = FileDatabaseConfigStore(tmp_path, {})
    store.save(web_config(store, DatabaseDsn.parse(pg_database.url), uuid.uuid4()))
    assert refusal(lambda: open_control_plane(tmp_path, {})) is StartupRefusal.FOREIGN_INSTALLATION


@pytest.mark.postgres
def test_web_config_pointing_at_an_empty_database_is_refused(tmp_path, pg_database):
    store = FileDatabaseConfigStore(tmp_path, {})
    store.save(web_config(store, DatabaseDsn.parse(pg_database.url), uuid.uuid4()))
    assert refusal(lambda: open_control_plane(tmp_path, {})) is StartupRefusal.FOREIGN_INSTALLATION


@pytest.mark.postgres
def test_web_config_wins_and_lost_key_never_falls_back(tmp_path, pg_database):
    instance = uuid.uuid4()
    PgProvisioner(pg_database.url).bootstrap(str(instance))
    store = FileDatabaseConfigStore(tmp_path, {})
    store.save(web_config(store, DatabaseDsn.parse(pg_database.url), instance, generation=4))
    env = {DATABASE_URL_ENV: "postgresql://tam@127.0.0.1:1/other?sslmode=disable"}
    plane = open_control_plane(tmp_path, env)
    plane.close()
    assert (plane.current().instance_id, plane.current().generation) == (str(instance), 4)
    (tmp_path / MASTER_KEY_FILE).unlink()
    assert refusal(lambda: open_control_plane(tmp_path, env)) is StartupRefusal.KEY_UNAVAILABLE


@pytest.mark.postgres
def test_missing_prerequisites_stop_startup_with_dba_sql(tmp_path, pg_database):
    import psycopg

    with psycopg.connect(pg_database.url, autocommit=True) as admin:
        admin.execute("CREATE ROLE tam_no_rights_w4 LOGIN PASSWORD 'norights'")
    try:
        url = DatabaseDsn.parse(pg_database.url).model_copy(
            update={"user": "tam_no_rights_w4", "password": __import__("pydantic").SecretStr("norights")}).to_uri()
        with pytest.raises(PrerequisitesMissing) as caught:
            open_control_plane(tmp_path, {DATABASE_URL_ENV: url})
        assert any("CREATEROLE" in line for line in caught.value.dba_sql)
    finally:
        with psycopg.connect(pg_database.server.admin_url, autocommit=True) as admin:
            admin.execute("DROP ROLE tam_no_rights_w4")


@pytest.mark.postgres
def test_bootstrap_is_idempotent_and_detects_changed_migrations(pg_database, monkeypatch, tmp_path):
    import psycopg

    instance = str(uuid.uuid4())
    provisioner = PgProvisioner(pg_database.url)
    assert provisioner.bootstrap(instance) == instance
    assert provisioner.bootstrap(str(uuid.uuid4())) == instance
    with psycopg.connect(pg_database.url, autocommit=True) as admin:
        admin.execute("UPDATE tam_control.control_migrations SET sha256 = 'changed' WHERE name = 'control/0001_identity.sql'")
    with pytest.raises(Conflict):
        provisioner.bootstrap(instance)


@pytest.mark.postgres
def test_minimal_admin_bootstraps_control_schemas(pg_database):
    import psycopg

    with pg_admin(pg_database, superuser=False) as url:
        instance = str(uuid.uuid4())
        assert PgProvisioner(url).bootstrap(instance) == instance
        with psycopg.connect(url) as admin:
            tables = {row[0] for row in admin.execute(
                "SELECT table_schema || '.' || table_name FROM information_schema.tables "
                "WHERE table_schema IN ('tam_control', 'tam_learning')")}
    assert {"tam_control.users", "tam_control.meta", "tam_control.workspace_schemas",
            "tam_learning.curricula", "tam_learning.personal_outbox"} <= tables


@pytest.mark.postgres
def test_postgres_control_plane_serves_the_registry(tmp_path, pg_database):
    pytest.importorskip("tam_db.pg_connection")
    plane = open_control_plane(tmp_path, {DATABASE_URL_ENV: pg_database.url})
    try:
        registry = Registry(tmp_path, plane)
        registry.add_user("root", "Root")
        registry.set_org_role("root", "superadmin")
        token = registry.issue_token("root", "codex")
        assert registry.authenticate(token).org_role == "superadmin"
        with pytest.raises(Conflict):
            registry.set_active("root", False)
        assert [event["action"] for event in registry.audit_events(action="user")] == ["user_created"]
    finally:
        plane.close()
    assert not (tmp_path / "identity.db").exists()


@pytest.mark.postgres
def test_repoint_switches_the_live_plane_and_audits(tmp_path, pg_database):
    pytest.importorskip("tam_db.pg_connection")
    plane = open_control_plane(tmp_path, {DATABASE_URL_ENV: pg_database.url})
    recycled = []
    try:
        registry = Registry(tmp_path, plane)
        registry.add_user("root", "Root")
        lease = server_lease(pg_database.url).acquire()
        handed = []

        def handover(target):
            # Called before the switch: the plane still serves the old generation.
            handed.append((target.generation, plane.current().generation))
            lease.handover(target.url)

        service = DatabaseSettingsService(tmp_path, plane, on_activated=lambda: recycled.append(1),
                                          lease_handover=handover, environ={})
        dsn = DatabaseDsn.parse(pg_database.url).model_copy(update={"application_name": "tam-repointed"})
        try:
            view = service.repoint(dsn, "root")
            assert handed == [(1, 0)] and lease.url == dsn.to_uri() and lease.verify()
            with pytest.raises(Conflict):
                server_lease(pg_database.url).acquire()
        finally:
            lease.release()
        assert (view.source, view.generation, recycled) == (ConfigSource.WEB, 1, [1])
        assert registry.list_users()[0]["id"] == "root"
        assert events(plane, DATABASE_REPOINTED)
        stored = json.loads((tmp_path / DATABASE_CONFIG_FILE).read_text())
        assert (stored["generation"], stored["origin"]) == (1, "web")
        options = dict(plane.current().connect_options)
        assert options["passfile"] == str(tmp_path / ".empty-pgpass") and options["sslcertmode"] == "disable"
        with pytest.raises(Conflict):
            DatabaseSettingsService(tmp_path, plane, checker=FakeChecker(TargetState.FOREIGN_INSTALLATION),
                                    environ={}).repoint(dsn, "root")
    finally:
        plane.close()
    assert os.stat(tmp_path / DATABASE_CONFIG_FILE).st_mode & 0o077 == 0


@pytest.mark.postgres
def test_keep_superadmin_is_race_free_on_postgres(tmp_path, pg_database, monkeypatch):
    """SERIALIZABLE plus retry closes the count-then-update race: one demotion wins, the retried one conflicts."""
    plane = open_control_plane(tmp_path, {DATABASE_URL_ENV: pg_database.url})
    try:
        registry = Registry(tmp_path, plane)
        for user_id in ("alice", "bob"):
            registry.add_user(user_id, user_id.title())
            registry.set_org_role(user_id, "superadmin")
        barrier = threading.Barrier(2)
        original = Registry._keep_superadmin

        def slow_check(db, user_id):
            original(db, user_id)
            try:
                barrier.wait(RACE_WAIT_SECONDS)
            except threading.BrokenBarrierError:
                pass

        monkeypatch.setattr(Registry, "_keep_superadmin", staticmethod(slow_check))
        outcomes = {}

        def demote(user_id):
            try:
                registry.set_org_role(user_id, "member")
                outcomes[user_id] = "ok"
            except Conflict:
                outcomes[user_id] = "conflict"

        threads = [threading.Thread(target=demote, args=(user_id,)) for user_id in ("alice", "bob")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sorted(outcomes.values()) == ["conflict", "ok"], outcomes
        assert sum(user["org_role"] == "superadmin" for user in registry.list_users()) == 1
    finally:
        plane.close()


@pytest.mark.postgres
def test_setup_accounts_settings_and_learning_run_on_postgres(tmp_path, pg_database):
    from team_memory.accounts import AccountPolicy, Accounts
    from team_memory.contracts import SetupComplete
    from team_memory.learning.repository import LearningRepository
    from team_memory.metrics import Metrics
    from team_memory.settings import SettingsStore, load_cipher
    from team_memory.setup import SetupService

    plane = open_control_plane(tmp_path, {DATABASE_URL_ENV: pg_database.url})
    try:
        registry = Registry(tmp_path, plane)
        accounts = Accounts(registry, AccountPolicy())
        setup = SetupService(registry, accounts, Metrics())
        issued = setup.issue_token()
        assert setup.status().token_active
        setup.verify(issued.token, "127.0.0.1")
        session = setup.complete(SetupComplete(token=issued.token, company_name="Acme", user_id="root",
                                               name="Root", password="correct horse battery staple"), "127.0.0.1")
        assert session.actor.org_role == "superadmin" and not setup.required()
        assert registry.organization() == {"name": "Acme", "setup_state": "admin_created"}
        assert accounts.session(session.session_id).actor.user_id == "root"
        with pytest.raises(Unauthorized):
            accounts.login_password("root", "wrong password!!", "10.0.0.1")
        assert accounts.login_summary(1)["failures"] == 1
        settings = SettingsStore(registry, load_cipher(tmp_path, {}), {})
        settings.update({"OPENAI_API_KEY": "sk-test-" + "x" * 24, "MEMORY_LLM_PROVIDER": "openai"})
        assert settings.effective()["MEMORY_LLM_PROVIDER"] == "openai"
        settings.record_check("llm", "openai", True, "ok")
        assert settings.checks()[("llm", "openai")]["ok"] is True
        learning = LearningRepository(tmp_path, plane)
        assert learning.instance_id
        revision = learning.replace_curriculum("eng", {"title": "Onboarding", "modules": [
            {"title": "Basics", "summary": "s", "pass_threshold": 0.5, "max_attempts": 2,
             "lessons": [{"title": "One", "body": "b", "record_ids": [1]}]}]}, 0, "root", {0: "h1"}, "2026-09-25T10:00:00Z")
        module = learning.curriculum("eng")["modules"][0]
        lesson = module["lessons"][0]
        learning.update_lesson_source(lesson["id"], "h2", [1], "2026-09-25T11:00:00Z")
        assert (revision, learning.lesson(lesson["id"])["version"]) == (1, 2)
        assert learning.open_lesson("root", lesson["id"], "t")["user_id"] == "root"
        assert learning.open_lesson("root", lesson["id"], "t2")["opened_at"] == "t"
        attempt = {"team_id": "eng", "module_id": module["id"], "user_id": "root", "quiz_revision": 1,
                   "submitted_at": "t", "status": "graded", "score": 1.0, "max_score": 1.0, "pass_threshold": 0.5,
                   "passed": True, "graded_at": "t"}
        log = {"team_id": "eng", "user_id": "root", "at": "t", "event": "quiz", "subject_id": "", "summary": "s"}
        first = learning.insert_attempt(attempt, [], 2, log, "note")
        assert isinstance(first, int) and learning.attempt(first)["passed"] == 1
        assert [note["content"] for note in learning.pending_notes("root")] == ["note"]
    finally:
        plane.close()


@pytest.mark.parametrize("raw", ["postgresql://tam@%2Fvar%2Frun%2Fpostgresql/tam",
                                 "postgresql://tam@db.internal/tam?sslmode=require&sslcert=/etc/ssl/tam.crt",
                                 "postgresql://tam@db.internal/tam?sslrootcert=/etc/ssl/root.crt"])
def test_dashboard_refuses_server_local_dsn_parts(tmp_path, monkeypatch, raw):
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    registry = Registry(tmp_path)
    checker = FakeChecker()
    service = DatabaseSettingsService(tmp_path, registry.plane, checker=checker, environ={})
    dsn = DatabaseDsn.parse(raw, DsnOrigin.ENV)
    for call in (service.test, service.repoint):
        with pytest.raises(InvalidDsn):
            call(dsn, "root")
    assert checker.calls == []


def test_dashboard_checks_run_isolated_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    registry = Registry(tmp_path)
    assert DatabaseSettingsService(tmp_path, registry.plane, environ={}).checker.isolated


@pytest.mark.postgres
def test_c_locale_database_is_refused_at_startup(tmp_path, pg_server):
    from tests.test_db_check import locale_database

    with locale_database(pg_server, "LOCALE_PROVIDER libc LOCALE 'C'") as url, \
            pytest.raises(PrerequisitesMissing) as caught:
        open_control_plane(tmp_path, {DATABASE_URL_ENV: url})
    assert any("BUILTIN_LOCALE 'C.UTF-8'" in line for line in caught.value.dba_sql)


WEB_OPTION_KEYS = {"passfile", "sslcertmode", "gssencmode", "require_auth"}


def test_checker_isolation_follows_the_dsn_origin():
    assert checker_for(DsnOrigin.WEB).isolated and not checker_for(DsnOrigin.ENV).isolated


def test_web_config_without_password_is_refused_even_with_pgpassword(tmp_path, monkeypatch):
    """A web-origin DSN never borrows PGPASSWORD or ~/.pgpass: without its own password it is refused."""
    monkeypatch.setenv("PGPASSWORD", PASSWORD)
    store = FileDatabaseConfigStore(tmp_path, {})
    passwordless = DatabaseDsn.parse("postgresql://tam@db.internal:5432/tam?sslmode=require", DsnOrigin.ENV)
    store.save(web_config(store, passwordless, uuid.uuid4()).model_copy(update={"origin": DsnOrigin.WEB}))
    assert refusal(store.effective) is StartupRefusal.CONFIG_UNREADABLE
    store.save(web_config(store, passwordless, uuid.uuid4()))
    assert store.effective().origin is DsnOrigin.ENV


@pytest.mark.postgres
def test_web_origin_plane_applies_the_overrides_to_every_connection(tmp_path, pg_database, monkeypatch):
    """Control pool, provisioning, role sessions, readers and leases of a WEB-origin plane all connect
    with the empty passfile, without client certificates or GSS, and with password auth only."""
    import psycopg

    home = tmp_path / "home"
    (home / ".postgresql").mkdir(parents=True)
    (home / ".postgresql" / "postgresql.crt").write_text("not a certificate")
    (home / ".pgpass").write_text("*:*:*:*:not-the-password\n")
    (home / ".pgpass").chmod(0o600)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PGSSLCERT", str(home / ".postgresql" / "postgresql.crt"))
    monkeypatch.setenv("PGPASSWORD", "not-the-password")
    instance = uuid.uuid4()
    PgProvisioner(pg_database.url).bootstrap(str(instance))
    root = tmp_path / "root"
    root.mkdir()
    store = FileDatabaseConfigStore(root, {})
    store.save(web_config(store, DatabaseDsn.parse(pg_database.url), instance).model_copy(
        update={"origin": DsnOrigin.WEB}))
    seen = []
    original = psycopg.Connection.connect.__func__

    def spy(cls, conninfo="", **kwargs):
        seen.append({key: kwargs.get(key) for key in WEB_OPTION_KEYS})
        return original(cls, conninfo, **kwargs)

    monkeypatch.setattr(psycopg.Connection, "connect", classmethod(spy))
    monkeypatch.setattr(psycopg, "connect", psycopg.Connection.connect)
    plane = open_control_plane(root, {})
    try:
        assert plane.current().connect_kwargs()["passfile"] == str(root / ".empty-pgpass")
        assert (root / ".empty-pgpass").read_bytes() == b"" and oct((root / ".empty-pgpass").stat().st_mode & 0o777) == "0o600"
        registry = Registry(root, plane)
        registry.add_user("root", "Root")
        key = registry.team_workspace_key("eng")
        plane.workspaces.ensure(key)
        with plane.workspace_reader(key) as reader:
            assert reader.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0] == 0
        current = plane.current()
        with server_lease(current.url, current.settings, connect_overrides=current.connect_kwargs()):
            pass
    finally:
        plane.close()
    assert seen and all(set(options) == WEB_OPTION_KEYS and all(options.values()) for options in seen), seen
    assert {options["passfile"] for options in seen} == {str(root / ".empty-pgpass")}
    assert {options["require_auth"] for options in seen} == {"password,md5,scram-sha-256"}


def test_missing_config_is_none_where_mode_bits_are_not_checked(tmp_path, monkeypatch):
    """Windows CI regression: without the POSIX check a missing file must still mean "no config"."""
    import paths
    monkeypatch.setattr(paths, "POSIX", False)
    assert FileDatabaseConfigStore(tmp_path, {}).load() is None

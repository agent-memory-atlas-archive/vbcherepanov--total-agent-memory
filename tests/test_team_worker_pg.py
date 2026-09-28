"""WorkerPool on PostgreSQL: target chosen from the control plane at spawn, no SQLite file in the workspace."""
import secrets
import threading
import uuid
from pathlib import Path

import pytest

# These scenarios run with a PostgreSQL control plane active, where spawning a worker imports psycopg.
pytest.importorskip("psycopg")

from tam_db.contracts import ActiveDatabase, Backend, StoreDatabase
from team_memory.contracts import Save, Unavailable, Work
from team_memory.registry import Registry
from team_memory.worker import WorkerPool

MASTER_KEY_BYTES = 32
PROVISION_WAIT_SECONDS = 60
LEASE_CHECK_SECONDS = 0.2
LEASE_EXIT_WAIT_SECONDS = 30
INSTANCE_ID = str(uuid.uuid4())


class FixedPlane:
    """ControlPlane double: only ``current`` is used by WorkerPool."""

    def __init__(self, target: ActiveDatabase):
        self.target = target

    def current(self) -> ActiveDatabase:
        return self.target


class RecordingProvisioner:
    def __init__(self):
        self.ensured: list[str] = []

    def ensure(self, key):
        self.ensured.append(key)

    def store_database(self, key):
        raise AssertionError("a SQLite plane must not ask the provisioner")


def sqlite_files(directory: Path) -> list[Path]:
    return sorted(path for pattern in ("*.db", "*.db-wal", "*.db-shm", "*.sqlite", "*.sqlite3")
                  for path in directory.rglob(pattern))


@pytest.fixture
def team(team_backend, tmp_path, monkeypatch):
    """A registry on the selected backend (team_backend exports TAM_TEAM_DATABASE_URL for postgres)."""
    monkeypatch.setenv("MEMORY_LLM_ENABLED", "false")
    monkeypatch.setenv("MEMORY_QUALITY_GATE_ENABLED", "false")
    monkeypatch.setenv("MEMORY_MODE", "fast")
    registry = Registry(tmp_path)
    registry.add_user("vasya", "Вася")
    token = registry.issue_token("vasya", "worker-pg")
    actor = registry.authenticate(token)
    workspace = next(item for item in registry.workspaces(actor) if item.scope.kind.value == "shared")
    return registry, token, actor, workspace


def save_work(actor, workspace, content: str) -> Work:
    return Work(actor=actor, workspace=workspace, operation="memory_save",
                arguments=Save(content=content).model_dump(mode="json", exclude={"scope"}))


def test_sqlite_plane_spawns_sqlite_workers(team):
    registry, token, actor, workspace = team
    provisioner = RecordingProvisioner()
    plane = FixedPlane(ActiveDatabase(backend=Backend.SQLITE, instance_id=str(uuid.uuid4()), generation=0))
    pool = WorkerPool(registry.root, maximum=1, plane=plane, provisioner=provisioner)
    try:
        assert pool.store_database(workspace.key) == StoreDatabase.sqlite()
        saved = pool.invoke(save_work(actor, workspace, "SQLite workers keep memory.db per workspace."), token)
        assert saved["saved"] is True
    finally:
        pool.close()
    assert provisioner.ensured == []
    assert (registry.root / "workspaces" / workspace.key / "memory.db").is_file()


def test_postgres_plane_without_provisioner_is_unavailable(team):
    registry, token, actor, workspace = team
    plane = FixedPlane(ActiveDatabase(backend=Backend.POSTGRES, instance_id=str(uuid.uuid4()), generation=1,
                                      url="postgresql://tam@127.0.0.1:5432/tam?sslmode=disable"))
    pool = WorkerPool(registry.root, maximum=1, plane=plane)
    try:
        with pytest.raises(Unavailable):
            pool.invoke(save_work(actor, workspace, "No provisioner, no worker."), token)
        assert pool.workers == {}
    finally:
        pool.close()


@pytest.mark.postgres
def test_postgres_worker_creates_no_sqlite_file(team, pg_database):
    from team_memory.pg_provision import PgProvisioner, PgWorkspaceProvisioner

    registry, token, actor, workspace = team
    instance_id = PgProvisioner(pg_database.url).bootstrap(str(uuid.uuid4()))
    provisioner = PgWorkspaceProvisioner(pg_database.url, instance_id, secrets.token_bytes(MASTER_KEY_BYTES))
    plane = FixedPlane(ActiveDatabase(backend=Backend.POSTGRES, instance_id=instance_id, generation=1,
                                      url=pg_database.url))
    pool = WorkerPool(registry.root, maximum=1, plane=plane, provisioner=provisioner)
    try:
        saved = pool.invoke(save_work(actor, workspace, "PostgreSQL workers keep workspace data in a schema."), token)
        assert saved["saved"] is True and saved["created_by"]["user_id"] == "vasya"
        recall = Work(actor=actor, workspace=workspace, operation="memory_recall",
                      arguments={"query": "PostgreSQL workspace schema", "limit": 5, "project": None})
        hits = pool.invoke(recall, token)
        assert [hit["id"] for hit in hits][:1] == [saved["id"]]
    finally:
        pool.close()
    assert provisioner.exists(workspace.key)
    workspace_dir = registry.root / "workspaces" / workspace.key
    assert workspace_dir.is_dir()
    assert sqlite_files(workspace_dir) == []


class Gate:
    """MaintenanceGate double: ``state`` is truthy while maintenance is on."""

    def __init__(self):
        self.active = None

    def state(self):
        return self.active


def test_pool_reuses_the_gateways_registry_and_its_plane(team, monkeypatch):
    import team_memory.registry as registry_module

    registry, _token, _actor, workspace = team
    monkeypatch.setattr(registry_module, "Registry", lambda *args, **kwargs: pytest.fail("second Registry opened"))
    pool = WorkerPool(registry.root, maximum=1, registry=registry)
    try:
        assert pool.registry is registry
        assert pool.plane is registry.plane
        assert pool.store_database(workspace.key).backend is registry.plane.current().backend
    finally:
        pool.close()


def test_provisioner_defaults_to_the_active_planes_workspaces(team):
    registry, _token, _actor, workspace = team
    provisioner = RecordingProvisioner()
    target = StoreDatabase.postgres("postgresql://wsr@127.0.0.1:5432/tam?sslmode=disable",
                                    "ws_" + "0" * 48)
    provisioner.store_database = lambda key: target
    plane = FixedPlane(ActiveDatabase(backend=Backend.POSTGRES, instance_id=str(uuid.uuid4()), generation=1,
                                      url="postgresql://tam@127.0.0.1:5432/tam?sslmode=disable"))
    plane.workspaces = provisioner
    pool = WorkerPool(registry.root, maximum=1, plane=plane, registry=registry)
    try:
        assert pool.store_database(workspace.key) is target
        assert provisioner.ensured == [workspace.key]
    finally:
        pool.close()


def test_maintenance_blocks_new_workers_only(team):
    registry, token, actor, workspace = team
    gate = Gate()
    pool = WorkerPool(registry.root, maximum=1, registry=registry, maintenance=gate)
    try:
        gate.active = "migration"
        with pytest.raises(Unavailable, match="Maintenance"):
            pool.invoke(save_work(actor, workspace, "Blocked while the database migrates."), token)
        assert pool.workers == {}
        gate.active = None
        assert pool.invoke(save_work(actor, workspace, "Accepted after maintenance ends."), token)["saved"] is True
    finally:
        pool.close()


@pytest.mark.postgres
def test_pool_follows_the_shared_plane_after_activation(tmp_path, monkeypatch, pg_database):
    """SQLite at start; after the gateway's plane is activated on PostgreSQL the same pool spawns PG workers."""
    from team_memory.database_contracts import DATABASE_URL_ENV
    from team_memory.pg_provision import PgProvisioner

    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    monkeypatch.setenv("MEMORY_LLM_ENABLED", "false")
    monkeypatch.setenv("MEMORY_QUALITY_GATE_ENABLED", "false")
    registry = Registry(tmp_path)
    registry.add_user("vasya", "Вася")
    token = registry.issue_token("vasya", "before")
    actor = registry.authenticate(token)
    workspace = next(item for item in registry.workspaces(actor) if item.scope.kind.value == "shared")
    pool = WorkerPool(registry.root, maximum=1, registry=registry)
    try:
        assert pool.invoke(save_work(actor, workspace, "Written while the plane is SQLite."), token)["saved"]
        assert (registry.root / "workspaces" / workspace.key / "memory.db").is_file()

        current = registry.plane.current()
        PgProvisioner(pg_database.url).bootstrap(current.instance_id)
        registry.plane.activate(ActiveDatabase(backend=Backend.POSTGRES, instance_id=current.instance_id,
                                               generation=current.generation + 1, url=pg_database.url))
        assert pool.recycle() == 1
        registry.add_user("vasya", "Вася")
        token = registry.issue_token("vasya", "after")
        actor = registry.authenticate(token)
        saved = pool.invoke(save_work(actor, workspace, "Written after the switch to PostgreSQL."), token)
        assert saved["saved"] is True
        assert registry.plane.workspaces.exists(workspace.key)
        export = Work(actor=actor, workspace=workspace, operation="memory_export", arguments={"after": 0, "limit": 10})
        assert [record["content"] for record in pool.invoke(export, token)] == [
            "Written after the switch to PostgreSQL."]
    finally:
        pool.close()


class OwnedLock:
    """threading.Lock that knows which thread holds it (``locked()`` alone cannot tell)."""

    def __init__(self):
        self.lock, self.owner = threading.Lock(), None

    def __enter__(self):
        self.lock.acquire()
        self.owner = threading.get_ident()
        return self

    def __exit__(self, *exc_info):
        self.owner = None
        self.lock.release()

    def held_by_me(self) -> bool:
        return self.owner == threading.get_ident()


class CountingProvisioner:
    """Provisioner double serving SQLite stores, so workers really spawn; records ``ensure`` calls."""

    def __init__(self, pool_lock=None, block: dict | None = None, error: BaseException | None = None):
        self.calls: list[str] = []
        self.forced: list[str] = []
        self.pool_lock_held: list[bool] = []
        self.pool_lock, self.block, self.error = pool_lock, block or {}, error
        self.guard = threading.Lock()

    def ensure(self, key, *, force=False):
        with self.guard:
            self.calls.append(key)
            if force:
                self.forced.append(key)
        if self.pool_lock is not None:
            self.pool_lock_held.append(self.pool_lock.held_by_me())
        if key in self.block:
            assert self.block[key].wait(PROVISION_WAIT_SECONDS)
        if self.error is not None:
            raise self.error

    def store_database(self, key):
        return StoreDatabase.sqlite()


def postgres_plane(generation: int = 1) -> FixedPlane:
    return FixedPlane(ActiveDatabase(backend=Backend.POSTGRES, instance_id=INSTANCE_ID, generation=generation,
                                     url="postgresql://tam@127.0.0.1:5432/tam?sslmode=disable"))


def test_ensure_runs_once_per_key_and_target(team):
    registry, _token, _actor, workspace = team
    provisioner = CountingProvisioner()
    plane = postgres_plane()
    pool = WorkerPool(registry.root, maximum=1, plane=plane, provisioner=provisioner, registry=registry)
    try:
        for _ in range(3):
            pool.store_database(workspace.key)
        assert provisioner.calls == [workspace.key]
        plane.target = postgres_plane(generation=2).target
        pool.store_database(workspace.key)
        assert provisioner.calls == [workspace.key] * 2
        pool.forget(workspace.key)
        pool.store_database(workspace.key)
        assert provisioner.calls == [workspace.key] * 3
    finally:
        pool.close()


def test_worker_restart_does_not_provision_again(team):
    registry, token, actor, workspace = team
    provisioner = CountingProvisioner()
    pool = WorkerPool(registry.root, maximum=1, plane=postgres_plane(), provisioner=provisioner, registry=registry)
    try:
        pool.invoke(save_work(actor, workspace, "First spawn provisions the workspace."), token)
        pool.recycle()
        pool.invoke(save_work(actor, workspace, "Second spawn reuses the provisioned workspace."), token)
        assert provisioner.calls == [workspace.key]
    finally:
        pool.close()


def test_provisioning_runs_outside_the_pool_lock_and_blocks_only_its_workspace(team):
    registry, token, actor, workspace = team
    other = next(item for item in registry.workspaces(actor) if item.key != workspace.key)
    release = threading.Event()
    pool = WorkerPool(registry.root, maximum=2, plane=postgres_plane(), registry=registry)
    pool.lock = OwnedLock()
    provisioner = CountingProvisioner(pool_lock=pool.lock, block={workspace.key: release})
    pool.provisioner = provisioner
    try:
        pool.invoke(save_work(actor, other, "Warm worker for the other workspace."), token)
        results: dict[str, object] = {}
        slow = threading.Thread(target=lambda: results.update(
            slow=pool.invoke(save_work(actor, workspace, "Provisioned slowly."), token)))
        twin = threading.Thread(target=lambda: results.update(
            twin=pool.invoke(save_work(actor, workspace, "Waits for the same provisioning."), token)))
        slow.start()
        twin.start()
        fast = pool.invoke(save_work(actor, other, "Served while another workspace provisions."), token)
        assert fast["saved"] is True and "slow" not in results
        release.set()
        slow.join(PROVISION_WAIT_SECONDS)
        twin.join(PROVISION_WAIT_SECONDS)
        assert results["slow"]["saved"] is True and results["twin"]["saved"] is True
        assert provisioner.calls.count(workspace.key) == 1
        assert provisioner.pool_lock_held and not any(provisioner.pool_lock_held)
    finally:
        release.set()
        pool.close()


@pytest.mark.parametrize("error", ["driver", "domain", "compat"])
def test_provisioning_failures_become_unavailable(team, error):
    psycopg = pytest.importorskip("psycopg")
    from tam_db.contracts import PgOperationalError
    from team_memory.contracts import Conflict

    registry, token, actor, workspace = team
    failure = {"driver": psycopg.OperationalError("role creation failed"),
               "domain": Conflict("Workspace map disagrees with the installation id; refusing to provision"),
               "compat": PgOperationalError("lock timeout", sqlstate="55P03")}[error]
    provisioner = CountingProvisioner(error=failure)
    pool = WorkerPool(registry.root, maximum=1, plane=postgres_plane(), provisioner=provisioner, registry=registry)
    try:
        with pytest.raises(Unavailable):
            pool.invoke(save_work(actor, workspace, "Never reaches a worker."), token)
        assert pool.workers == {}
        provisioner.error = None
        assert pool.invoke(save_work(actor, workspace, "Provisioned on retry."), token)["saved"] is True
        assert provisioner.calls == [workspace.key] * 2
    finally:
        pool.close()


@pytest.mark.postgres
def test_worker_exits_when_its_workspace_lease_is_lost(team, pg_database, monkeypatch):
    import time

    import psycopg

    from team_memory.pg_provision import (
        LEASE_APPLICATION,
        LEASE_CHECK_ENV,
        PgProvisioner,
        PgWorkspaceProvisioner,
    )
    from team_memory.worker import LEASE_LOST_EXIT_CODE

    monkeypatch.setenv(LEASE_CHECK_ENV, str(LEASE_CHECK_SECONDS))
    registry, token, actor, workspace = team
    instance_id = PgProvisioner(pg_database.url).bootstrap(str(uuid.uuid4()))
    provisioner = PgWorkspaceProvisioner(pg_database.url, instance_id, secrets.token_bytes(MASTER_KEY_BYTES))
    plane = FixedPlane(ActiveDatabase(backend=Backend.POSTGRES, instance_id=instance_id, generation=1,
                                      url=pg_database.url))
    pool = WorkerPool(registry.root, maximum=1, plane=plane, provisioner=provisioner, registry=registry)
    try:
        assert pool.invoke(save_work(actor, workspace, "Saved while the lease is held."), token)["saved"]
        process = pool.workers[workspace.key][0]
        role = provisioner.target(workspace.key).role
        with psycopg.connect(pg_database.url, autocommit=True) as admin:
            terminated = admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = %s AND application_name = %s",
                (role, LEASE_APPLICATION)).fetchall()
        assert terminated == [(True,)]
        deadline = time.monotonic() + LEASE_EXIT_WAIT_SECONDS
        while process.exitcode is None and time.monotonic() < deadline:
            time.sleep(LEASE_CHECK_SECONDS)
        assert process.exitcode == LEASE_LOST_EXIT_CODE
        with pytest.raises(Unavailable):
            pool.invoke(save_work(actor, workspace, "The dead worker cannot take this."), token)
        again = pool.invoke(save_work(actor, workspace, "A new worker holds a new lease."), token)
        assert again["saved"] is True
    finally:
        pool.close()


def test_a_dead_worker_forces_role_repair_on_the_next_spawn(team):
    registry, token, actor, workspace = team
    provisioner = CountingProvisioner()
    pool = WorkerPool(registry.root, maximum=1, plane=postgres_plane(), provisioner=provisioner, registry=registry)
    try:
        pool.invoke(save_work(actor, workspace, "Served by the first worker."), token)
        process = pool.workers[workspace.key][0]
        process.kill()
        process.join(PROVISION_WAIT_SECONDS)
        with pytest.raises(Unavailable):
            pool.invoke(save_work(actor, workspace, "The dead worker cannot take this."), token)
        assert pool.invoke(save_work(actor, workspace, "Served after the repair."), token)["saved"] is True
        assert provisioner.calls == [workspace.key] * 2
        assert provisioner.forced == [workspace.key]
    finally:
        pool.close()


@pytest.mark.postgres
def test_role_login_failure_is_repaired_and_the_call_retried(team, pg_database):
    import psycopg
    from psycopg import sql

    from team_memory.pg_provision import PgProvisioner, PgWorkspaceProvisioner

    registry, token, actor, workspace = team
    instance_id = PgProvisioner(pg_database.url).bootstrap(str(uuid.uuid4()))
    provisioner = PgWorkspaceProvisioner(pg_database.url, instance_id, secrets.token_bytes(MASTER_KEY_BYTES))
    plane = FixedPlane(ActiveDatabase(backend=Backend.POSTGRES, instance_id=instance_id, generation=1,
                                      url=pg_database.url))
    pool = WorkerPool(registry.root, maximum=1, plane=plane, provisioner=provisioner, registry=registry)
    try:
        assert pool.invoke(save_work(actor, workspace, "Saved before the password drifted."), token)["saved"]
        pool.recycle()
        role = provisioner.target(workspace.key).role
        with psycopg.connect(pg_database.url, autocommit=True) as admin:
            admin.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(secrets.token_urlsafe(24))))
        # The new worker cannot log in; the pool forces one role repair and respawns transparently.
        assert pool.invoke(save_work(actor, workspace, "Saved after the forced repair."), token)["saved"] is True
        export = Work(actor=actor, workspace=workspace, operation="memory_export", arguments={"after": 0, "limit": 10})
        assert [record["content"] for record in pool.invoke(export, token)] == [
            "Saved before the password drifted.", "Saved after the forced repair."]
    finally:
        pool.close()


def test_login_failure_is_retried_once_then_unavailable(team, monkeypatch):
    from team_memory import worker

    registry, token, actor, workspace = team
    provisioner = CountingProvisioner()
    pool = WorkerPool(registry.root, maximum=1, plane=postgres_plane(), provisioner=provisioner, registry=registry)
    replies = iter([worker.Reply(error="Workspace role login failed", code=worker.LOGIN_FAILED_CODE)] * 2)

    class LoginFailingConnection:
        def send(self, payload):
            return None

        def poll(self, timeout):
            return True

        def recv(self):
            return next(replies).model_dump_json()

        def close(self):
            return None

    class IdleProcess:
        def is_alive(self):
            return False

        def join(self, timeout=None):
            return None

        def close(self):
            return None

    def spawn_failing(key, work, database):
        pool.workers[key] = (IdleProcess(), LoginFailingConnection())
        return original_call(key, work, database)

    original_call = pool.call
    monkeypatch.setattr(pool, "call", spawn_failing)
    try:
        with pytest.raises(Unavailable):
            pool.invoke(save_work(actor, workspace, "The role can never log in."), token)
        assert provisioner.calls == [workspace.key] * 2
        assert provisioner.forced == [workspace.key]
        assert pool.workers == {}
    finally:
        pool.close()


def test_worker_lease_uses_the_targets_connect_options(monkeypatch):
    from team_memory import pg_provision, worker

    captured = {}
    monkeypatch.setattr(pg_provision, "workspace_lease",
                        lambda url, key, settings, **kwargs: captured.update(url=url, key=key, **kwargs))
    options = (("gssencmode", "disable"), ("sslcertmode", "disable"))
    database = StoreDatabase(backend=Backend.POSTGRES, url="postgresql://wsr@127.0.0.1:5432/tam?sslmode=disable",
                             schema="ws_" + "0" * 48, connect_options=options)
    worker.workspace_lease("shared", database)
    assert captured["key"] == "shared"
    assert captured["connect_overrides"] == dict(options)
    assert captured["on_lost"] is worker.exit_on_lost_lease

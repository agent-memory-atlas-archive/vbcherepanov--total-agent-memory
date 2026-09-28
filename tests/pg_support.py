"""PostgreSQL test infrastructure (pytest plugin, registered by tests/conftest.py).

Fixtures:
  pg_server     session-scoped server: TAM_TEST_PG_URL (an admin URI allowed to create
                databases and roles, e.g. a CI service container) or a throwaway docker
                container of PG_TEST_IMAGE on a random loopback port. Neither -> skip.
  pg_database   a fresh database per test, created the way plan 1.4 requires
                (template0, UTF8, builtin C.UTF-8) with the vector extension in schema
                "extensions". Dropped afterwards together with any workspace roles the
                test created (roles are cluster-global).
  team_backend  parametrized by --backend: "sqlite" (default), "postgres" or "both".
                The postgres variant exports TAM_TEAM_DATABASE_URL so spawned workers
                inherit it.

Tests marked ``postgres`` run only with --backend=postgres|both or when TAM_TEST_PG_URL
is set, so the default suite never starts containers.
"""

import os
import secrets
import shutil
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

import pytest

from tam_db.contracts import (
    EXTENSIONS_SCHEMA,
    REQUIRED_EXTENSIONS,
    Backend,
    is_workspace_role,
)
from team_memory.database_contracts import DATABASE_URL_ENV, DatabaseDsn

PG_URL_ENV = "TAM_TEST_PG_URL"
PG_IMAGE_ENV = "TAM_TEST_PG_IMAGE"
PG_TEST_IMAGE = "pgvector/pgvector:0.8.1-pg18"
PG_TEST_USER = "tam_test"
PG_CONTAINER_PORT = "5432/tcp"
DOCKER_RUN_TIMEOUT_SECONDS = 300
DOCKER_COMMAND_TIMEOUT_SECONDS = 30
READY_TIMEOUT_SECONDS = 90
READY_POLL_SECONDS = 0.5
CONNECT_TIMEOUT_SECONDS = 3
BACKEND_OPTION = "--backend"
BACKEND_CHOICES = ("sqlite", "postgres", "both")
DEFAULT_BACKEND = "sqlite"
POSTGRES_MARKER = "postgres"
DATABASE_PREFIX = "tam_test_"


@dataclass(frozen=True)
class PgServer:
    """A reachable server. ``admin_url`` may create databases and roles."""

    admin_url: str = field(repr=False)
    container_id: str | None = None

    def url_for(self, database: str) -> str:
        return DatabaseDsn.parse(self.admin_url).model_copy(update={"database": database}).to_uri()


@dataclass(frozen=True)
class PgDatabase:
    name: str
    url: str = field(repr=False)
    server: PgServer


def selected_backends(config: pytest.Config) -> tuple[Backend, ...]:
    choice = config.getoption(BACKEND_OPTION)
    if choice == "both":
        return (Backend.SQLITE, Backend.POSTGRES)
    return (Backend(choice),)


def postgres_enabled(config: pytest.Config) -> bool:
    return Backend.POSTGRES in selected_backends(config) or bool(os.environ.get(PG_URL_ENV))


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(BACKEND_OPTION, action="store", default=DEFAULT_BACKEND, choices=BACKEND_CHOICES,
                     help="team server backend(s) for tests using the team_backend fixture; "
                          "postgres-marked tests run only with postgres or both")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", f"{POSTGRES_MARKER}: needs a PostgreSQL server "
                                       f"(--backend=postgres|both or {PG_URL_ENV})")


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "team_backend" not in metafunc.fixturenames:
        return
    params = [pytest.param(backend, id=backend.value,
                           marks=[pytest.mark.postgres] if backend is Backend.POSTGRES else [])
              for backend in selected_backends(metafunc.config)]
    metafunc.parametrize("team_backend", params, indirect=True)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if postgres_enabled(config):
        return
    skip = pytest.mark.skip(reason=f"PostgreSQL tests need {BACKEND_OPTION}=postgres|both or {PG_URL_ENV}")
    for item in items:
        if item.get_closest_marker(POSTGRES_MARKER) is not None:
            item.add_marker(skip)


def _docker(*args: str, timeout: float = DOCKER_COMMAND_TIMEOUT_SECONDS) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=False)


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return _docker("info", "--format", "{{.ServerVersion}}").returncode == 0
    except subprocess.TimeoutExpired:
        return False


def _published_port(container_id: str) -> int:
    result = _docker("port", container_id, PG_CONTAINER_PORT)
    if result.returncode != 0:
        raise RuntimeError(f"docker port failed: {result.stderr.strip()}")
    for line in result.stdout.splitlines():
        host, _, port = line.strip().rpartition(":")
        if host.startswith("127.0.0.1") and port.isdigit():
            return int(port)
    raise RuntimeError(f"no loopback port published for {PG_CONTAINER_PORT}")


def _wait_ready(container_id: str, admin_url: str) -> None:
    import psycopg

    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    last_error = "pg_isready never succeeded"
    while time.monotonic() < deadline:
        # -h 127.0.0.1: the image's init phase listens on the socket only, so TCP
        # readiness means the final server is up.
        probe = _docker("exec", container_id, "pg_isready", "-h", "127.0.0.1", "-U", PG_TEST_USER)
        if probe.returncode == 0:
            try:
                with psycopg.connect(admin_url, connect_timeout=CONNECT_TIMEOUT_SECONDS) as connection:
                    connection.execute("SELECT 1")
                return
            except psycopg.OperationalError as exc:
                last_error = type(exc).__name__
        else:
            last_error = probe.stdout.strip() or probe.stderr.strip()
        time.sleep(READY_POLL_SECONDS)
    raise RuntimeError(f"PostgreSQL container not ready after {READY_TIMEOUT_SECONDS}s: {last_error}")


def _start_container() -> PgServer:
    password = secrets.token_urlsafe(24)
    name = "tam-test-pg-" + secrets.token_hex(4)
    image = os.environ.get(PG_IMAGE_ENV, PG_TEST_IMAGE)
    run = _docker("run", "--detach", "--rm", "--name", name,
                  "--env", f"POSTGRES_USER={PG_TEST_USER}", "--env", f"POSTGRES_PASSWORD={password}",
                  "--env", "POSTGRES_DB=postgres", "--publish", "127.0.0.1::5432", image,
                  timeout=DOCKER_RUN_TIMEOUT_SECONDS)
    if run.returncode != 0:
        raise RuntimeError(f"docker run {image} failed: {run.stderr.strip()}")
    container_id = run.stdout.strip()
    try:
        port = _published_port(container_id)
        admin_url = f"postgresql://{PG_TEST_USER}:{password}@127.0.0.1:{port}/postgres?sslmode=disable"
        _wait_ready(container_id, admin_url)
    except BaseException:
        _docker("rm", "--force", container_id)
        raise
    return PgServer(admin_url=admin_url, container_id=container_id)


@pytest.fixture(scope="session")
def pg_server(pytestconfig: pytest.Config) -> Iterator[PgServer]:
    pytest.importorskip("psycopg", reason="install the postgres extra: pip install '.[postgres]'")
    external = os.environ.get(PG_URL_ENV)
    if external:
        yield PgServer(admin_url=external)
        return
    if not postgres_enabled(pytestconfig):
        pytest.skip(f"PostgreSQL tests need {BACKEND_OPTION}=postgres|both or {PG_URL_ENV}")
    if not _docker_available():
        pytest.skip(f"docker is not available and {PG_URL_ENV} is not set")
    server = _start_container()
    try:
        yield server
    finally:
        _docker("rm", "--force", server.container_id)


def _workspace_roles(connection) -> set[str]:
    rows = connection.execute("SELECT rolname FROM pg_roles").fetchall()
    return {row[0] for row in rows if is_workspace_role(row[0])}


@contextmanager
def fresh_database(server: PgServer) -> Iterator[PgDatabase]:
    """A new database meeting the prerequisites; dropped on exit with the workspace roles created meanwhile."""
    import psycopg
    from psycopg import sql

    name = DATABASE_PREFIX + secrets.token_hex(6)
    with psycopg.connect(server.admin_url, autocommit=True, connect_timeout=CONNECT_TIMEOUT_SECONDS) as admin:
        roles_before = _workspace_roles(admin)
        admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8' LOCALE_PROVIDER builtin "
                              "BUILTIN_LOCALE 'C.UTF-8'").format(sql.Identifier(name)))
    database = PgDatabase(name=name, url=server.url_for(name), server=server)
    try:
        with psycopg.connect(database.url, autocommit=True, connect_timeout=CONNECT_TIMEOUT_SECONDS) as connection:
            connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(EXTENSIONS_SCHEMA)))
            for extension in REQUIRED_EXTENSIONS:
                connection.execute(sql.SQL("CREATE EXTENSION {} SCHEMA {}").format(
                    sql.Identifier(extension), sql.Identifier(EXTENSIONS_SCHEMA)))
        yield database
    finally:
        with psycopg.connect(server.admin_url, autocommit=True, connect_timeout=CONNECT_TIMEOUT_SECONDS) as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
            for role in sorted(_workspace_roles(admin) - roles_before):
                admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


@pytest.fixture
def pg_database(pg_server: PgServer) -> Iterator[PgDatabase]:
    with fresh_database(pg_server) as database:
        yield database


@pytest.fixture
def team_backend(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Backend:
    backend: Backend = request.param
    if backend is Backend.POSTGRES:
        database: PgDatabase = request.getfixturevalue("pg_database")
        monkeypatch.setenv(DATABASE_URL_ENV, database.url)
    else:
        monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    return backend

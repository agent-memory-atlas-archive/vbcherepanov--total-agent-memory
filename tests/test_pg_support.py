import os
import uuid

import pytest

from tam_db.contracts import (
    EXTENSIONS_SCHEMA,
    MIN_SERVER_VERSION_NUM,
    REQUIRED_EXTENSIONS,
    Backend,
    role_for,
)
from team_memory.database_contracts import DATABASE_URL_ENV, DatabaseDsn
from tests.pg_support import fresh_database

pytestmark = pytest.mark.postgres


def test_pg_database_meets_the_prerequisites(pg_database):
    import psycopg

    with psycopg.connect(pg_database.url) as connection:
        version = connection.execute("SELECT current_setting('server_version_num')::int").fetchone()[0]
        assert version >= MIN_SERVER_VERSION_NUM
        encoding, provider, locale = connection.execute(
            "SELECT pg_encoding_to_char(encoding), datlocprovider, datlocale FROM pg_database "
            "WHERE datname = current_database()").fetchone()
        assert (encoding, provider, locale) == ("UTF8", "b", "C.UTF-8")
        installed = dict(connection.execute(
            "SELECT e.extname, n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace"
        ).fetchall())
        assert all(installed.get(name) == EXTENSIONS_SCHEMA for name in REQUIRED_EXTENSIONS)
        distance = connection.execute(
            "SELECT '[1,0]'::extensions.vector OPERATOR(extensions.<=>) '[0,1]'::extensions.vector").fetchone()[0]
        assert distance == pytest.approx(1.0)
        # Bytewise ordering, like SQLite BINARY.
        ordered = [row[0] for row in connection.execute(
            "SELECT x FROM (VALUES ('b'), ('B'), ('a'), ('_')) AS t(x) ORDER BY x").fetchall()]
        assert ordered == ["B", "_", "a", "b"]


def test_pg_database_url_is_a_valid_tam_dsn(pg_database):
    dsn = DatabaseDsn.parse(pg_database.url)
    assert dsn.database == pg_database.name and not dsn.warnings()


def test_pg_database_cleans_up_workspace_roles(pg_server):
    import psycopg
    from psycopg import sql

    role = role_for("shared", str(uuid.uuid4()))
    with fresh_database(pg_server) as database, psycopg.connect(database.url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role)))
    with psycopg.connect(pg_server.admin_url) as admin:
        assert admin.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone() is None
        assert admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database.name,)).fetchone() is None


def test_team_backend_exports_the_database_url(team_backend):
    if team_backend is Backend.POSTGRES:
        assert DatabaseDsn.parse(os.environ[DATABASE_URL_ENV]).database.startswith("tam_test_")
    else:
        assert DATABASE_URL_ENV not in os.environ


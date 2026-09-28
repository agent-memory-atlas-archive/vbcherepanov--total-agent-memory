import pickle
import sqlite3
import subprocess
import sys
import tomllib
import uuid
from pathlib import Path

import pytest

from tam_db.contracts import (
    CONNECT_OPTION_KEYS,
    DEFAULT_SERIALIZABLE_ATTEMPTS,
    POOL_MAX_SIZE_ENV,
    POOL_MIN_SIZE_ENV,
    STATEMENT_TIMEOUT_ENV,
    ActiveDatabase,
    Backend,
    CompatConnection,
    CompatCursor,
    CompatRow,
    DatabaseSettings,
    PgDatabaseError,
    PgIntegrityError,
    PgOperationalError,
    PgSerializationFailure,
    StoreDatabase,
    UntranslatableSQL,
    WorkspaceTarget,
    as_connect_options,
    error_class_for_sqlstate,
    is_workspace_role,
    is_workspace_schema,
    role_for,
    schema_for,
)

ROOT = Path(__file__).resolve().parents[1]
INSTANCE = str(uuid.UUID("0b8f7c1e-8a52-4c1d-9d57-3a3b2f4e5d6c"))
OTHER_INSTANCE = str(uuid.UUID("5f0e3a9b-1c2d-4e5f-8a7b-9c0d1e2f3a4b"))
PERSONAL_KEY = "personal_" + "a" * 64


def test_schema_for_fits_postgres_identifiers_and_is_deterministic():
    name = schema_for(PERSONAL_KEY)
    assert name == schema_for(PERSONAL_KEY)
    assert len(name) == 51 and len(name.encode()) <= 63
    assert is_workspace_schema(name)
    assert schema_for("shared") != schema_for("team_" + "b" * 64)


@pytest.mark.parametrize("key", ["", " shared", "shared ", "a\nb", "x" * 129, None, 7])
def test_schema_for_rejects_invalid_keys(key):
    with pytest.raises(ValueError):
        schema_for(key)


def test_role_for_is_unique_per_installation():
    role = role_for("shared", INSTANCE)
    assert is_workspace_role(role) and len(role) <= 63
    assert role != role_for("shared", OTHER_INSTANCE)
    assert role != schema_for("shared")
    assert not is_workspace_schema(role) and not is_workspace_role(schema_for("shared"))


@pytest.mark.parametrize("instance", ["not-a-uuid", INSTANCE.upper(), INSTANCE.replace("-", "")])
def test_role_for_requires_canonical_instance_id(instance):
    with pytest.raises(ValueError):
        role_for("shared", instance)


def test_workspace_target():
    target = WorkspaceTarget.for_key("shared", INSTANCE)
    assert target.schema == schema_for("shared") and target.role == role_for("shared", INSTANCE)
    with pytest.raises(ValueError):
        WorkspaceTarget(key="shared", schema=schema_for("other"), role=target.role)
    with pytest.raises(ValueError):
        WorkspaceTarget(key="shared", schema=target.schema, role="postgres")


def test_store_database_variants_hide_the_url_and_pickle():
    assert StoreDatabase.sqlite().url is None
    url = "postgresql://wsr_x:hunter2-secret@127.0.0.1:5432/tam"
    database = StoreDatabase.postgres(url, schema_for("shared"))
    assert "hunter2-secret" not in repr(database)
    assert pickle.loads(pickle.dumps(database)) == database
    with pytest.raises(ValueError):
        StoreDatabase(backend=Backend.SQLITE, url=url)
    with pytest.raises(ValueError):
        StoreDatabase.postgres(url, "public")
    with pytest.raises(ValueError):
        StoreDatabase.postgres("", schema_for("shared"))


def test_active_database_validation():
    url = "postgresql://tam:hunter2-secret@db/tam"
    active = ActiveDatabase(backend=Backend.POSTGRES, instance_id=INSTANCE, generation=2, url=url)
    assert "hunter2-secret" not in repr(active)
    assert ActiveDatabase(backend=Backend.SQLITE, instance_id=INSTANCE, generation=0).url is None
    for kwargs in ({"backend": Backend.POSTGRES, "generation": 1},
                   {"backend": Backend.SQLITE, "generation": 1, "url": url},
                   {"backend": Backend.SQLITE, "generation": -1},
                   {"backend": Backend.SQLITE, "generation": True}):
        with pytest.raises(ValueError):
            ActiveDatabase(instance_id=INSTANCE, **kwargs)
    with pytest.raises(ValueError):
        ActiveDatabase(backend=Backend.SQLITE, instance_id="nope", generation=0)


def test_database_settings_from_environ():
    assert DatabaseSettings.from_environ({}) == DatabaseSettings()
    assert DatabaseSettings().serializable_attempts == DEFAULT_SERIALIZABLE_ATTEMPTS
    settings = DatabaseSettings.from_environ({STATEMENT_TIMEOUT_ENV: " 1500 ", POOL_MAX_SIZE_ENV: "3"})
    assert settings.statement_timeout_ms == 1500 and settings.pool_max_size == 3
    for environ in ({STATEMENT_TIMEOUT_ENV: "fast"}, {STATEMENT_TIMEOUT_ENV: "0"},
                    {POOL_MIN_SIZE_ENV: "5", POOL_MAX_SIZE_ENV: "2"}):
        with pytest.raises(ValueError):
            DatabaseSettings.from_environ(environ)


def test_pg_errors_are_caught_by_existing_sqlite_handlers():
    with pytest.raises(sqlite3.IntegrityError):
        raise PgIntegrityError("duplicate", sqlstate="23505")
    with pytest.raises(sqlite3.OperationalError):
        raise PgSerializationFailure("conflict", sqlstate="40001")
    with pytest.raises(sqlite3.NotSupportedError):
        raise UntranslatableSQL("FTS5 MATCH", "SELECT rowid FROM knowledge_fts WHERE knowledge_fts MATCH ?")
    with pytest.raises(PgDatabaseError) as caught:
        raise PgOperationalError("no such table", sqlstate="42P01")
    assert caught.value.sqlstate == "42P01"
    assert isinstance(caught.value, sqlite3.DatabaseError)


@pytest.mark.parametrize(("sqlstate", "expected"), [
    ("23505", PgIntegrityError), ("23502", PgIntegrityError), ("P0001", PgIntegrityError),
    ("40001", PgSerializationFailure), ("40P01", PgSerializationFailure),
    ("42P01", PgOperationalError), ("42601", PgOperationalError), ("57014", PgOperationalError),
    ("08006", PgOperationalError), ("22012", PgOperationalError), (None, PgOperationalError),
    ("XX000", PgDatabaseError), ("58030", PgDatabaseError),
])
def test_sqlstate_mapping(sqlstate, expected):
    assert error_class_for_sqlstate(sqlstate) is expected


def test_sqlite3_objects_satisfy_the_compat_protocols():
    connection = sqlite3.connect(":memory:")
    try:
        connection.row_factory = sqlite3.Row
        assert isinstance(connection, CompatConnection)
        cursor = connection.execute("SELECT 1 AS one")
        assert isinstance(cursor, CompatCursor)
        assert isinstance(cursor.fetchone(), CompatRow)
    finally:
        connection.close()


def test_contract_modules_do_not_import_the_driver():
    code = ("import sys; import tam_db.contracts, team_memory.database_contracts; "
            "assert not [m for m in sys.modules if m.split('.')[0] in ('psycopg', 'psycopg_pool', 'pgvector')]")
    subprocess.run([sys.executable, "-c", code], cwd=ROOT / "src", check=True, timeout=60)


def test_postgres_extra_is_declared_outside_the_base_install():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    extra = project["optional-dependencies"]["postgres"]
    assert "psycopg[binary,pool]>=3.2,<4" in extra and "pgvector>=0.3,<1" in extra
    assert not any(dep.startswith(("psycopg", "pgvector")) for dep in project["dependencies"])


WEB_OPTIONS = (("gssencmode", "disable"), ("passfile", "/srv/tam/.empty-pgpass"),
               ("require_auth", "password,md5,scram-sha-256"), ("sslcertmode", "disable"))


def test_targets_carry_validated_connect_options():
    url = "postgresql://tam:pw@db/tam"
    store = StoreDatabase.postgres(url, schema_for("shared"), connect_options=WEB_OPTIONS)
    assert store.connect_kwargs() == dict(WEB_OPTIONS)
    assert pickle.loads(pickle.dumps(store)) == store
    active = ActiveDatabase(backend=Backend.POSTGRES, instance_id=INSTANCE, generation=1, url=url,
                            connect_options=WEB_OPTIONS)
    assert active.connect_kwargs() == dict(WEB_OPTIONS)
    assert StoreDatabase.sqlite().connect_kwargs() == {}
    assert {key for key, _ in WEB_OPTIONS} == CONNECT_OPTION_KEYS


@pytest.mark.parametrize(("options", "error"), [
    ((("options", "-csearch_path=ws_x"),), ValueError),
    ((("host", "evil"),), ValueError),
    ((("passfile", "/a"), ("passfile", "/b")), ValueError),
    ((("passfile", ""),), ValueError),
    ((("passfile", "/a\n"),), ValueError),
    ((("passfile",),), TypeError),
    ([("passfile", "/a")], TypeError),
    ((("passfile", 1),), TypeError),
])
def test_connect_options_are_allow_listed(options, error):
    with pytest.raises(error):
        StoreDatabase(backend=Backend.POSTGRES, url="postgresql://tam:pw@db/tam", schema=schema_for("shared"),
                      connect_options=options)
    with pytest.raises(error):
        ActiveDatabase(backend=Backend.POSTGRES, instance_id=INSTANCE, generation=1,
                       url="postgresql://tam:pw@db/tam", connect_options=options)


def test_sqlite_targets_take_no_connect_options():
    with pytest.raises(ValueError):
        StoreDatabase(backend=Backend.SQLITE, connect_options=WEB_OPTIONS)
    with pytest.raises(ValueError):
        ActiveDatabase(backend=Backend.SQLITE, instance_id=INSTANCE, generation=0, connect_options=WEB_OPTIONS)


def test_store_database_accepts_a_mapping_and_hides_options_from_repr():
    url = "postgresql://tam:pw@db/tam"
    from_mapping = StoreDatabase.postgres(url, schema_for("shared"), connect_options=dict(reversed(WEB_OPTIONS)))
    assert from_mapping.connect_options == WEB_OPTIONS
    assert from_mapping == StoreDatabase.postgres(url, schema_for("shared"), connect_options=WEB_OPTIONS)
    assert StoreDatabase.postgres(url, schema_for("shared"), connect_options=None).connect_options == ()
    assert "passfile" not in repr(from_mapping)
    assert pickle.loads(pickle.dumps(from_mapping)).connect_kwargs() == dict(WEB_OPTIONS)
    assert hash(from_mapping) == hash(pickle.loads(pickle.dumps(from_mapping)))
    assert as_connect_options({}) == () and as_connect_options(None) == ()
    with pytest.raises(ValueError):
        StoreDatabase.postgres(url, schema_for("shared"), connect_options={"options": "-c role=x"})

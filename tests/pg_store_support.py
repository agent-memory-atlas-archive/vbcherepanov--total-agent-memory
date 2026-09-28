"""Store-level backend matrix: the same Store test on SQLite and on a PostgreSQL workspace.

Import ``store_backend`` into a test module (``from tests.pg_store_support import store_backend``)
and build the store with ``server.Store(database=store_backend)``. The SQLite variant yields
None, so the store is built exactly as before; the PostgreSQL variant (marked ``postgres``,
skipped unless --backend=postgres|both or TAM_TEST_PG_URL) provisions a workspace schema and
role in a fresh database and yields its StoreDatabase.
"""
import secrets
import uuid

import pytest

from tam_db.contracts import Backend, StoreDatabase

POSTGRES_MARKER = "postgres"
MASTER_KEY_BYTES = 32
DEFAULT_WORKSPACE_KEY = "shared"


def provision_workspace(admin_url: str, key: str = DEFAULT_WORKSPACE_KEY) -> StoreDatabase:
    """Bootstrap the control plane of ``admin_url`` and provision one workspace for ``key``."""
    from team_memory.pg_provision import PgProvisioner, PgWorkspaceProvisioner

    instance_id = PgProvisioner(admin_url).bootstrap(str(uuid.uuid4()))
    provisioner = PgWorkspaceProvisioner(admin_url, instance_id, secrets.token_bytes(MASTER_KEY_BYTES))
    provisioner.ensure(key)
    return provisioner.store_database(key)


@pytest.fixture(params=[pytest.param(Backend.SQLITE, id="sqlite"),
                        pytest.param(Backend.POSTGRES, id="postgres", marks=pytest.mark.postgres)])
def store_backend(request: pytest.FixtureRequest) -> StoreDatabase | None:
    if request.param is Backend.SQLITE:
        return None
    database = request.getfixturevalue("pg_database")
    return provision_workspace(database.url)

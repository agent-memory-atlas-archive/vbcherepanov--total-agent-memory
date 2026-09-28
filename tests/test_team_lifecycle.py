import json
import sqlite3
import sys
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from team_memory.contracts import Conflict
from team_memory.lifecycle import ServerLease, backup, restore
from team_memory.registry import Registry


def test_backup_restore_authentication_and_checksum(tmp_path):
    root = tmp_path / 'original'
    registry = Registry(root)
    registry.add_user('vasya', 'Вася')
    token = registry.issue_token('vasya', 'client')
    snapshot = tmp_path / 'snapshot'
    with ServerLease(root), pytest.raises(Conflict):
        backup(root, snapshot)
    manifest = backup(root, snapshot)
    assert len(manifest.databases) == 1
    recovered = tmp_path / 'recovered'
    restore(snapshot, recovered)
    assert Registry(recovered).authenticate(token).user_id == 'vasya'
    with pytest.raises(FileExistsError):
        restore(snapshot, recovered)
    with (snapshot / 'identity.db').open('ab') as target:
        target.write(b'corrupted')
    with pytest.raises(Conflict):
        restore(snapshot, tmp_path / 'invalid')
    assert not (tmp_path / 'invalid').exists()


def test_lease_released_after_error(tmp_path):
    with pytest.raises(RuntimeError), ServerLease(tmp_path):
        with pytest.raises(Conflict), ServerLease(tmp_path):
            pytest.fail('Second server acquired the same data')
        raise RuntimeError('Crash')
    with ServerLease(tmp_path):
        assert (tmp_path / '.server.lock').is_file()


def test_restore_copy_failure_never_publishes_partial_server(tmp_path, monkeypatch):
    import shutil

    root = tmp_path / 'original'
    Registry(root).add_user('vasya', 'Вася')
    snapshot = tmp_path / 'snapshot'
    backup(root, snapshot)

    def failed_copy(source, target):
        target.write_bytes(b'partial')
        raise OSError('Disk full')

    monkeypatch.setattr(shutil, 'copyfile', failed_copy)
    destination = tmp_path / 'restored'
    with pytest.raises(OSError, match='Disk full'):
        restore(snapshot, destination)
    assert not destination.exists()


def test_backup_refuses_orphan_worker(tmp_path):
    root = tmp_path / 'server'
    Registry(root).add_user('vasya', 'Вася')
    workspace = root / 'workspaces' / 'shared'
    workspace.mkdir(parents=True)
    with sqlite3.connect(workspace / 'memory.db') as db:
        db.execute('CREATE TABLE example(id INTEGER PRIMARY KEY)')
    with ServerLease(workspace), pytest.raises(Conflict):
        backup(root, tmp_path / 'snapshot')
    assert not (tmp_path / 'snapshot').exists()


def test_idempotency_uses_same_transaction_as_write(tmp_path, monkeypatch):
    import server
    from team_memory.contracts import Save, Scope, Update, Work
    from team_memory.worker import Runtime

    root = tmp_path / 'data'
    monkeypatch.setattr(server, 'MEMORY_DIR', root)
    for name, value in {'TAM_MEMORY_DIR': str(root), 'CLAUDE_MEMORY_DIR': str(root),
                        'MEMORY_ASYNC_ENRICHMENT': 'false', 'USE_BINARY_SEARCH': 'true',
                        'MEMORY_QUALITY_GATE_ENABLED': 'false'}.items():
        monkeypatch.setenv(name, value)
    registry = Registry(tmp_path / 'identity')
    registry.add_user('vasya', 'Вася')
    actor = registry.authenticate(registry.issue_token('vasya', 'client'))
    workspace = registry.authorize(actor, Scope(), True)
    runtime = Runtime(str(root))
    request = Save(content='The release database uses SQLite WAL mode.', request_id=uuid4())
    work = Work(actor=actor, workspace=workspace, operation='memory_save',
                arguments=request.model_dump(mode='json', exclude={'scope'}))
    try:
        first = runtime.execute(work)
        assert runtime.execute(work) == first
        assert runtime.store.db.execute('SELECT count(*) FROM tam_requests').fetchone()[0] == 1
        changed = work.model_copy(update={'arguments': {**work.arguments, 'content': 'Different payload'}})
        with pytest.raises(Conflict):
            runtime.execute(changed)
        update = Update(id=first['id'], expected_revision=first['revision'], content='The release database uses WAL checkpoints.',
                        reason='Updated plan', request_id=uuid4())
        work = Work(actor=actor, workspace=workspace, operation='memory_update',
                    arguments=update.model_dump(mode='json', exclude={'scope'}))
        second = runtime.execute(work)
        assert runtime.execute(work) == second
        assert runtime.store.db.execute('SELECT count(*) FROM knowledge').fetchone()[0] == 2
    finally:
        runtime.store.db.close()
    with sqlite3.connect(root / 'memory.db') as db:
        assert db.execute('SELECT count(*) FROM tam_requests').fetchone()[0] == 2


# Storage backend dispatch (SQLite or PostgreSQL)

PG_DSN_VAR = 'TAM_TEST_TARGET_DSN'


def test_configured_backend_follows_database_json_then_env_then_sqlite(tmp_path, monkeypatch):
    from tam_db.contracts import Backend
    from team_memory import replication
    from team_memory.database_config import FileDatabaseConfigStore
    from team_memory.database_contracts import DATABASE_URL_ENV, DatabaseConfig
    from team_memory.lifecycle import configured_backend

    root = tmp_path / 'server'
    Registry(root)
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    assert configured_backend(root) is Backend.SQLITE and replication.storage_backend(root) == 'sqlite'
    replication.require_sqlite(root)
    monkeypatch.setenv(DATABASE_URL_ENV, 'postgresql://tam@db.internal/tam')
    assert replication.storage_backend(root) == 'postgres'
    with pytest.raises(Conflict, match='PostgreSQL tooling'):
        replication.require_sqlite(root)
    monkeypatch.delenv(DATABASE_URL_ENV)
    store = FileDatabaseConfigStore(root)
    store.save(DatabaseConfig(backend=Backend.POSTGRES, dsn_token='sealed', instance_id=uuid4(), generation=1,
                              updated_at=datetime.now(UTC), updated_by='test'))
    assert configured_backend(root) is Backend.POSTGRES and replication.storage_backend(root) == 'postgres'


def test_restore_rejects_a_dsn_for_sqlite_snapshots_and_requires_one_for_postgres(tmp_path):
    from team_memory.database_contracts import DatabaseDsn, DsnOrigin

    root = tmp_path / 'original'
    Registry(root).add_user('vasya', 'Вася')
    backup(root, tmp_path / 'snapshot')
    dsn = DatabaseDsn.parse('postgresql://tam@db.internal/tam', DsnOrigin.ENV)
    with pytest.raises(Conflict, match='SQLite snapshot'):
        restore(tmp_path / 'snapshot', tmp_path / 'restored', dsn)
    postgres = tmp_path / 'pg-snapshot'
    postgres.mkdir()
    (postgres / 'manifest.json').write_text('{"format_version": 2, "backend": "postgres"}')
    with pytest.raises(Conflict, match='--dsn-env'):
        restore(postgres, tmp_path / 'restored')


def run_cli(monkeypatch, capsys, root, *args):
    from team_memory import cli

    monkeypatch.setattr(sys, 'argv', ['tam-team', '--root', str(root), *args])
    try:
        cli.main()
        code = 0
    except SystemExit as exit_info:
        code = exit_info.code or 0
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.mark.postgres
def test_cli_checks_migrates_backs_up_and_serves_on_postgres(tmp_path, monkeypatch, capsys, pg_database):
    import psycopg

    from team_memory.database_contracts import DATABASE_URL_ENV

    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    root = tmp_path / 'server'
    registry = Registry(root)
    registry.add_user('vasya', 'Вася')
    registry.set_organization({'name': 'Acme'})
    monkeypatch.setenv(PG_DSN_VAR, pg_database.url)

    code, out, _ = run_cli(monkeypatch, capsys, root, 'db-check', '--dsn-env', PG_DSN_VAR)
    assert code == 0 and 'target: empty' in out and pg_database.url.split('@')[0] not in out
    code, out, _ = run_cli(monkeypatch, capsys, root, 'db-migrate', '--dsn-env', PG_DSN_VAR, '--dry-run')
    assert code == 0 and 'identity' in out and 'blocker' not in out
    with psycopg.connect(pg_database.url) as connection:
        assert connection.execute("SELECT to_regnamespace('tam_control')").fetchone()[0] is None
    code, out, err = run_cli(monkeypatch, capsys, root, 'db-migrate', '--dsn-env', PG_DSN_VAR)
    assert code == 0, err
    assert 'now uses PostgreSQL' in out and not (root / 'identity.db').exists()
    assert (root / 'database.json').is_file() and list((root / 'archive').glob('sqlite-*/identity.db'))

    code, _, err = run_cli(monkeypatch, capsys, root, 'user-add', 'petya', 'Петя')
    assert code == 0, err
    with psycopg.connect(pg_database.url) as connection:
        users = [row[0] for row in connection.execute('SELECT id FROM tam_control.users ORDER BY id')]
    assert users == ['petya', 'vasya']

    code, _, err = run_cli(monkeypatch, capsys, root, 'backup', '--out', str(tmp_path / 'pg-snapshot'))
    assert code == 0, err
    manifest = json.loads((tmp_path / 'pg-snapshot' / 'manifest.json').read_text())
    assert manifest['backend'] == 'postgres' and manifest['format_version'] == 2
    code, out, _ = run_cli(monkeypatch, capsys, root, 'db-migrate', '--dsn-env', PG_DSN_VAR, '--dry-run')
    assert code == 1 and 'already runs on PostgreSQL' in out

    captured = {}

    def fake_run(app, **kwargs):
        from team_memory.pg_provision import server_lease

        captured['app'], captured['kwargs'] = app, kwargs
        with pytest.raises(Conflict, match='Another TAM server'):
            server_lease(pg_database.url).acquire()

    def fake_create_app(service, dashboard, **kwargs):
        captured['create_app'] = kwargs
        return object()

    import uvicorn

    from team_memory import app as team_app
    monkeypatch.setattr(uvicorn, 'run', fake_run)
    monkeypatch.setattr(team_app, 'create_app', fake_create_app)
    code, _, err = run_cli(monkeypatch, capsys, root, 'serve', '--port', '0')
    assert code == 0, err
    wiring = captured['create_app']
    assert wiring['migration'] is not None and wiring['database'] is not None
    assert wiring['maintenance'] is wiring['migration'].gate
    assert wiring['migration'].switch.plane.current().backend.value == 'postgres'


def test_cli_rejects_missing_dsn_variables(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(PG_DSN_VAR, raising=False)
    code, _, err = run_cli(monkeypatch, capsys, tmp_path / 'server', 'db-check', '--dsn-env', PG_DSN_VAR)
    assert code == 2 and PG_DSN_VAR in err
    monkeypatch.setenv(PG_DSN_VAR, 'host=db user=tam')
    code, _, err = run_cli(monkeypatch, capsys, tmp_path / 'server', 'db-migrate', '--dsn-env', PG_DSN_VAR)
    assert code == 2 and 'key=value' in err


def test_serve_wires_one_maintenance_gate_on_sqlite(tmp_path, monkeypatch, capsys):
    import uvicorn

    from team_memory import app as team_app
    from team_memory.database_contracts import DATABASE_URL_ENV, MaintenanceReason

    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    captured = {}
    monkeypatch.setattr(uvicorn, 'run', lambda app, **kwargs: captured.update(kwargs))
    monkeypatch.setattr(team_app, 'create_app', lambda service, dashboard, **kwargs: captured.update(wiring=kwargs))
    code, _, err = run_cli(monkeypatch, capsys, tmp_path / 'server', 'serve', '--port', '0')
    assert code == 0, err
    wiring = captured['wiring']
    assert wiring['maintenance'] is wiring['migration'].gate is not None
    pool = wiring['migration'].switch.pool
    assert pool.maintenance is wiring['maintenance'] and pool.registry.plane is wiring['migration'].switch.plane
    assert wiring['database'].lease_handover == wiring['migration'].switch.lease.handover
    assert wiring['migration'].checker.isolated is True
    plane = wiring['migration'].switch.plane
    assert not plane.maintenance_active()
    wiring['maintenance'].enter(MaintenanceReason.MIGRATION, None)
    assert plane.maintenance_active()
    wiring['maintenance'].leave()
    assert wiring['database'].migration is wiring['migration']
    assert wiring['migration'].switch.plane.current().backend.value == 'sqlite'


def test_mutating_cli_commands_wait_for_a_running_migration(tmp_path, monkeypatch, capsys):
    from team_memory.database_contracts import MigrationPhase, MigrationProgress

    root = tmp_path / 'server'
    Registry(root)
    journal = root / 'migration'
    journal.mkdir()
    now = datetime.now(UTC)
    progress = MigrationProgress(job_id=uuid4(), plan_id=uuid4(), phase=MigrationPhase.COPY_WORKSPACES,
                                 started_at=now, updated_at=now, started_by='admin', target='postgresql://tam@db/tam')
    (journal / f'{progress.job_id}.json').write_text(progress.model_dump_json())
    code, _, err = run_cli(monkeypatch, capsys, root, 'user-add', 'late', 'Late')
    assert code == 1 and 'migration is running' in err
    assert 'late' not in [user['id'] for user in Registry(root).list_users()]
    finished = progress.model_copy(update={'phase': MigrationPhase.CANCELLED, 'finished_at': now})
    (journal / f'{progress.job_id}.json').write_text(finished.model_dump_json())
    code, _, err = run_cli(monkeypatch, capsys, root, 'user-add', 'late', 'Late')
    assert code == 0, err


def test_cli_accepts_operator_dsns_the_web_form_refuses(monkeypatch):
    import argparse

    from team_memory.cli import dsn_from_env

    monkeypatch.setenv(PG_DSN_VAR, 'postgresql://tam@%2Fvar%2Frun%2Fpostgresql/tam?sslrootcert=/etc/ssl/db-ca.pem')
    dsn = dsn_from_env(argparse.ArgumentParser(), PG_DSN_VAR)
    assert dsn.hosts[0].host == '/var/run/postgresql' and dsn.sslrootcert == '/etc/ssl/db-ca.pem'


@pytest.mark.postgres
def test_serve_exits_non_zero_when_another_server_takes_the_lease(tmp_path, monkeypatch, capsys, pg_database):
    import time

    import psycopg
    import uvicorn

    from team_memory import app as team_app
    from team_memory import cli
    from team_memory.database_contracts import DATABASE_URL_ENV, MaintenanceReason

    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv(DATABASE_URL_ENV, pg_database.url)
    monkeypatch.setenv('TAM_TEAM_PG_LEASE_CHECK_SECONDS', '0.2')
    shutdowns, captured = [], {}
    monkeypatch.setattr(cli, 'request_shutdown', lambda: shutdowns.append('sigterm'))
    monkeypatch.setattr(team_app, 'create_app', lambda service, dashboard, **kwargs: captured.update(kwargs))

    def fake_run(app, **kwargs):
        with psycopg.connect(pg_database.url, autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                          "WHERE application_name = 'tam-lease' AND datname = current_database()")
        deadline = time.monotonic() + 10
        while captured['maintenance'].state() is None and time.monotonic() < deadline:
            time.sleep(0.05)

    monkeypatch.setattr(uvicorn, 'run', fake_run)
    code, _, _ = run_cli(monkeypatch, capsys, tmp_path / 'server', 'serve', '--port', '0')
    assert code == cli.EXIT_LEASE_LOST and shutdowns == ['sigterm']
    assert captured['maintenance'].state().reason is MaintenanceReason.LEASE_LOST

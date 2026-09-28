import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from tam_db.contracts import Backend
from team_memory.accounts import AccountPolicy, Accounts
from team_memory.contracts import Conflict, DomainError, Forbidden, Unauthorized
from team_memory.lifecycle import ServerLease
from team_memory.offboarding import (
    export_personal,
    personal_key,
    personal_workspace,
    purge_personal,
)
from team_memory.registry import Registry
from team_memory.service import MemoryService
from team_memory.worker import WorkerPool
from tests.team_db_helpers import learning, live_worker_lease, stored_identity_bytes

SRC = Path(__file__).resolve().parents[1] / 'src'
TEAM = {'kind': 'team', 'team_id': 'sales'}


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture(autouse=True)
def backend(team_backend):
    return team_backend


def cli(root: Path, *args: str, launcher: tuple[str, str] = ('-m', 'team_memory.cli')) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *launcher, '--root', str(root), *args],
                          env={**os.environ, 'PYTHONPATH': str(SRC)}, capture_output=True, text=True, timeout=120,
                          check=False)


@pytest.fixture
async def company(tmp_path, monkeypatch):
    monkeypatch.setenv('MEMORY_LLM_ENABLED', 'false')
    monkeypatch.setenv('MEMORY_QUALITY_GATE_ENABLED', 'false')
    registry = Registry(tmp_path / 'server')
    for user in ('leaver', 'colleague'):
        registry.add_user(user, user.title())
    registry.add_team('sales', 'Sales')
    registry.membership('leaver', 'sales', 'editor')
    registry.membership('colleague', 'sales', 'reader')
    leaver, colleague = registry.issue_token('leaver', 'codex'), registry.issue_token('colleague', 'codex')
    pool = WorkerPool(registry.root, maximum=3)
    service = MemoryService(registry, pool)
    ids = {}
    try:
        first = await service.call(leaver, 'memory_save', {'content': 'Private: my review of the Orchard deal is due Friday.'})
        await service.call(leaver, 'memory_save', {'content': 'Private: call the dentist on Monday.'})
        await service.call(leaver, 'memory_update', {'id': first['data']['id'], 'expected_revision': 1,
                                                     'content': 'Private: my review of the Orchard deal is due Thursday.',
                                                     'reason': 'moved'})
        ids['team'] = (await service.call(leaver, 'memory_save', {'scope': TEAM, 'content': 'Orchard renews in Q3.'}))['data']['id']
        ids['shared'] = (await service.call(leaver, 'memory_save', {'scope': {'kind': 'shared'},
                                                                    'content': 'The cafeteria opens at 9.'}))['data']['id']
    finally:
        pool.close()
    with learning(registry) as db:
        db.execute("INSERT INTO personal_outbox(user_id,team_id,content,created_at) VALUES "
                   "('leaver','sales','Onboarding note: lesson 2 passed.','2026-09-25T10:00:00Z'),"
                   "('colleague','sales','Onboarding note for someone else.','2026-09-25T10:00:00Z')")
    return registry, leaver, colleague, ids


def outbox(registry, user):
    with learning(registry) as db:
        return [row[0] for row in db.execute("SELECT content FROM personal_outbox WHERE user_id=?", (user,))]


@pytest.mark.anyio
async def test_export_writes_records_and_history_for_a_disabled_user_only(company, tmp_path):
    registry, _, _, _ = company
    out = tmp_path / 'leaver.jsonl'
    with pytest.raises(Forbidden):
        export_personal(registry, 'leaver', out)
    assert not out.exists()
    registry.set_active('leaver', False)
    result = cli(registry.root, 'user-export', 'leaver', '--out', str(out))
    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    lines = [json.loads(line) for line in out.read_text().splitlines()]
    header, records = lines[0], [line['record'] for line in lines if line['type'] == 'record']
    history = [line['event'] for line in lines if line['type'] == 'history']
    assert header['type'] == 'export' and header['records'] == len(records) == 3 and header['history'] == len(history)
    assert {r['status'] for r in records} == {'active', 'superseded'}
    assert any('Thursday' in r['content'] for r in records) and any('Friday' in r['content'] for r in records)
    assert all(r['created_by']['user_id'] == 'leaver' for r in records)
    assert [h['operation'] for h in history].count('insert') == 3
    assert any(h['operation'] == 'update' and h['reason'] == 'moved' for h in history)
    notes = [line['note']['content'] for line in lines if line['type'] == 'onboarding_note']
    assert notes == ['Onboarding note: lesson 2 passed.'] and header['onboarding_notes'] == 1
    again = cli(registry.root, 'user-export', 'leaver', '--out', str(out))
    assert again.returncode == 1 and out.read_text().splitlines()[0] == json.dumps(header, ensure_ascii=False)
    assert registry_events(registry, 'personal_exported') == [('personal_exported', 'leaver')]


WITHOUT_POSTGRES_DRIVER = "import runpy, sys; sys.modules['psycopg'] = None; runpy.run_module('team_memory.cli', run_name='__main__')"


@pytest.mark.anyio
async def test_sqlite_purge_with_an_archived_copy_runs_without_the_postgres_extra(company, backend):
    """The personal install and the SQLite team backend do not ship psycopg; purge must not import it."""
    if backend is not Backend.SQLITE:
        pytest.skip('the postgres extra is installed with the PostgreSQL backend')
    registry, _, _, _ = company
    registry.set_active('leaver', False)
    archived = registry.root / 'archive' / 'sqlite-20260925T100000Z' / 'workspaces' / personal_key(registry, 'leaver')
    archived.mkdir(parents=True)
    (archived / 'memory.db').write_bytes(b'archived copy')
    result = cli(registry.root, 'user-purge', 'leaver', '--confirm', 'leaver', launcher=('-c', WITHOUT_POSTGRES_DRIVER))
    assert result.returncode == 0, result.stderr
    assert not archived.exists()


def registry_events(registry, action):
    with registry.connect() as db:
        return [tuple(row) for row in db.execute('SELECT action,subject FROM admin_events WHERE action=?', (action,))]


@pytest.mark.anyio
async def test_purge_needs_disabled_user_confirmation_and_a_stopped_server(company):
    registry, _, colleague, ids = company
    workspace = personal_workspace(registry, 'leaver')
    with pytest.raises(Forbidden):
        purge_personal(registry, 'leaver', 'leaver')
    accounts = Accounts(registry, AccountPolicy())
    session = accounts.redeem_invite('leaver', accounts.issue_invite('leaver').code, 'correct horse battery staple', 'ip')
    registry.set_active('leaver', False)
    assert cli(registry.root, 'user-purge', 'leaver', '--confirm', 'colleague').returncode == 1
    with ServerLease(registry.root), pytest.raises(Conflict):
        purge_personal(registry, 'leaver', 'leaver')
    with live_worker_lease(registry, personal_key(registry, 'leaver')), pytest.raises(Conflict):
        purge_personal(registry, 'leaver', 'leaver')
    assert registry.plane.workspaces.exists(personal_key(registry, 'leaver'))

    archived = registry.root / 'archive' / 'sqlite-20260925T100000Z' / 'workspaces' / personal_key(registry, 'leaver')
    archived.mkdir(parents=True)
    (archived / 'memory.db').write_bytes(b'archived copy: call the dentist')
    kept = archived.parent / registry.team_workspace_key('sales')
    kept.mkdir()

    result = cli(registry.root, 'user-purge', 'leaver', '--confirm', 'leaver')
    assert result.returncode == 0, result.stderr
    assert not archived.exists() and kept.is_dir()
    assert '3 records, 1 onboarding notes' in result.stdout and 'Backups taken earlier still contain it' in result.stdout
    assert outbox(registry, 'leaver') == [] and outbox(registry, 'colleague') == ['Onboarding note for someone else.']
    with registry.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM sessions WHERE user_id='leaver'").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM invites WHERE user_id='leaver' AND used_at IS NULL").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM tokens WHERE user_id='leaver' AND revoked=0").fetchone()[0] == 0
    with pytest.raises(Unauthorized):
        accounts.session(session.session_id)
    assert not workspace.exists() and not registry.plane.workspaces.exists(personal_key(registry, 'leaver'))
    assert registry_events(registry, 'personal_purged') == [('personal_purged', 'leaver')]
    assert b'dentist' not in stored_identity_bytes(registry)
    with pytest.raises(Conflict):
        purge_personal(registry, 'leaver', 'leaver')
    with pytest.raises(DomainError):
        purge_personal(registry, 'nobody', 'nobody')

    pool = WorkerPool(registry.root, maximum=3)
    service = MemoryService(registry, pool)
    try:
        for scope, record_id in ((TEAM, ids['team']), ({'kind': 'shared'}, ids['shared'])):
            record = (await service.call(colleague, 'memory_get', {'scope': scope, 'id': record_id}))['data']
            assert record['created_by']['user_id'] == 'leaver'
    finally:
        pool.close()


@pytest.mark.anyio
async def test_purged_user_recreated_in_the_same_process_can_save_again(company):
    registry, _, _, _ = company
    pool = WorkerPool(registry.root, maximum=3, registry=registry)
    service = MemoryService(registry, pool)
    try:
        await service.call(registry.issue_token('leaver', 'laptop'), 'memory_save', {'content': 'Before purge.'})
        registry.set_active('leaver', False)
        pool.close()
        dropped = []
        purge_personal(registry, 'leaver', 'leaver', on_dropped=lambda key: (dropped.append(key), pool.forget(key)))
        assert dropped == [personal_key(registry, 'leaver')]
        registry.set_active('leaver', True)
        saved = await service.call(registry.issue_token('leaver', 'new-laptop'), 'memory_save',
                                   {'content': 'After purge, a fresh personal area.'})
        assert saved['data']['saved']
    finally:
        pool.close()


@pytest.mark.anyio
async def test_purge_refuses_while_a_postgres_server_holds_its_lease(company):
    registry, _, _, _ = company
    if registry.plane.backend is not Backend.POSTGRES:
        pytest.skip('the advisory server lease exists only on PostgreSQL')
    from team_memory.pg_provision import server_lease

    registry.set_active('leaver', False)
    current = registry.plane.current()
    with server_lease(current.url, current.settings), pytest.raises(Conflict):
        purge_personal(registry, 'leaver', 'leaver')
    assert registry.plane.workspaces.exists(personal_key(registry, 'leaver'))

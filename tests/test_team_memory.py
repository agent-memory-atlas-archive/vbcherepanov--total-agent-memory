import json
import sqlite3

import pytest
from pydantic import ValidationError

from team_memory.audit import AuditedConnection, authorship, install
from team_memory.contracts import (
    Actor,
    Conflict,
    Forbidden,
    Save,
    Scope,
    ScopeKind,
    Unauthorized,
)
from team_memory.registry import Registry
from tests.team_db_helpers import stored_identity_bytes


@pytest.fixture(autouse=True)
def backend(team_backend):
    """Every test of this module runs on each selected team backend (--backend)."""
    return team_backend


@pytest.fixture
def registry(tmp_path):
    result = Registry(tmp_path)
    result.add_user('vasya', 'Вася')
    result.add_user('petya', 'Петя')
    result.add_team('engineering', 'Разработка')
    result.membership('vasya', 'engineering', 'editor')
    result.membership('petya', 'engineering', 'reader')
    return result


def test_identity_membership_and_revocation(registry):
    token = registry.issue_token('vasya', 'codex')
    actor = registry.authenticate(token)
    assert actor == Actor(user_id='vasya', display_name='Вася', client='codex')
    assert len(registry.workspaces(actor)) == 3
    team = Scope(kind=ScopeKind.team, team_id='engineering')
    assert registry.authorize(actor, team, True).writable
    registry.membership('vasya', 'engineering', None)
    with pytest.raises(Forbidden):
        registry.authorize(actor, team, False)
    registry.revoke(token)
    with pytest.raises(Unauthorized):
        registry.authenticate(token)
    assert token.encode() not in stored_identity_bytes(registry)


def test_reader_cannot_write_and_cannot_select_foreign_personal_owner(registry):
    actor = registry.authenticate(registry.issue_token('petya', 'cursor'))
    with pytest.raises(Forbidden):
        registry.authorize(actor, Scope(kind=ScopeKind.team, team_id='engineering'), True)
    with pytest.raises(ValidationError):
        Save.model_validate({'content': 'secret', 'scope': {'kind': 'personal', 'owner_id': 'vasya'}})
    with pytest.raises(ValidationError):
        Save(content='secret', tags=['scope:shared'])
    with pytest.raises(ValidationError):
        Save.model_validate({'content': 'secret', 'created_by': 'vasya'})
    with pytest.raises(ValueError):
        registry.add_user('../escape', 'Escape')


def test_audit_is_atomic_and_preserves_original_author():
    db = sqlite3.connect(':memory:', factory=AuditedConnection)
    db.execute('CREATE TABLE knowledge(id INTEGER PRIMARY KEY,type TEXT,content TEXT,context TEXT,project TEXT,tags TEXT,status TEXT,superseded_by INTEGER)')
    install(db)
    vasya = Actor(user_id='vasya', display_name='Вася', client='codex')
    petya = Actor(user_id='petya', display_name='Петя', client='cursor')
    with db.transaction(vasya):
        db.execute("INSERT INTO knowledge(id,content,status) VALUES (1,'first','active')")
        db.commit()
    with pytest.raises(RuntimeError), db.transaction(petya):
        db.executescript("UPDATE knowledge SET content='rolled back' WHERE id=1;")
        db.commit()
        raise RuntimeError('Simulated crash before transaction commit')
    assert db.execute('SELECT content FROM knowledge').fetchone()[0] == 'first'
    assert db.execute('SELECT COUNT(*) FROM tam_history').fetchone()[0] == 1
    with db.transaction(petya, 'Correction'):
        db.execute("UPDATE knowledge SET content='second' WHERE id=1")
    meta = authorship(db, 1)
    assert meta['revision'] == 2
    assert meta['created_by']['user_id'] == 'vasya'
    assert meta['updated_by']['user_id'] == 'petya'
    row = db.execute('SELECT actor,before_state,after_state FROM tam_history ORDER BY sequence DESC').fetchone()
    assert json.loads(row[0])['user_id'] == 'petya'
    assert json.loads(row[1])['content'] == 'first'
    assert json.loads(row[2])['content'] == 'second'
    db.close()


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.anyio
async def test_real_workers_isolate_search_and_preserve_edit_authors(registry, monkeypatch):
    from team_memory.service import MemoryService
    from team_memory.worker import WorkerPool
    monkeypatch.setenv('MEMORY_LLM_ENABLED', 'false')
    monkeypatch.setenv('MEMORY_QUALITY_GATE_ENABLED', 'false')
    monkeypatch.setenv('MEMORY_MODE', 'fast')
    pool = WorkerPool(registry.root, maximum=2)
    service = MemoryService(registry, pool)
    vasya = registry.issue_token('vasya', 'codex')
    petya = registry.issue_token('petya', 'cursor')
    team = {'kind': 'team', 'team_id': 'engineering'}
    registry.membership('petya', 'engineering', 'editor')
    try:
        private = await service.call(vasya, 'memory_save', {'content': 'Private comet launch key belongs to Vasya.'})
        await service.call(petya, 'memory_save', {'content': 'Private comet launch schedule belongs to Petya.'})
        shared = await service.call(vasya, 'memory_save', {'content': 'The shared comet launch is on Tuesday.', 'scope': {'kind': 'shared'}})
        confirmed = await service.call(petya, 'memory_save', {'content': 'The shared comet launch is on Tuesday.', 'scope': {'kind': 'shared'}})
        assert confirmed['data']['id'] == shared['data']['id']
        events = await service.call(petya, 'memory_history', {'id': shared['data']['id'], 'scope': {'kind': 'shared'}})
        assert events['data'][-1]['operation'] == 'confirm'
        assert events['data'][-1]['actor']['user_id'] == 'petya'
        saved = await service.call(vasya, 'memory_save', {'content': 'Our engineering comet launch uses PostgreSQL.', 'scope': team})
        results = await service.call(petya, 'memory_recall', {'query': 'comet launch', 'limit': 20})
        encoded = json.dumps(results)
        assert 'belongs to Vasya' not in encoded
        assert 'belongs to Petya' in encoded
        assert 'Tuesday' in encoded
        assert 'PostgreSQL' in encoded
        assert len(pool.workers) == 2
        original = saved['data']
        updated = await service.call(petya, 'memory_update', {'scope': team, 'id': original['id'],
                                    'expected_revision': original['revision'], 'reason': 'New decision',
                                    'content': 'Our engineering comet launch uses SQLite.'})
        assert updated['data']['created_by']['user_id'] == 'vasya'
        assert updated['data']['updated_by']['user_id'] == 'petya'
        with pytest.raises(Conflict):
            await service.call(vasya, 'memory_delete', {'scope': team, 'id': original['id'],
                               'expected_revision': original['revision'], 'reason': 'Outdated decision'})
        history = await service.call(vasya, 'memory_history', {'scope': team, 'id': original['id']})
        assert history['data'][-1]['actor']['user_id'] == 'petya'
        assert history['data'][-1]['after_state']['superseded_by'] == updated['data']['id']
        registry.membership('petya', 'engineering', None)
        with pytest.raises(Forbidden):
            await service.call(petya, 'memory_get', {'scope': team, 'id': updated['data']['id']})
        registry.revoke(vasya)
        with pytest.raises(Unauthorized):
            await service.call(vasya, 'memory_get', {'id': private['data']['id']})
        assert shared['data']['created_by']['user_id'] == 'vasya'
    finally:
        pool.close()


def test_http_auth_and_identity_is_per_request(registry):
    from starlette.testclient import TestClient

    from team_memory.app import create_app
    from team_memory.service import MemoryService
    from team_memory.worker import WorkerPool
    token = registry.issue_token('vasya', 'codex')
    service = MemoryService(registry, WorkerPool(registry.root))
    with TestClient(create_app(service)) as client:
        assert client.get('/').status_code == 200
        assert client.post('/api/call', json={'name': 'memory_scopes'}).status_code == 401
        headers = {'Authorization': 'Bearer ' + token}
        result = client.post('/api/call', headers=headers, json={'name': 'memory_scopes'})
        assert result.json()['actor']['user_id'] == 'vasya'
        assert client.post('/api/call', headers=headers, content=b'x' * 1_000_001).status_code == 413
        assert client.post('/api/call', headers={**headers, 'Origin': 'https://untrusted.invalid'},
                           json={'name': 'memory_scopes'}).status_code == 403
        registry.revoke(token)
        assert client.post('/api/call', headers=headers, json={'name': 'memory_scopes'}).status_code == 401


def test_remote_stdio_to_real_http_mcp(registry, tmp_path):
    import os
    import socket
    import subprocess
    import sys
    import time
    from pathlib import Path

    import httpx

    token = registry.issue_token('vasya', 'remote-client')
    token_file = tmp_path / 'client-token'
    token_file.write_text(token)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, 'PYTHONPATH': str(root / 'src')}
    with (tmp_path / 'http.log').open('w') as log:
        process = subprocess.Popen([sys.executable, '-m', 'team_memory.cli', '--root', str(registry.root),
                                    'serve', '--port', str(port)], env=env, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 20
            while True:
                try:
                    if httpx.get(f'http://127.0.0.1:{port}/healthz').status_code == 200:
                        break
                except httpx.ConnectError:
                    if time.monotonic() >= deadline:
                        raise
                time.sleep(0.05)
            frames = [
                {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
                    'protocolVersion': '2025-06-18', 'capabilities': {},
                    'clientInfo': {'name': 'tests', 'version': '1'}}},
                {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}},
                {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
                 'params': {'name': 'memory_scopes', 'arguments': {}}},
                {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call',
                 'params': {'name': 'memory_scopes', 'arguments': {'created_by': 'petya'}}},
            ]
            bridge_env = {**env, 'TAM_REMOTE_URL': f'http://127.0.0.1:{port}/mcp',
                          'PYTHONIOENCODING': 'ascii',
                          'TAM_REMOTE_TOKEN_FILE': str(token_file)}
            result = subprocess.run([sys.executable, str(root / 'src/team_memory/remote.py')],
                                    input=''.join(json.dumps(frame) + '\n' for frame in frames),
                                    env=bridge_env, capture_output=True, text=True, encoding='utf-8', timeout=30, check=False)
            assert result.returncode == 0, result.stderr
            replies = {r['id']: r for r in map(json.loads, result.stdout.splitlines())}
            assert 'protocolVersion' in replies[1]['result']
            assert len(replies[2]['result']['tools']) >= 8
            data = json.loads(replies[3]['result']['content'][0]['text'])
            assert data['actor']['user_id'] == 'vasya'
            assert replies[4]['result']['isError'] is True
            import mcp.types as mcp_types
            if hasattr(mcp_types, 'PROTOCOL_VERSION_META_KEY'):
                modern = {'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call', 'params': {
                    'name': 'memory_scopes', 'arguments': {}, '_meta': {
                        mcp_types.PROTOCOL_VERSION_META_KEY: mcp_types.LATEST_PROTOCOL_VERSION,
                        mcp_types.CLIENT_INFO_META_KEY: {'name': 'modern-client', 'version': '1'},
                        mcp_types.CLIENT_CAPABILITIES_META_KEY: {},
                    }}}
                result = subprocess.run([sys.executable, str(root / 'src/team_memory/remote.py')],
                                        input=json.dumps(modern) + '\n', env=bridge_env,
                                        capture_output=True, text=True, encoding='utf-8', timeout=30, check=False)
                reply = json.loads(result.stdout)
                assert json.loads(reply['result']['content'][0]['text'])['actor']['user_id'] == 'vasya'
        finally:
            process.terminate()
            process.wait(timeout=10)


def test_real_store_update_rolls_back_new_record_and_audit(registry, monkeypatch, tmp_path):
    import server
    from team_memory.contracts import Update, Work
    from team_memory.worker import Runtime

    data_dir = tmp_path / 'isolated-runtime'
    monkeypatch.setattr(server, 'MEMORY_DIR', data_dir)
    for key, value in {'TAM_MEMORY_DIR': str(data_dir), 'CLAUDE_MEMORY_DIR': str(data_dir),
                       'MEMORY_QUALITY_GATE_ENABLED': 'false', 'MEMORY_ASYNC_ENRICHMENT': 'false',
                       'USE_BINARY_SEARCH': 'true'}.items():
        monkeypatch.setenv(key, value)
    runtime = Runtime(str(data_dir))
    actor = registry.authenticate(registry.issue_token('vasya', 'test'))
    workspace = registry.authorize(actor, Scope(), True)
    request = Save(content='The database deployment uses PostgreSQL for durable records.')
    try:
        original = runtime.execute(Work(actor=actor, workspace=workspace, operation='memory_save',
                                       arguments=request.model_dump(mode='json', exclude={'scope'})))
        before_count = runtime.store.db.execute('SELECT COUNT(*) FROM knowledge').fetchone()[0]
        before_history = runtime.store.db.execute('SELECT COUNT(*) FROM tam_history').fetchone()[0]
        save = runtime.save

        def fail_after_save(*args, **kwargs):
            save(*args, **kwargs)
            raise Conflict('Injected failure after the replacement was saved')

        monkeypatch.setattr(runtime, 'save', fail_after_save)
        update = Update(id=original['id'], expected_revision=original['revision'],
                        content='The database deployment uses SQLite for durable records.', reason='Changed decision')
        with pytest.raises(Conflict):
            runtime.execute(Work(actor=actor, workspace=workspace, operation='memory_update',
                                 arguments=update.model_dump(mode='json', exclude={'scope'})))
        assert runtime.store.db.execute('SELECT COUNT(*) FROM knowledge').fetchone()[0] == before_count
        assert runtime.store.db.execute('SELECT COUNT(*) FROM tam_history').fetchone()[0] == before_history
        assert runtime.record(original['id'])['status'] == 'active'
    finally:
        runtime.store.db.close()

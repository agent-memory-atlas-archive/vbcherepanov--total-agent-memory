import os
import subprocess
import sys
from pathlib import Path

import pytest

from team_memory.accounts import AccountPolicy, Accounts
from team_memory.contracts import Conflict, Forbidden, Unauthorized
from team_memory.registry import Registry
from team_memory.service import MemoryService

PASSWORD = 'correct horse battery staple'
SRC = Path(__file__).resolve().parents[1] / 'src'


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture(autouse=True)
def backend(team_backend):
    """Every test of this module runs on each selected team backend (--backend)."""
    return team_backend


class RecordingPool:
    def __init__(self):
        self.calls = []

    def search_order(self, scopes):
        return scopes

    def invoke(self, work, credential):
        self.calls.append(work.operation)
        return {'id': 1, 'saved': True} if work.operation != 'memory_recall' else []


@pytest.fixture
def org(tmp_path):
    registry = Registry(tmp_path)
    registry.add_user('root', 'Root')
    registry.set_org_role('root', 'superadmin')
    registry.add_user('leaver', 'Leaver')
    registry.add_team('sales', 'Sales')
    registry.membership('leaver', 'sales', 'editor')
    return registry


def cli(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, '-m', 'team_memory.cli', '--root', str(root), *args],
                          env={**os.environ, 'PYTHONPATH': str(SRC)}, capture_output=True, text=True, timeout=60,
                          check=False)


@pytest.mark.anyio
async def test_user_disable_ends_every_access_path(org):
    accounts = Accounts(org, AccountPolicy())
    tokens = [org.issue_token('leaver', client) for client in ('codex', 'cursor')]
    session = accounts.redeem_invite('leaver', accounts.issue_invite('leaver').code, PASSWORD, 'ip')
    pending = accounts.issue_invite('leaver')
    pool = RecordingPool()
    memory = MemoryService(org, pool)
    await memory.call(tokens[0], 'memory_save', {'scope': {'kind': 'shared'}, 'content': 'Before offboarding.'})
    assert pool.calls == ['memory_save']

    result = cli(org.root, 'user-disable', 'leaver')
    assert result.returncode == 0, result.stderr
    assert '2 tokens revoked, 1 sessions ended, 1 invites voided' in result.stdout

    def session_credential():
        return accounts.session(session.session_id, touch=False).actor

    for credential in (*tokens, session_credential):
        with pytest.raises(Unauthorized):
            await memory.call(credential, 'memory_save', {'scope': {'kind': 'shared'}, 'content': 'After.'})
        with pytest.raises(Unauthorized):
            await memory.call(credential, 'memory_recall', {'scope': {'kind': 'personal'}, 'query': 'notes'})
        with pytest.raises(Unauthorized):
            await memory.call(credential, 'memory_get', {'id': 1})
    assert pool.calls == ['memory_save']
    with pytest.raises(Unauthorized):
        accounts.session(session.session_id)
    with pytest.raises(Unauthorized):
        accounts.redeem_invite('leaver', pending.code, PASSWORD, 'ip')
    with pytest.raises(Forbidden):
        org.issue_token('leaver', 'reissue')
    event = org.audit_events(action='user_disabled')[0]
    assert event['subject'] == 'leaver' and event['actor'] == 'cli'

    result = cli(org.root, 'user-enable', 'leaver')
    assert result.returncode == 0, result.stderr
    for token in tokens:
        with pytest.raises(Unauthorized):
            org.authenticate(token)
    fresh = org.issue_token('leaver', 'new-laptop')
    assert (await memory.call(fresh, 'memory_scopes', {}))['actor']['user_id'] == 'leaver'


def test_last_superadmin_cannot_be_disabled(org):
    result = cli(org.root, 'user-disable', 'root')
    assert result.returncode == 1
    assert 'At least one active superadmin must remain' in result.stderr
    assert org.authenticate(org.issue_token('root', 'still-here')).user_id == 'root'
    with pytest.raises(Conflict):
        org.set_active('root', False)
    assert cli(org.root, 'user-disable', 'nobody').returncode == 1


def test_dashboard_and_cli_share_one_disable_path(org):
    from team_memory.contracts import UserActive
    from team_memory.dashboard_service import DashboardService
    from team_memory.metrics import Metrics
    from team_memory.settings import SettingsStore, load_cipher

    token = org.issue_token('leaver', 'codex')
    accounts = Accounts(org, AccountPolicy())
    memory = MemoryService(org, RecordingPool())
    dashboard = DashboardService(org, accounts, memory, SettingsStore(org, load_cipher(org.root)), Metrics())
    admin = org.authenticate(org.issue_token('root', 'admin'))
    effect = dashboard.set_active(admin, UserActive(user_id='leaver', active=False))
    assert effect == {'user_id': 'leaver', 'active': False, 'tokens_revoked': 1, 'sessions_ended': 0,
                      'invites_voided': 0}
    with pytest.raises(Unauthorized):
        org.authenticate(token)
    assert org.audit_events(action='user_disabled')[0]['actor'] == 'root'

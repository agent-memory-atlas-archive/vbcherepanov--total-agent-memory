import pytest

from team_memory.accounts import AccountPolicy, Accounts
from team_memory.contracts import Forbidden, Unauthorized
from team_memory.registry import Registry
from team_memory.service import MemoryService

TEAM = {'kind': 'team', 'team_id': 'finance'}
PASSWORD = 'correct horse battery staple'


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
def registry(tmp_path):
    result = Registry(tmp_path)
    result.add_user('anna', 'Anna')
    result.add_team('finance', 'Finance')
    result.membership('anna', 'finance', 'editor')
    return result


class RevokedDuringOperation:
    """Pool double: authorises like the real pool, then access is withdrawn while the worker runs."""

    def __init__(self, registry, withdraw):
        self.registry, self.withdraw = registry, withdraw
        self.calls = 0

    def search_order(self, scopes):
        return scopes

    def invoke(self, work, credential):
        actor = credential() if callable(credential) else self.registry.authenticate(credential)
        self.registry.authorize(actor, work.workspace.scope, work.operation == 'memory_save')
        self.calls += 1
        self.withdraw()
        return {'id': 7, 'content': work.arguments.get('content', ''), 'saved': True}


def service(registry, withdraw):
    pool = RevokedDuringOperation(registry, withdraw)
    return MemoryService(registry, pool), pool


@pytest.mark.anyio
async def test_committed_write_is_success_when_membership_goes_mid_operation(registry):
    token = registry.issue_token('anna', 'test')
    memory, pool = service(registry, lambda: registry.membership('anna', 'finance', None))
    result = await memory.call(token, 'memory_save', {'scope': TEAM, 'content': 'Close on day 3.'})
    assert result['data']['id'] == 7 and result['scope'] == TEAM
    with pytest.raises(Forbidden):
        await memory.call(token, 'memory_save', {'scope': TEAM, 'content': 'Second write.'})
    assert pool.calls == 1


@pytest.mark.anyio
async def test_committed_write_is_success_when_token_is_revoked_mid_operation(registry):
    token = registry.issue_token('anna', 'test')
    memory, _ = service(registry, lambda: registry.revoke(token))
    result = await memory.call(token, 'memory_save', {'scope': TEAM, 'content': 'Close on day 3.'})
    assert result['data']['saved'] is True
    with pytest.raises(Unauthorized):
        await memory.call(token, 'memory_save', {'scope': TEAM, 'content': 'Second write.'})


@pytest.mark.anyio
async def test_committed_write_is_success_when_dashboard_session_ends_mid_operation(registry):
    accounts = Accounts(registry, AccountPolicy())
    session = accounts.redeem_invite('anna', accounts.issue_invite('anna').code, PASSWORD, 'ip')

    def credential():
        return accounts.session(session.session_id, touch=False).actor

    memory, _ = service(registry, lambda: accounts.logout(session.session_id))
    result = await memory.call(credential, 'memory_save', {'scope': TEAM, 'content': 'Close on day 3.'})
    assert result['data']['id'] == 7
    with pytest.raises(Unauthorized):
        await memory.call(credential, 'memory_save', {'scope': TEAM, 'content': 'Second write.'})


@pytest.mark.anyio
@pytest.mark.parametrize('operation,arguments', [
    ('memory_get', {'scope': TEAM, 'id': 7}),
    ('memory_recall', {'scope': TEAM, 'query': 'close'}),
])
async def test_read_is_withheld_when_access_goes_mid_operation(registry, operation, arguments):
    token = registry.issue_token('anna', 'test')
    memory, _ = service(registry, lambda: registry.membership('anna', 'finance', None))
    with pytest.raises(Forbidden):
        await memory.call(token, operation, arguments)


@pytest.mark.anyio
async def test_session_read_is_withheld_when_session_ends_mid_operation(registry):
    accounts = Accounts(registry, AccountPolicy())
    session = accounts.redeem_invite('anna', accounts.issue_invite('anna').code, PASSWORD, 'ip')
    memory, _ = service(registry, lambda: accounts.logout(session.session_id))
    with pytest.raises(Unauthorized):
        await memory.call(lambda: accounts.session(session.session_id, touch=False).actor, 'memory_get',
                          {'scope': TEAM, 'id': 7})

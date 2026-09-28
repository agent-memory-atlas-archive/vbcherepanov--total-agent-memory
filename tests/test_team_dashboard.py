import json
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from team_memory import provider_check
from team_memory.accounts import (
    AccountPolicy,
    Accounts,
    hash_password,
    normalize_invite,
    verify_password,
)
from team_memory.app import create_app
from team_memory.contracts import (
    Actor,
    Conflict,
    DomainError,
    Forbidden,
    RateLimited,
    Scope,
    ScopeKind,
    Unauthorized,
)
from team_memory.dashboard_service import DashboardService
from team_memory.metrics import Metrics
from team_memory.registry import Registry
from team_memory.sections import (
    SECTIONS,
    Capability,
    Section,
    register,
    visible_sections,
)
from team_memory.service import MemoryService
from team_memory.settings import MASTER_KEY_FILE, SettingsStore, load_cipher
from team_memory.worker import WorkerPool
from tests.team_db_helpers import identity, skip_unless_sqlite, stored_identity_bytes

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = 'correct horse battery staple'
USERS = {'root': 'superadmin', 'boss': 'member', 'dev': 'member', 'audit': 'company_viewer', 'other': 'member'}


@pytest.fixture(autouse=True)
def backend(team_backend):
    """Every test of this module runs on each selected team backend (--backend)."""
    return team_backend


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def build(root: Path, clock=None, transport=None, environ=None):
    registry = Registry(root)
    accounts = Accounts(registry, AccountPolicy(), **({'clock': clock} if clock else {}))
    settings = SettingsStore(registry, load_cipher(root, {}), environ if environ is not None else {})
    pool = WorkerPool(registry.root, maximum=2, environment=settings.overrides)
    service = MemoryService(registry, pool)
    dashboard = DashboardService(registry, accounts, service, settings, Metrics(), provider_transport=transport)
    return registry, accounts, settings, pool, dashboard, create_app(service, dashboard)


def seed(registry: Registry, accounts: Accounts):
    for user_id, role in USERS.items():
        registry.add_user(user_id, user_id.title())
        if role != 'member':
            registry.set_org_role(user_id, role)
        invite = accounts.issue_invite(user_id)
        accounts.redeem_invite(user_id, invite.code, PASSWORD, '127.0.0.1')
    registry.add_team('eng', 'Engineering')
    registry.add_team('ops', 'Operations')
    registry.membership('boss', 'eng', 'manager')
    registry.membership('dev', 'eng', 'editor')
    registry.membership('other', 'ops', 'editor')


@pytest.fixture
def world(tmp_path):
    registry, accounts, settings, pool, dashboard, app = build(tmp_path / 'root')
    seed(registry, accounts)
    with TestClient(app) as client:
        yield {'registry': registry, 'accounts': accounts, 'settings': settings, 'pool': pool,
               'dashboard': dashboard, 'client': client}


def sign_in(client, user_id, password=PASSWORD):
    client.cookies.clear()
    reply = client.post('/dashboard/api/login', json={'user_id': user_id, 'password': password})
    assert reply.status_code == 200, reply.text
    return {'X-CSRF-Token': reply.json()['csrf']}


OLD_SCHEMA = """
CREATE TABLE users (id TEXT PRIMARY KEY, name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE teams (id TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE membership (user_id TEXT REFERENCES users(id), team_id TEXT REFERENCES teams(id),
    role TEXT NOT NULL CHECK(role IN ('reader','editor')), PRIMARY KEY(user_id,team_id));
CREATE TABLE tokens (digest TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
    client TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE admin_events (id INTEGER PRIMARY KEY, at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    action TEXT NOT NULL, subject TEXT NOT NULL);
"""


def test_migrates_v14_identity_database_in_place(tmp_path, backend):
    skip_unless_sqlite(backend, "upgrades a legacy SQLite identity.db in place")
    root = tmp_path / 'legacy'
    root.mkdir()
    token = 'legacy-token-value'
    with sqlite3.connect(root / 'identity.db') as db:
        db.executescript(OLD_SCHEMA)
        db.execute("INSERT INTO users VALUES ('vasya','Вася',1)")
        db.execute("INSERT INTO teams VALUES ('eng','Engineering')")
        db.execute("INSERT INTO membership VALUES ('vasya','eng','editor')")
        db.execute("INSERT INTO tokens(digest,user_id,client) VALUES (?,?,?)", (Registry.digest(token), 'vasya', 'codex'))
        db.execute("INSERT INTO admin_events(action,subject) VALUES ('user_created','vasya')")
    db.close()
    registry = Registry(root)
    assert registry.authenticate(token) == Actor(user_id='vasya', display_name='Вася', client='codex', org_role='member')
    assert registry.team_role('vasya', 'eng') == 'editor'
    registry.membership('vasya', 'eng', 'manager')
    assert registry.team_role('vasya', 'eng') == 'manager'
    assert registry.authorize(registry.authenticate(token), Scope(kind=ScopeKind.team, team_id='eng'), True).writable
    with sqlite3.connect(root / 'identity.db') as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE membership SET role='owner'")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE users SET org_role='god'")
        schema = db.execute("SELECT sql FROM sqlite_master WHERE name='membership'").fetchone()[0]
    db.close()
    assert "'manager'" in schema
    Registry(root)
    events = registry.audit_events()
    assert [e['action'] for e in events].count('schema_migrated') == 1
    assert events[-1]['action'] == 'user_created'


def test_registry_contract_roles_and_oversight(tmp_path):
    registry = Registry(tmp_path)
    for user_id in ('root', 'boss', 'dev', 'audit'):
        registry.add_user(user_id, user_id)
    registry.add_team('eng', 'Engineering')
    registry.add_team('ops', 'Operations')
    registry.membership('boss', 'eng', 'manager')
    registry.membership('dev', 'eng', 'reader')
    with registry.acting_as('root'):
        registry.set_org_role('root', 'superadmin')
        registry.set_org_role('audit', 'company_viewer')
    assert registry.audit_events(limit=1)[0] == {**registry.audit_events(limit=1)[0], 'actor': 'root',
                                                 'action': 'org_role:company_viewer', 'subject': 'audit'}
    assert registry.org_role('audit') == 'company_viewer'
    assert registry.teams_of('boss') == [('eng', 'manager')]
    assert registry.team_members('eng') == [('boss', 'boss', 'manager'), ('dev', 'dev', 'reader')]
    assert registry.list_teams() == registry.teams() == [('eng', 'Engineering'), ('ops', 'Operations')]
    assert {u['id']: u['org_role'] for u in registry.list_users()}['root'] == 'superadmin'
    actors = {uid: registry.authenticate(registry.issue_token(uid, 'test')) for uid in ('root', 'boss', 'dev', 'audit')}
    assert actors['root'].org_role == 'superadmin'
    assert [registry.can_view_team_people(actors[u], 'eng') for u in ('root', 'boss', 'dev', 'audit')] == [True, True, False, True]
    assert not registry.can_view_team_people(actors['boss'], 'ops')
    assert not registry.can_view_team_people(actors['root'], 'missing')
    ops = Scope(kind=ScopeKind.team, team_id='ops')
    assert not registry.authorize(actors['audit'], ops, False).writable
    with pytest.raises(Forbidden):
        registry.authorize(actors['audit'], ops, True)
    with pytest.raises(Forbidden):
        registry.authorize(actors['boss'], ops, False)
    with pytest.raises(Forbidden):
        registry.authorize(actors['root'], Scope(kind=ScopeKind.team, team_id='missing'), False)
    assert registry.authorize(actors['boss'], Scope(kind=ScopeKind.team, team_id='eng'), True).writable
    assert registry.authorize(actors['root'], Scope(), False).owner_id == 'root'
    assert len(registry.workspaces(actors['root'])) == 2
    with pytest.raises(Conflict):
        registry.set_org_role('root', 'member')
    with pytest.raises(Conflict):
        registry.set_active('root', False)
    with pytest.raises(ValueError):
        registry.set_org_role('dev', 'owner')
    with pytest.raises(Conflict):
        registry.delete_team('eng')
    registry.plane.workspaces.ensure(registry.team_workspace_key('ops'))
    with pytest.raises(Conflict):
        registry.delete_team('ops')


def test_password_hashing():
    stored = hash_password(PASSWORD)
    scheme, n, r, p, salt, digest = stored.split('$')
    assert (scheme, n, r, p) == ('scrypt', str(2 ** 15), '8', '1')
    assert len(bytes.fromhex(salt)) == 16 and len(bytes.fromhex(digest)) == 32
    assert PASSWORD not in stored
    assert verify_password(PASSWORD, stored)
    assert not verify_password(PASSWORD + '!', stored)
    assert not verify_password(PASSWORD, stored[:-2] + ('00' if stored[-2:] != '00' else '11'))
    assert not verify_password(PASSWORD, 'garbage')
    assert hash_password(PASSWORD) != stored


def test_invite_is_single_use_expires_and_is_stored_hashed(tmp_path):
    clock = Clock()
    registry = Registry(tmp_path)
    accounts = Accounts(registry, AccountPolicy(invite_ttl_seconds=3600), clock=clock)
    registry.add_user('ivan', 'Ivan')
    invite = accounts.issue_invite('ivan')
    assert len(normalize_invite(invite.code)) == 20
    assert normalize_invite(invite.code).encode() not in stored_identity_bytes(registry)
    with pytest.raises(DomainError):
        accounts.redeem_invite('ivan', invite.code, 'short', 'ip')
    session = accounts.redeem_invite('ivan', invite.code.lower().replace('-', ' '), PASSWORD, 'ip')
    assert session.actor.user_id == 'ivan' and session.method == 'invite'
    with pytest.raises(Unauthorized):
        accounts.redeem_invite('ivan', invite.code, PASSWORD, 'ip')
    assert accounts.login_password('ivan', PASSWORD, 'ip').actor.user_id == 'ivan'
    stale, fresh = accounts.issue_invite('ivan'), None
    fresh = accounts.issue_invite('ivan')
    with pytest.raises(Unauthorized):
        accounts.redeem_invite('ivan', stale.code, PASSWORD, 'ip')
    clock.now += 3601
    with pytest.raises(Unauthorized):
        accounts.redeem_invite('ivan', fresh.code, PASSWORD, 'ip')
    registry.add_user('olga', 'Olga')
    foreign = accounts.issue_invite('olga')
    with pytest.raises(Unauthorized):
        accounts.redeem_invite('ivan', foreign.code, PASSWORD, 'ip2')
    registry.set_active('olga', False)
    with pytest.raises(Unauthorized):
        accounts.redeem_invite('olga', foreign.code, PASSWORD, 'ip3')


def test_session_idle_absolute_expiry_and_logout(tmp_path):
    clock = Clock()
    registry = Registry(tmp_path)
    accounts = Accounts(registry, AccountPolicy(session_idle_seconds=600, session_max_seconds=3600), clock=clock)
    registry.add_user('ivan', 'Ivan')
    session = accounts.redeem_invite('ivan', accounts.issue_invite('ivan').code, PASSWORD, 'ip')
    clock.now += 500
    accounts.session(session.session_id)
    clock.now += 500
    assert accounts.session(session.session_id).actor.client == 'dashboard'
    clock.now += 601
    with pytest.raises(Unauthorized):
        accounts.session(session.session_id)
    long = accounts.login_password('ivan', PASSWORD, 'ip')
    for _ in range(6):
        clock.now += 590
        accounts.session(long.session_id)
    clock.now += 590
    with pytest.raises(Unauthorized):
        accounts.session(long.session_id)
    short = accounts.login_password('ivan', PASSWORD, 'ip')
    accounts.logout(short.session_id)
    with pytest.raises(Unauthorized):
        accounts.session(short.session_id)
    assert short.session_id.encode() not in stored_identity_bytes(registry)
    token = registry.issue_token('ivan', 'cli')
    via_token = accounts.login_token(token, 'ip')
    registry.revoke(token)
    with pytest.raises(Unauthorized):
        accounts.session(via_token.session_id)
    disabled = accounts.login_password('ivan', PASSWORD, 'ip')
    registry.set_active('ivan', False)
    with pytest.raises(Unauthorized):
        accounts.session(disabled.session_id)


def test_login_lockout_per_user_and_ip(tmp_path):
    clock = Clock()
    registry = Registry(tmp_path)
    accounts = Accounts(registry, AccountPolicy(max_failures=3, lock_seconds=300, max_failures_per_ip=10), clock=clock)
    registry.add_user('ivan', 'Ivan')
    accounts.redeem_invite('ivan', accounts.issue_invite('ivan').code, PASSWORD, 'setup')
    for _ in range(3):
        with pytest.raises(Unauthorized):
            accounts.login_password('ivan', 'wrong password!', '10.0.0.1')
    with pytest.raises(RateLimited):
        accounts.login_password('ivan', PASSWORD, '10.0.0.1')
    assert accounts.login_password('ivan', PASSWORD, '10.0.0.2').actor.user_id == 'ivan'
    clock.now += 301
    assert accounts.login_password('ivan', PASSWORD, '10.0.0.1').actor.user_id == 'ivan'
    for index in range(10):
        with pytest.raises(Unauthorized):
            accounts.login_password(f'ghost{index}', 'wrong password!', '10.0.0.9')
    with pytest.raises(RateLimited):
        accounts.login_password('ivan', PASSWORD, '10.0.0.9')
    with pytest.raises(RateLimited):
        accounts.login_token('anything', '10.0.0.9')
    assert any(e['action'] == 'login_locked' for e in registry.audit_events(limit=200))


def test_http_login_cookie_flags_csrf_and_logout(world):
    client = world['client']
    assert client.get('/dashboard/api/session').status_code == 401
    page = client.get('/dashboard/')
    assert page.status_code == 200
    assert "script-src 'self'" in page.headers['content-security-policy']
    assert '<script>' not in page.text and 'style=' not in page.text
    assert client.get('/dashboard/static/app.js').headers['content-type'].startswith('text/javascript')
    assert client.get('/dashboard/static/..%2Fregistry.py').status_code == 404
    reply = client.post('/dashboard/api/login', json={'user_id': 'dev', 'password': PASSWORD})
    cookie = reply.headers['set-cookie']
    assert 'tam_session=' in cookie and 'HttpOnly' in cookie and 'SameSite=strict' in cookie
    assert 'Secure' not in cookie
    csrf = reply.json()['csrf']
    assert client.post('/dashboard/api/tokens', json={'client': 'x'}).status_code == 403
    assert client.post('/dashboard/api/tokens', json={'client': 'x'}, headers={'X-CSRF-Token': 'forged'}).status_code == 403
    assert client.post('/dashboard/api/tokens', content='client=x', headers={
        'X-CSRF-Token': csrf, 'Content-Type': 'application/x-www-form-urlencoded'}).status_code == 400
    assert client.post('/dashboard/api/tokens', json={'client': 'x'}, headers={
        'X-CSRF-Token': csrf, 'Origin': 'https://evil.invalid'}).status_code == 403
    created = client.post('/dashboard/api/tokens', json={'client': 'laptop'}, headers={'X-CSRF-Token': csrf})
    assert created.status_code == 200 and created.json()['token']
    listed = client.get('/dashboard/api/tokens').json()
    assert created.json()['token'] not in json.dumps(listed)
    assert client.post('/dashboard/api/logout', headers={'X-CSRF-Token': csrf}).status_code == 200
    assert client.get('/dashboard/api/session').status_code == 401
    assert client.post('/dashboard/api/login', json={'user_id': 'dev', 'password': 'nope nope nope'}).status_code == 401
    assert client.post('/dashboard/api/login', json={'user_id': 'nobody', 'password': 'nope nope nope'}).json()['error'] == \
        'Invalid user ID or password'
    snapshot = world['dashboard'].metrics.snapshot()['counters']
    assert any(c['name'] == 'login' and c['labels'] == {'method': 'password', 'outcome': 'unauthorized'} for c in snapshot)
    assert any(c['name'] == 'csrf_rejected' for c in snapshot)


def test_https_cookie_uses_host_prefix_and_secure(tmp_path):
    registry, accounts, _settings, _pool, _dashboard, app = build(tmp_path / 'root')
    registry.add_user('dev', 'Dev')
    accounts.redeem_invite('dev', accounts.issue_invite('dev').code, PASSWORD, 'ip')
    with TestClient(app, base_url='https://testserver') as client:
        reply = client.post('/dashboard/api/login', json={'user_id': 'dev', 'password': PASSWORD})
        assert reply.headers['set-cookie'].startswith('__Host-tam_session=')
        assert 'Secure' in reply.headers['set-cookie']
        assert client.get('/dashboard/api/session').json()['user']['user_id'] == 'dev'


def test_http_invite_redeem_and_password_change(world):
    client, accounts = world['client'], world['accounts']
    world['registry'].add_user('newbie', 'Newbie')
    code = accounts.issue_invite('newbie').code
    reply = client.post('/dashboard/api/invite/redeem', json={'user_id': 'newbie', 'code': code, 'password': PASSWORD})
    assert reply.status_code == 200 and reply.json()['user']['password_set']
    assert client.post('/dashboard/api/invite/redeem', json={'user_id': 'newbie', 'code': code,
                                                             'password': PASSWORD}).status_code == 401
    headers = sign_in(client, 'newbie')
    assert client.post('/dashboard/api/password', json={'current': 'wrong password', 'new': 'another good passphrase'},
                       headers=headers).status_code == 401
    assert client.post('/dashboard/api/password', json={'current': PASSWORD, 'new': 'another good passphrase'},
                       headers=headers).status_code == 200
    assert client.get('/dashboard/api/session').status_code == 200
    client.cookies.clear()
    assert client.post('/dashboard/api/login', json={'user_id': 'newbie', 'password': PASSWORD}).status_code == 401
    sign_in(client, 'newbie', 'another good passphrase')


def test_http_login_lockout(world):
    client = world['client']
    for _ in range(5):
        assert client.post('/dashboard/api/login', json={'user_id': 'dev', 'password': 'bad password!'}).status_code == 401
    locked = client.post('/dashboard/api/login', json={'user_id': 'dev', 'password': PASSWORD})
    assert locked.status_code == 429 and locked.json()['code'] == 'rate_limited'


MATRIX = [
    ('GET', '/dashboard/api/session', None, set(USERS)),
    ('GET', '/dashboard/api/tokens', None, set(USERS)),
    ('POST', '/dashboard/api/tokens', {'client': 'matrix'}, set(USERS)),
    ('GET', '/dashboard/api/teams/eng/people', None, {'root', 'boss', 'audit'}),
    ('GET', '/dashboard/api/teams/ops/people', None, {'root', 'audit'}),
    ('GET', '/dashboard/api/company', None, {'root', 'audit'}),
    ('GET', '/dashboard/api/admin/users', None, {'root'}),
    ('POST', '/dashboard/api/admin/users', {'id': 'fresh', 'name': 'Fresh'}, {'root'}),
    ('POST', '/dashboard/api/admin/users/dev/invite', {}, {'root'}),
    ('POST', '/dashboard/api/admin/users/other/active', {'active': True}, {'root'}),
    ('POST', '/dashboard/api/admin/users/other/role', {'org_role': 'member'}, {'root'}),
    ('GET', '/dashboard/api/admin/teams', None, {'root'}),
    ('POST', '/dashboard/api/admin/teams', {'id': 'fresh', 'name': 'Fresh'}, {'root'}),
    ('POST', '/dashboard/api/admin/teams/ops/rename', {'name': 'Ops'}, {'root'}),
    ('POST', '/dashboard/api/admin/teams/missing/delete', {}, {'root'}),
    ('POST', '/dashboard/api/admin/membership', {'user_id': 'dev', 'team_id': 'eng', 'role': 'editor'}, {'root'}),
    ('GET', '/dashboard/api/admin/tokens', None, {'root'}),
    ('POST', '/dashboard/api/admin/tokens/revoke', {'id': '0' * 64}, {'root'}),
    ('GET', '/dashboard/api/admin/audit', None, {'root'}),
    ('GET', '/dashboard/api/admin/settings', None, {'root'}),
    ('POST', '/dashboard/api/admin/settings', {'values': {'MEMORY_LLM_MODEL': 'qwen2.5:7b'}}, {'root'}),
    ('POST', '/dashboard/api/admin/settings/test', {'target': 'embed'}, {'root'}),
    ('GET', '/dashboard/api/admin/backup', None, {'root'}),
    ('GET', '/dashboard/api/admin/metrics', None, {'root'}),
]


def test_authorization_matrix(world):
    client = world['client']
    for user_id in USERS:
        headers = sign_in(client, user_id)
        for method, path, body, allowed in MATRIX:
            reply = client.request(method, path, json=body, headers=headers if method == 'POST' else None)
            if user_id in allowed:
                assert reply.status_code not in (401, 403), (user_id, path, reply.text)
            else:
                assert reply.status_code == 403, (user_id, path, reply.status_code, reply.text)
    client.cookies.clear()
    for method, path, body, _allowed in MATRIX:
        assert client.request(method, path, json=body).status_code == 401, path
    actions = {e['action'] for e in world['registry'].audit_events(limit=200) if e['actor'] == 'root'}
    assert {'user_created', 'invite_issued', 'user_enabled', 'org_role:member', 'team_created', 'team_renamed',
            'membership:editor', 'setting_updated', 'provider_tested'} <= actions


def test_session_overview_sections_by_role(world):
    client = world['client']
    personal = {'overview', 'memory', 'learning', 'reports', 'access'}
    admin = {'admin-users', 'admin-teams', 'admin-tokens', 'audit', 'settings', 'system', 'database'}
    expected = {'root': personal | {'department', 'company'} | admin, 'boss': personal | {'department'},
                'dev': personal, 'audit': personal | {'department', 'company'}}
    labels = {'root': 'Superadmin', 'boss': 'Manager · Engineering', 'dev': 'Editor · Engineering',
              'audit': 'Company viewer'}
    for user_id, sections in expected.items():
        sign_in(client, user_id)
        overview = client.get('/dashboard/api/session').json()
        assert {s['id'] for s in overview['sections']} == sections
        assert overview['user']['org_role'] == USERS[user_id]
        assert overview['user']['role_label'] == labels[user_id]
        assert {s['group'] for s in overview['sections'] if s['id'] in admin} <= {'administration'}
        assert all(s['icon'] for s in overview['sections'])
    assert [t['team_id'] for t in overview['viewableTeams']] == ['eng', 'ops']


def test_admin_user_lifecycle_through_http(world):
    client, registry = world['client'], world['registry']
    headers = sign_in(client, 'root')
    invite = client.post('/dashboard/api/admin/users', json={'id': 'lena', 'name': 'Lena', 'org_role': 'company_viewer'},
                         headers=headers).json()
    assert invite['user_id'] == 'lena' and len(invite['code']) == 24
    assert registry.org_role('lena') == 'company_viewer'
    assert client.post('/dashboard/api/admin/users', json={'id': 'lena', 'name': 'Lena'}, headers=headers).status_code == 409
    assert client.post('/dashboard/api/admin/users/root/role', json={'org_role': 'member'}, headers=headers).status_code == 409
    assert client.post('/dashboard/api/admin/teams', json={'id': 'hr', 'name': 'HR'}, headers=headers).status_code == 200
    assert client.post('/dashboard/api/admin/membership', json={'user_id': 'lena', 'team_id': 'hr', 'role': 'manager'},
                       headers=headers).status_code == 200
    assert registry.team_role('lena', 'hr') == 'manager'
    assert client.post('/dashboard/api/admin/teams/hr/delete', json={}, headers=headers).status_code == 409
    client.post('/dashboard/api/admin/membership', json={'user_id': 'lena', 'team_id': 'hr', 'role': None}, headers=headers)
    assert client.post('/dashboard/api/admin/teams/hr/delete', json={}, headers=headers).status_code == 200
    token = registry.issue_token('dev', 'cursor')
    listed = client.get('/dashboard/api/admin/tokens?user_id=dev').json()
    assert [t['client'] for t in listed] == ['cursor'] and token not in json.dumps(listed)
    assert client.post('/dashboard/api/admin/tokens/revoke', json={'id': listed[0]['id']}, headers=headers).status_code == 200
    with pytest.raises(Unauthorized):
        registry.authenticate(token)
    dev_session = world['accounts'].login_password('dev', PASSWORD, 'elsewhere')
    assert client.post('/dashboard/api/admin/users/dev/active', json={'active': False}, headers=headers).status_code == 200
    with pytest.raises(Unauthorized):
        world['accounts'].session(dev_session.session_id)
    page = client.get('/dashboard/api/admin/audit?limit=3').json()
    assert len(page['events']) == 3 and page['next'] == page['events'][-1]['id']
    older = client.get(f"/dashboard/api/admin/audit?limit=3&before={page['next']}").json()
    assert older['events'][0]['id'] < page['next']


def test_own_token_revocation_is_owner_scoped(world):
    client, registry = world['client'], world['registry']
    foreign = registry.issue_token('other', 'phone')
    foreign_id = Registry.digest(foreign)
    headers = sign_in(client, 'dev')
    assert client.post('/dashboard/api/tokens/revoke', json={'id': foreign_id}, headers=headers).status_code == 400
    assert registry.authenticate(foreign).user_id == 'other'


SECRET = 'sk-test-plaintext-should-never-leak-1234WXYZ'


def test_secrets_encrypted_at_rest_and_never_returned(world):
    client, registry = world['client'], world['registry']
    headers = sign_in(client, 'root')
    reply = client.post('/dashboard/api/admin/settings', headers=headers, json={'values': {
        'OPENAI_API_KEY': SECRET, 'MEMORY_LLM_PROVIDER': 'openai', 'MEMORY_LLM_MODEL': 'gpt-4o-mini'}})
    assert reply.status_code == 200 and SECRET not in reply.text
    assert SECRET.encode() not in stored_identity_bytes(registry)
    for suffix in ('-wal', '-journal'):
        sidecar = registry.path.with_name(registry.path.name + suffix)
        if sidecar.exists():
            assert SECRET.encode() not in sidecar.read_bytes()
    view = client.get('/dashboard/api/admin/settings')
    assert SECRET not in view.text
    item = next(i for i in view.json()['settings'] if i['key'] == 'OPENAI_API_KEY')
    assert item == {**item, 'source': 'web', 'is_set': True, 'value': None, 'hint': '••••WXYZ'}
    assert next(i for i in view.json()['settings'] if i['key'] == 'MEMORY_LLM_MODEL')['value'] == 'gpt-4o-mini'
    assert world['settings'].overrides()['OPENAI_API_KEY'] == SECRET
    audit = json.dumps(client.get('/dashboard/api/admin/audit?limit=200').json())
    assert SECRET not in audit and 'OPENAI_API_KEY' in audit
    assert (registry.root / MASTER_KEY_FILE).stat().st_mode & 0o777 == 0o600
    assert client.post('/dashboard/api/admin/settings', headers=headers,
                       json={'values': {'MEMORY_LLM_PROVIDER': 'gemini'}}).status_code == 400
    assert client.post('/dashboard/api/admin/settings', headers=headers,
                       json={'values': {'MEMORY_LLM_API_BASE': 'https://user:pw@example.invalid'}}).status_code == 400
    assert client.post('/dashboard/api/admin/settings', headers=headers,
                       json={'values': {'TAM_MEMORY_DIR': '/etc'}}).status_code == 400
    client.post('/dashboard/api/admin/settings', headers=headers, json={'values': {'OPENAI_API_KEY': None}})
    assert 'OPENAI_API_KEY' not in world['settings'].overrides()


def test_wrong_master_key_reports_unreadable_secret(tmp_path):
    from cryptography.fernet import Fernet
    registry = Registry(tmp_path)
    SettingsStore(registry, Fernet(Fernet.generate_key()), {}).update({'ANTHROPIC_API_KEY': SECRET})
    other = SettingsStore(registry, Fernet(Fernet.generate_key()), {})
    item = next(v for v in other.view() if v.key == 'ANTHROPIC_API_KEY')
    assert item.is_set and not item.readable and item.hint is None
    assert 'ANTHROPIC_API_KEY' not in other.overrides()


def test_precedence_web_over_env_over_default(tmp_path):
    registry = Registry(tmp_path)
    store = SettingsStore(registry, load_cipher(tmp_path, {}), {'MEMORY_LLM_MODEL': 'env-model', 'OLLAMA_URL': 'http://env:1'})
    views = {v.key: v for v in store.view()}
    assert (views['MEMORY_LLM_MODEL'].source, views['MEMORY_LLM_MODEL'].value) == ('env', 'env-model')
    assert views['MEMORY_EMBED_MODEL'].source == 'default'
    store.update({'MEMORY_LLM_MODEL': 'web-model'})
    assert store.effective()['MEMORY_LLM_MODEL'] == 'web-model'
    assert store.effective()['OLLAMA_URL'] == 'http://env:1'
    store.update({'MEMORY_LLM_MODEL': None})
    assert store.effective()['MEMORY_LLM_MODEL'] == 'env-model'


def test_master_key_from_env_and_rejects_loose_file(tmp_path):
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    load_cipher(tmp_path, {'TAM_TEAM_MASTER_KEY': key})
    assert not (tmp_path / MASTER_KEY_FILE).exists()
    with pytest.raises(ValueError):
        load_cipher(tmp_path, {'TAM_TEAM_MASTER_KEY': 'not-a-key'})
    load_cipher(tmp_path, {})
    (tmp_path / MASTER_KEY_FILE).chmod(0o644)
    with pytest.raises(PermissionError):
        load_cipher(tmp_path, {})


def test_provider_check_sends_key_only_to_provider_and_never_reports_it(tmp_path):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(401 if request.headers.get('authorization') != 'Bearer ' + SECRET else 200, json={'data': []})

    registry, accounts, _settings, _pool, _dashboard, app = build(tmp_path / "root", transport=httpx.MockTransport(handler))
    registry.add_user('root', 'Root')
    registry.set_org_role('root', 'superadmin')
    accounts.redeem_invite('root', accounts.issue_invite('root').code, PASSWORD, 'ip')
    with TestClient(app) as client:
        headers = sign_in(client, 'root')
        client.post('/dashboard/api/admin/settings', headers=headers, json={'values': {
            'MEMORY_LLM_PROVIDER': 'openai', 'OPENAI_API_KEY': SECRET}})
        ok = client.post('/dashboard/api/admin/settings/test', headers=headers, json={'target': 'llm'})
        assert ok.json()['ok'] is True and SECRET not in ok.text
        assert str(seen[-1].url) == 'https://api.openai.com/v1/models'
        client.post('/dashboard/api/admin/settings', headers=headers, json={'values': {'OPENAI_API_KEY': SECRET + 'x'}})
        failed = client.post('/dashboard/api/admin/settings/test', headers=headers, json={'target': 'llm'}).json()
        assert failed == {**failed, 'ok': False, 'detail': 'HTTP 401 (authentication failed)'}
        assert SECRET not in json.dumps(failed)


def test_provider_check_timeouts_and_resolution():
    def timeout(_request):
        raise httpx.ConnectTimeout('slow', request=_request)
    endpoint = provider_check.resolve_llm({'MEMORY_LLM_PROVIDER': 'ollama', 'OLLAMA_URL': 'http://ollama.invalid:11434/'})
    assert (endpoint.base, endpoint.key) == ('http://ollama.invalid:11434', None)
    assert provider_check.check(endpoint, httpx.MockTransport(timeout)).detail == 'Timed out'
    assert provider_check.check(provider_check.resolve_embed({})).ok
    missing = provider_check.check(provider_check.resolve_llm({'MEMORY_LLM_PROVIDER': 'anthropic'}))
    assert (missing.ok, missing.detail) == (False, 'No API key configured')
    anthropic = provider_check.resolve_llm({'MEMORY_LLM_PROVIDER': 'auto', 'ANTHROPIC_API_KEY': 'k'})
    url, headers = provider_check.probe_request(anthropic)
    assert (url, headers['x-api-key']) == ('https://api.anthropic.com/v1/models', 'k')


def test_dashscope_embedding_provider_is_selectable_and_checked(monkeypatch):
    import config
    from team_memory import settings as settings_module
    assert 'dashscope' in settings_module.EMBED_PROVIDERS
    card = next(p for p in settings_module.PROVIDERS if p.target == 'embed' and p.id == 'dashscope')
    assert all(field in settings_module.SPECS for field in card.fields)
    assert settings_module.SPECS['DASHSCOPE_API_KEY'].kind == 'secret'
    unset = provider_check.check(provider_check.resolve_embed({'MEMORY_EMBED_PROVIDER': 'dashscope'}))
    assert (unset.ok, unset.detail) == (False, 'No API key configured')
    monkeypatch.delenv('MEMORY_EMBED_API_BASE', raising=False)
    endpoint = provider_check.resolve_embed({'MEMORY_EMBED_PROVIDER': 'dashscope', 'DASHSCOPE_API_KEY': 'k'})
    assert endpoint.base == config.get_embed_api_base('dashscope').rstrip('/')
    url, headers = provider_check.probe_request(endpoint)
    assert (url, headers['Authorization']) == (endpoint.base + '/models', 'Bearer k')


@pytest.mark.parametrize('env', [
    {}, {'MEMORY_LLM_PROVIDER': 'openai', 'OPENAI_API_KEY': 'a'},
    {'MEMORY_LLM_PROVIDER': 'auto', 'ANTHROPIC_API_KEY': 'b'},
    {'MEMORY_LLM_PROVIDER': 'openai-compatible', 'MEMORY_LLM_API_BASE': 'http://x/v1/', 'MEMORY_LLM_API_KEY': 'c'},
    {'MEMORY_LLM_PROVIDER': 'ollama', 'OLLAMA_URL': 'http://o:1'},
])
def test_provider_resolution_matches_runtime_config(monkeypatch, env):
    import config
    for name in ('MEMORY_LLM_PROVIDER', 'MEMORY_LLM_API_BASE', 'MEMORY_LLM_API_KEY', 'OPENAI_API_KEY',
                 'ANTHROPIC_API_KEY', 'OLLAMA_URL', 'COHERE_API_KEY'):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    endpoint = provider_check.resolve_llm(env)
    assert endpoint.provider == config.get_llm_provider()
    assert endpoint.base == config.get_llm_api_base()
    assert endpoint.key == config.get_llm_api_key()


def test_worker_env_propagation_and_recycle(tmp_path, monkeypatch):
    registry = Registry(tmp_path)
    registry.add_user('dev', 'Dev')
    settings = SettingsStore(registry, load_cipher(tmp_path, {}), {})
    settings.update({'MEMORY_LLM_MODEL': 'propagated-model', 'ANTHROPIC_API_KEY': SECRET})
    pool = WorkerPool(tmp_path, environment=settings.overrides)
    started = []

    class FakeConnection:
        def __init__(self):
            self.sent = None

        def send(self, payload):
            self.sent = payload

        def poll(self, _timeout):
            return True

        def recv(self):
            return '{"data": {"ok": true}}'

        def close(self):
            return None

    class FakeProcess:
        def __init__(self, target, args, daemon):
            self.target, self.args = target, args
            started.append(self)

        def start(self):
            return None

        def is_alive(self):
            return False

        def join(self, _timeout=None):
            return None

        def close(self):
            return None

    monkeypatch.setattr(pool.context, 'Process', FakeProcess)
    monkeypatch.setattr(pool.context, 'Pipe', lambda: (FakeConnection(), FakeConnection()))
    from team_memory.contracts import Work
    token = registry.issue_token('dev', 'test')
    actor = registry.authenticate(token)
    work = Work(actor=actor, workspace=registry.authorize(actor, Scope(), False), operation='memory_get', arguments={'id': 1})
    assert pool.invoke(work, token) == {'ok': True}
    assert started[-1].args[2] == {'MEMORY_LLM_MODEL': 'propagated-model', 'ANTHROPIC_API_KEY': SECRET}
    assert pool.recycle() == 1 and not pool.workers
    settings.update({'MEMORY_LLM_MODEL': 'second-model'})
    pool.invoke(work, lambda: registry.authenticate(token))
    assert started[-1].args[2]['MEMORY_LLM_MODEL'] == 'second-model'


def test_worker_serve_applies_environment_before_runtime(monkeypatch):
    from team_memory import worker
    captured = {}
    monkeypatch.setattr(worker, 'serve_locked', lambda _c, _r, _database=None: captured.update(model=__import__('os').environ.get('MEMORY_LLM_MODEL')))
    monkeypatch.setattr(worker, 'ServerLease', lambda _path: __import__('contextlib').nullcontext())
    monkeypatch.delenv('MEMORY_LLM_MODEL', raising=False)
    worker.serve(None, '/unused', {'MEMORY_LLM_MODEL': 'from-settings'})
    assert captured['model'] == 'from-settings'


def test_settings_update_recycles_live_pool(world, monkeypatch):
    calls = []
    monkeypatch.setattr(world['pool'], 'recycle', lambda: calls.append(1) or 0)
    headers = sign_in(world['client'], 'root')
    reply = world['client'].post('/dashboard/api/admin/settings', headers=headers,
                                 json={'values': {'MEMORY_LLM_ENABLED': 'false'}})
    assert reply.json() == {'changed': ['MEMORY_LLM_ENABLED'], 'workers_recycled': 0} and calls == [1]


def test_bearer_paths_unchanged_and_isolated_from_sessions(world):
    client, registry = world['client'], world['registry']
    token = registry.issue_token('dev', 'codex')
    bearer = {'Authorization': 'Bearer ' + token}
    reply = client.post('/api/call', headers=bearer, json={'name': 'memory_scopes'})
    assert reply.status_code == 200 and reply.json()['actor']['user_id'] == 'dev'
    sign_in(client, 'dev')
    assert client.post('/api/call', json={'name': 'memory_scopes'}).status_code == 401
    assert client.post('/mcp/', json={}).status_code == 401
    client.cookies.clear()
    assert client.get('/dashboard/api/session', headers=bearer).status_code == 401
    init = client.post('/mcp/', headers={**bearer, 'Accept': 'application/json, text/event-stream'}, json={
        'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
            'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 't', 'version': '1'}}})
    assert init.status_code == 200 and 'protocolVersion' in init.json()['result']
    assert client.get('/').status_code == 200
    assert client.get('/healthz').json()['status'] == 'ok'


def test_section_registry_extension(world):
    from starlette.routing import Route

    from team_memory.dashboard import session_endpoint
    with pytest.raises(ValueError):
        register(Section(id='memory', title='Dup', capability=Capability.authenticated, script='/x.js', mount='X.y'))
    with pytest.raises(ValueError):
        Section(id='bad', title='Bad', capability='team_people', script='https://cdn.invalid/x.js', mount='X.y')
    section = Section(id='probe', title='Probe', capability='team_people', script='/learning/static/probe.js',
                      stylesheet='/learning/static/probe.css', mount='TamProbe.mount', order=60)
    register(section)
    try:
        registry = world['registry']
        boss = registry.authenticate(registry.issue_token('boss', 't'))
        dev = registry.authenticate(registry.issue_token('dev', 't'))
        assert section in visible_sections(registry, boss) and section not in visible_sections(registry, dev)

        @session_endpoint(Capability.team_people)
        async def progress(request, actor):
            return {'viewer': actor.user_id, 'team': request.path_params['team_id']}

        world['client'].app.app.app.router.routes.append(Route('/learning/probe/{team_id}', progress, methods=['GET', 'POST']))
        client = world['client']
        headers = sign_in(client, 'boss')
        assert client.get('/learning/probe/eng').json() == {'viewer': 'boss', 'team': 'eng'}
        assert client.post('/learning/probe/eng', json={}).status_code == 403
        assert client.post('/learning/probe/eng', json={}, headers=headers).status_code == 200
        sign_in(client, 'dev')
        assert client.get('/learning/probe/eng').status_code == 403
        client.cookies.clear()
        assert client.get('/learning/probe/eng').status_code == 401
    finally:
        SECTIONS.remove(section)


def test_cli_bootstrap_user_role_and_invite(tmp_path):
    import os
    root = tmp_path / 'cli-root'
    env = {**os.environ, 'PYTHONPATH': str(ROOT / 'src')}

    def cli(*args):
        return subprocess.run([sys.executable, '-m', 'team_memory.cli', '--root', str(root), *args],
                              env=env, capture_output=True, text=True, timeout=60, check=False)

    first = cli('bootstrap-admin', 'boss', 'The Boss')
    assert first.returncode == 0, first.stderr
    code = first.stdout.split(': ', 1)[1].split()[0]
    registry = Registry(root)
    assert registry.org_role('boss') == 'superadmin'
    Accounts(registry).redeem_invite('boss', code, PASSWORD, 'ip')
    assert cli('bootstrap-admin', 'second', 'Second').returncode != 0
    assert cli('user-add', 'ivan', 'Ivan').returncode == 0
    assert cli('user-role', 'ivan', 'company_viewer').returncode == 0
    assert registry.org_role('ivan') == 'company_viewer'
    assert cli('member', 'ivan', 'eng', 'manager').returncode != 0
    assert cli('team-add', 'eng', 'Engineering').returncode == 0
    assert cli('member', 'ivan', 'eng', 'manager').returncode == 0
    assert registry.team_role('ivan', 'eng') == 'manager'
    reset = cli('invite', 'ivan')
    assert reset.returncode == 0 and 'Invite code for ivan' in reset.stdout
    assert any(e['actor'] == 'cli' and e['action'] == 'invite_issued' for e in registry.audit_events())


def test_packaging_ships_dashboard_assets():
    import tomllib
    from fnmatch import fnmatch
    data = tomllib.loads((ROOT / 'pyproject.toml').read_text())
    patterns = data['tool']['setuptools']['package-data']['src']
    static = ROOT / 'src' / 'team_memory' / 'static'
    for asset in static.rglob('*'):
        if asset.is_file():
            relative = asset.relative_to(ROOT / 'src').as_posix()
            assert any(fnmatch(relative, p) for p in patterns), relative
    assert {p.name for p in (static / 'fonts').glob('*-license.txt')} == {'inter-license.txt', 'jetbrains-mono-license.txt'}
    for section in SECTIONS:
        if section.script.startswith('/dashboard/static/'):
            assert (static / section.script.rsplit('/', 1)[1]).is_file()
    for script in static.glob('*.js'):
        text = script.read_text()
        assert 'innerHTML' not in text and 'eval(' not in text and 'console.log' not in text


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def test_real_memory_privacy_oversight_and_activity(world, monkeypatch):
    monkeypatch.setenv('MEMORY_LLM_ENABLED', 'false')
    monkeypatch.setenv('MEMORY_QUALITY_GATE_ENABLED', 'false')
    monkeypatch.setenv('MEMORY_MODE', 'fast')
    client = world['client']

    def memory(headers, name, arguments):
        return client.post('/dashboard/api/memory', headers=headers, json={'name': name, 'arguments': arguments})

    team = {'kind': 'team', 'team_id': 'eng'}
    dev = sign_in(client, 'dev')
    private = memory(dev, 'memory_save', {'content': 'Private nebula passphrase belongs to Dev alone.'})
    assert private.status_code == 200, private.text
    assert private.json()['data']['created_by'] == {'user_id': 'dev', 'display_name': 'Dev', 'client': 'dashboard',
                                                    'org_role': 'member'}
    saved = memory(dev, 'memory_save', {'content': 'Engineering nebula deploys run on Fridays.', 'scope': team})
    assert saved.status_code == 200, saved.text
    root = sign_in(client, 'root')
    recall = memory(root, 'memory_recall', {'query': 'nebula passphrase', 'limit': 20})
    assert 'belongs to Dev' not in recall.text
    personal = memory(root, 'memory_export', {'scope': {'kind': 'personal'}})
    assert 'belongs to Dev' not in personal.text
    assert memory(root, 'memory_get', {'id': private.json()['data']['id']}).status_code in (400, 409)
    browsed = memory(root, 'memory_export', {'scope': team})
    assert 'Fridays' in browsed.text
    auditor = sign_in(client, 'audit')
    assert 'Fridays' in memory(auditor, 'memory_export', {'scope': team}).text
    assert memory(auditor, 'memory_save', {'content': 'Viewer must not write here at all.', 'scope': team}).status_code == 403
    boss = sign_in(client, 'boss')
    assert memory(boss, 'memory_save', {'content': 'Manager note: nebula review every sprint.', 'scope': team}).status_code == 200
    people = {m['user_id']: m for m in client.get('/dashboard/api/teams/eng/people').json()['members']}
    assert people['dev']['saves'] == 1 and people['boss']['saves'] == 1 and people['dev']['last_activity']
    other = sign_in(client, 'other')
    assert memory(other, 'memory_export', {'scope': team}).status_code == 403
    overview = sign_in(client, 'audit') and client.get('/dashboard/api/company').json()
    assert {t['team_id']: t['saves'] for t in overview} == {'eng': 2, 'ops': 0}


def test_session_check_writes_nothing_during_maintenance(tmp_path):
    clock = Clock()
    registry = Registry(tmp_path)
    registry.add_user('dev', 'Dev')
    accounts = Accounts(registry, AccountPolicy(), clock=clock)
    session = accounts.redeem_invite('dev', accounts.issue_invite('dev').code, PASSWORD, 'ip')

    def last_seen():
        with identity(registry) as db:
            return db.execute('SELECT last_seen_at FROM sessions').fetchone()[0]

    opened = last_seen()
    migrating = threading.Event()
    migrating.set()
    registry.plane.watch_maintenance(migrating.is_set)
    clock.now += 60
    assert accounts.session(session.session_id).actor.user_id == 'dev'
    assert last_seen() == opened
    migrating.clear()
    accounts.session(session.session_id)
    assert last_seen() == clock.now

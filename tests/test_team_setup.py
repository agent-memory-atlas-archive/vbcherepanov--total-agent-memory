import logging
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from team_memory.accounts import AccountPolicy, Accounts
from team_memory.app import create_app
from team_memory.contracts import (
    Conflict,
    NotFound,
    RateLimited,
    SetupComplete,
    Unauthorized,
)
from team_memory.dashboard import CSP
from team_memory.dashboard_service import DashboardService
from team_memory.metrics import Metrics
from team_memory.registry import Registry
from team_memory.service import MemoryService
from team_memory.settings import SettingsStore, load_cipher
from team_memory.setup import SUPPORT_URL, SetupPolicy, SetupService
from team_memory.worker import WorkerPool

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = 'correct horse battery staple'
CODE = re.compile(r'Setup code: ([A-Z0-9-]+)')


class Clock:
    def __init__(self):
        self.now = 2_000_000.0

    def __call__(self):
        return self.now


def build(root: Path, clock=None, policy=None):
    clock = clock or Clock()
    registry = Registry(root)
    accounts = Accounts(registry, AccountPolicy(), clock=clock)
    settings = SettingsStore(registry, load_cipher(root, {}), {})
    pool = WorkerPool(registry.root, maximum=1, environment=settings.overrides)
    metrics = Metrics()
    dashboard = DashboardService(registry, accounts, MemoryService(registry, pool), settings, metrics)
    setup = SetupService(registry, accounts, metrics, policy or SetupPolicy(), clock)
    return registry, setup, create_app(dashboard.memory, dashboard, setup=setup, base_url='http://tam.test:3737')


def complete_body(token, **extra):
    return {'token': token, 'company_name': 'Acme Corp', 'public_url': 'https://memory.acme.test',
            'user_id': 'alice', 'name': 'Alice Admin', 'password': PASSWORD, **extra}


@pytest.fixture
def fresh(tmp_path, caplog):
    clock = Clock()
    registry, setup, app = build(tmp_path / 'root', clock)
    with caplog.at_level(logging.WARNING, logger='team_memory.setup'), TestClient(app) as client:
        match = CODE.search(caplog.text)
        assert match, caplog.text
        yield {'registry': registry, 'setup': setup, 'client': client, 'token': match.group(1), 'clock': clock,
               'log': caplog.text}


def test_startup_prints_single_use_code_and_url(fresh):
    assert '#setup=' + fresh['token'] in fresh['log']
    assert 'http://tam.test:3737/dashboard/' in fresh['log']
    status = fresh['client'].get('/dashboard/api/setup')
    assert status.status_code == 200
    assert status.json()['required'] is True and status.json()['token_active'] is True
    assert status.headers['content-security-policy'] == CSP
    with fresh['registry'].connect() as db:
        stored = [row[0] for row in db.execute('SELECT digest FROM setup_tokens')]
    assert stored == [Registry.digest(fresh['token'].replace('-', ''))]
    assert fresh['token'] not in str(fresh['registry'].audit_events())


def test_web_wizard_end_to_end(fresh):
    client, token, registry = fresh['client'], fresh['token'], fresh['registry']
    assert client.post('/dashboard/api/setup/verify', json={'token': 'WRONG-CODE'}).status_code == 401
    assert client.post('/dashboard/api/setup/verify', json={'token': token.lower()}).json() == {'valid': True}
    short = client.post('/dashboard/api/setup/complete', json=complete_body(token, password='short'))
    assert short.status_code == 400 and '12' in short.json()['error']
    assert client.post('/dashboard/api/setup/verify', json={'token': token}).status_code == 200

    done = client.post('/dashboard/api/setup/complete', json=complete_body(token))
    assert done.status_code == 200, done.text
    body = done.json()
    assert body['user']['user_id'] == 'alice' and body['user']['org_role'] == 'superadmin'
    assert body['organization'] == {'name': 'Acme Corp'} and body['setup_pending'] is True
    assert body['support_url'] == SUPPORT_URL and body['support_line'].endswith(SUPPORT_URL)
    assert 'tam_session' in done.headers['set-cookie'] and 'HttpOnly' in done.headers['set-cookie']
    headers = {'X-CSRF-Token': body['csrf']}

    assert client.post('/dashboard/api/admin/teams', headers=headers,
                       json={'id': 'eng', 'name': 'Engineering'}).status_code == 200
    saved = client.post('/dashboard/api/admin/settings', headers=headers,
                        json={'values': {'MEMORY_LLM_PROVIDER': 'openai', 'OPENAI_API_KEY': 'sk-wizard-secret-0001'}})
    assert saved.status_code == 200, saved.text
    organization = client.get('/dashboard/api/admin/organization').json()
    assert organization == {'name': 'Acme Corp', 'public_url': 'https://memory.acme.test', 'setup_state': 'admin_created'}
    moved = client.post('/dashboard/api/admin/organization', headers=headers, json={'public_url': 'https://tam.acme.test/'})
    assert moved.json()['public_url'] == 'https://tam.acme.test'
    assert client.post('/dashboard/api/admin/setup/finish', headers=headers).json()['setup_state'] == 'complete'
    session = client.get('/dashboard/api/session').json()
    assert session['setup_pending'] is False and session['organization']['name'] == 'Acme Corp'
    assert 'support_url' not in session

    for method, path, payload in (('get', '/dashboard/api/setup', None),
                                  ('post', '/dashboard/api/setup/verify', {'token': token}),
                                  ('post', '/dashboard/api/setup/complete', complete_body(token, user_id='mallory'))):
        reply = getattr(client, method)(path, json=payload) if payload else getattr(client, method)(path)
        assert reply.status_code == 404, path
    assert registry.org_role('alice') == 'superadmin'
    assert Accounts(registry).login_password('alice', PASSWORD, 'ip').actor.user_id == 'alice'
    actions = [(e['action'], e['subject'], e['actor']) for e in registry.audit_events(limit=200)]
    assert ('setup_completed', 'alice', 'alice') in actions
    assert ('organization_updated', 'name', 'alice') in actions
    assert ('organization_updated', 'public_url', 'alice') in actions
    assert 'sk-wizard-secret-0001' not in str(registry.audit_events(limit=200))


def test_code_is_required_single_use_and_expires(tmp_path):
    clock = Clock()
    registry, setup, _app = build(tmp_path / 'root', clock, SetupPolicy(token_ttl_seconds=600))
    with pytest.raises(Unauthorized):
        setup.complete(SetupComplete(**complete_body('NOPE')), 'ip')
    first = setup.issue_token()
    second = setup.issue_token()
    with pytest.raises(Unauthorized):
        setup.verify(first.token, 'ip')
    clock.now += 601
    assert setup.status().token_active is False
    with pytest.raises(Unauthorized):
        setup.verify(second.token, 'ip')
    third = setup.issue_token()
    setup.complete(SetupComplete(**complete_body(third.token)), 'ip')
    with pytest.raises(NotFound):
        setup.verify(third.token, 'ip')
    with pytest.raises(NotFound):
        setup.complete(SetupComplete(**complete_body(third.token, user_id='bob')), 'ip')
    with pytest.raises(Conflict):
        setup.issue_token()
    assert registry.list_users()[0]['id'] == 'alice'


def test_existing_user_id_keeps_the_code_usable(tmp_path):
    registry, setup, _app = build(tmp_path / 'root')
    registry.add_user('alice', 'Legacy Alice')
    issued = setup.issue_token()
    with pytest.raises(Conflict):
        setup.complete(SetupComplete(**complete_body(issued.token)), 'ip')
    setup.complete(SetupComplete(**complete_body(issued.token, user_id='admin')), 'ip')
    assert registry.org_role('admin') == 'superadmin' and registry.org_role('alice') == 'member'


def test_concurrent_completions_create_exactly_one_superadmin(tmp_path):
    registry, setup, _app = build(tmp_path / 'root')
    issued = setup.issue_token()
    results = []

    def attempt(user_id):
        try:
            setup.complete(SetupComplete(**complete_body(issued.token, user_id=user_id)), 'ip-' + user_id)
            results.append('ok')
        except (Unauthorized, NotFound) as exc:
            results.append(exc.code)

    threads = [threading.Thread(target=attempt, args=(f'admin{i}',)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count('ok') == 1
    assert sum(user['org_role'] == 'superadmin' for user in registry.list_users()) == 1


def test_wrong_codes_are_rate_limited_per_ip_and_globally(tmp_path):
    clock = Clock()
    policy = SetupPolicy(max_failures_per_ip=3, max_failures_total=5, lock_seconds=300, failure_window_seconds=300)
    registry, setup, _app = build(tmp_path / 'root', clock, policy)
    issued = setup.issue_token()
    for _ in range(3):
        with pytest.raises(Unauthorized):
            setup.verify('WRONG', '10.0.0.1')
    with pytest.raises(RateLimited):
        setup.verify(issued.token, '10.0.0.1')
    setup.verify(issued.token, '10.0.0.2')
    for ip in ('10.0.0.3', '10.0.0.4'):
        with pytest.raises(Unauthorized):
            setup.verify('WRONG', ip)
    with pytest.raises(RateLimited):
        setup.verify(issued.token, '10.0.0.9')
    clock.now += 301
    setup.verify(issued.token, '10.0.0.1')
    assert any(e['action'] == 'setup_locked' for e in registry.audit_events())


def test_http_rate_limit_and_cross_origin(fresh):
    client = fresh['client']
    for _ in range(5):
        assert client.post('/dashboard/api/setup/verify', json={'token': 'WRONG'}).status_code == 401
    assert client.post('/dashboard/api/setup/verify', json={'token': fresh['token']}).status_code == 429
    fresh['clock'].now += SetupPolicy().lock_seconds + 1
    evil = client.post('/dashboard/api/setup/complete', json=complete_body(fresh['token']),
                       headers={'Origin': 'https://evil.test'})
    assert evil.status_code == 403
    assert not fresh['registry'].has_superadmin()


def test_no_setup_when_superadmin_exists(tmp_path, caplog):
    root = tmp_path / 'root'
    registry = Registry(root)
    registry.add_user('boss', 'Boss')
    registry.set_org_role('boss', 'superadmin')
    _registry, _setup, app = build(root)
    with caplog.at_level(logging.WARNING, logger='team_memory.setup'), TestClient(app) as client:
        assert client.get('/dashboard/api/setup').status_code == 404
        assert client.post('/dashboard/api/setup/verify', json={'token': 'X'}).status_code == 404
    assert 'Setup code' not in caplog.text


def test_organization_admin_endpoints_validate_and_require_superadmin(fresh):
    client = fresh['client']
    body = client.post('/dashboard/api/setup/complete', json=complete_body(fresh['token'])).json()
    headers = {'X-CSRF-Token': body['csrf']}
    bad = client.post('/dashboard/api/admin/organization', headers=headers, json={'public_url': 'ftp://x'})
    assert bad.status_code == 400
    assert client.post('/dashboard/api/admin/organization', headers=headers, json={}).status_code == 400
    invite = client.post('/dashboard/api/admin/users', headers=headers, json={'id': 'bob', 'name': 'Bob'}).json()
    client.cookies.clear()
    bob = client.post('/dashboard/api/invite/redeem', json={'user_id': 'bob', 'code': invite['code'], 'password': PASSWORD})
    assert bob.json()['setup_pending'] is False and bob.json()['organization']['name'] == 'Acme Corp'
    bob_headers = {'X-CSRF-Token': bob.json()['csrf']}
    assert client.post('/dashboard/api/admin/organization', headers=bob_headers, json={'name': 'X'}).status_code == 403
    assert client.post('/dashboard/api/admin/setup/finish', headers=bob_headers).status_code == 403


def test_static_wizard_keeps_csp_strict(fresh):
    client = fresh['client']
    page = client.get('/dashboard/')
    assert page.headers['content-security-policy'] == CSP
    assert "'unsafe-inline'" not in CSP and "'unsafe-eval'" not in CSP
    assert '<style' not in page.text and ' style=' not in page.text
    assert re.search(r'<script(?![^>]*\bsrc=)', page.text) is None
    assert 'id="setup"' in page.text and '/dashboard/static/setup.js' in page.text
    script = client.get('/dashboard/static/setup.js')
    assert script.status_code == 200 and script.headers['content-security-policy'] == CSP
    for forbidden in ('innerHTML', 'outerHTML', 'eval(', 'new Function', "'style'", '.style.', 'console.log'):
        assert forbidden not in script.text, forbidden


def test_cli_setup_token_command(tmp_path):
    root = tmp_path / 'cli-root'
    env = {**os.environ, 'PYTHONPATH': str(ROOT / 'src'), 'HOME': str(tmp_path)}

    def cli(*args):
        return subprocess.run([sys.executable, '-m', 'team_memory.cli', '--root', str(root), *args], env=env,
                              capture_output=True, text=True, timeout=60, check=False)

    issued = cli('setup-token')
    assert issued.returncode == 0, issued.stderr
    code = CODE.search(issued.stdout).group(1)
    registry = Registry(root)
    SetupService(registry, Accounts(registry), Metrics()).verify(code, 'ip')
    assert cli('bootstrap-admin', 'boss', 'Boss').returncode == 0
    closed = cli('setup-token')
    assert closed.returncode == 1 and 'already complete' in closed.stderr

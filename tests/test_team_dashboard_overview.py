import json
import re
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from starlette.testclient import TestClient

from team_memory.insights import day_keys
from team_memory.overview import INACTIVE_DAYS, TREND_DAYS
from team_memory.registry import Registry
from team_memory.sections import Capability, Section
from tests.team_db_helpers import seed_workspace
from tests.test_team_dashboard import USERS, build, seed, sign_in

WORKSPACE_SCHEMA = """
CREATE TABLE knowledge (id INTEGER PRIMARY KEY, content TEXT, status TEXT);
CREATE TABLE tam_history (sequence INTEGER PRIMARY KEY, record_id INTEGER, at TEXT, operation TEXT, actor TEXT,
    reason TEXT, revision INTEGER, before_state TEXT, after_state TEXT);
"""


@pytest.fixture(autouse=True)
def backend(team_backend):
    """Every test of this module runs on each selected team backend (--backend)."""
    return team_backend


def actor(user_id):
    return json.dumps({'user_id': user_id, 'display_name': user_id.title(), 'client': 'test', 'org_role': 'member'})


def iso(days_ago=0):
    return (datetime.now(UTC) - timedelta(days=days_ago)).strftime('%Y-%m-%dT%H:%M:%S.000Z')


def workspace(registry, key, rows):
    statements = []
    for record_id, (user_id, content, days_ago, operation) in enumerate(rows, 1):
        statements.append(('INSERT INTO knowledge VALUES (?,?,?)', (record_id, content, 'active')))
        statements.append((('INSERT INTO tam_history(record_id,at,operation,actor,reason,revision,after_state) '
                            'VALUES (?,?,?,?,?,?,?)'),
                           (record_id, iso(days_ago), operation, actor(user_id), '', 1, json.dumps({'content': content}))))
    seed_workspace(registry, key, WORKSPACE_SCHEMA, statements)


@pytest.fixture
def world(tmp_path):
    transport = httpx.MockTransport(lambda request: httpx.Response(401))
    registry, accounts, settings, pool, _dashboard, app = build(tmp_path / "root", transport=transport)
    seed(registry, accounts)
    workspace(registry, 'personal_' + Registry.digest('dev'), [('dev', 'Dev private diary about the nebula.', 0, 'insert'),
                                                               ('dev', 'Second private thought.', 3, 'insert')])
    workspace(registry, registry.team_workspace_key('eng'), [('dev', 'Engineering deploys on Tuesdays.', 1, 'insert'),
                                                             ('dev', 'Rollback checklist lives in the wiki.', 2, 'insert'),
                                                             ('boss', 'Old manager note.', INACTIVE_DAYS + 20, 'insert')])
    with TestClient(app) as client:
        yield {'registry': registry, 'accounts': accounts, 'settings': settings, 'pool': pool, 'client': client}


OVERVIEW_MATRIX = [
    ('/dashboard/api/overview/me', set(USERS)),
    ('/dashboard/api/overview/team/eng', {'root', 'boss', 'audit'}),
    ('/dashboard/api/overview/team/ops', {'root', 'audit'}),
    ('/dashboard/api/overview/company', {'root', 'audit'}),
    ('/dashboard/api/overview/system', {'root'}),
]


def test_overview_authorization_matrix(world):
    client = world['client']
    for user_id in USERS:
        sign_in(client, user_id)
        for path, allowed in OVERVIEW_MATRIX:
            status = client.get(path).status_code
            assert status == (200 if user_id in allowed else 403), (user_id, path, status)
    client.cookies.clear()
    for path, _allowed in OVERVIEW_MATRIX:
        assert client.get(path).status_code == 401


def test_personal_counts_only_for_their_owner(world):
    client = world['client']
    sign_in(client, 'dev')
    mine = client.get('/dashboard/api/overview/me').json()
    by_label = {s['label']: s['records'] for s in mine['scopes']}
    assert by_label == {'Personal': 2, 'Engineering': 3, 'Shared': 0}
    assert mine['saves_30d'] == 4 and len(mine['trend_30d']) == TREND_DAYS and mine['trend_30d'][-1] == 1
    assert mine['last_activity'] and mine['active_tokens'] == 0
    assert {item['scope_label'] for item in mine['recent']} == {'Personal', 'Engineering'}
    for user_id in ('root', 'audit', 'boss'):
        sign_in(client, user_id)
        texts = ''.join(client.get(path).text for path, allowed in OVERVIEW_MATRIX if user_id in allowed)
        assert 'private' not in texts.lower() and 'diary' not in texts
        personal = next(s for s in client.get('/dashboard/api/overview/me').json()['scopes'] if s['label'] == 'Personal')
        assert personal['records'] == 0


def test_team_overview_members_trend_inactive_and_recent(world):
    client = world['client']
    sign_in(client, 'boss')
    team = client.get('/dashboard/api/overview/team/eng').json()
    members = {m['user_id']: m for m in team['members']}
    assert team['records'] == 3 and team['saves_30d'] == 2 and team['inactive_days'] == INACTIVE_DAYS
    assert members['dev']['saves_30d'] == 2 and not members['dev']['inactive']
    assert members['boss']['inactive'] and members['boss']['saves_30d'] == 0 and members['boss']['saves'] == 1
    assert team['inactive'] == ['boss']
    assert [r['excerpt'] for r in team['recent_records']][:2] == ['Old manager note.', 'Rollback checklist lives in the wiki.']
    assert team['trend_30d'][TREND_DAYS - 2] == 1 and len(team['trend_30d']) == TREND_DAYS


def test_company_overview_rows_and_trend(world):
    client = world['client']
    sign_in(client, 'audit')
    company = client.get('/dashboard/api/overview/company').json()
    rows = {d['team_id']: d for d in company['departments']}
    assert rows['eng'] == {**rows['eng'], 'members': 2, 'records': 3, 'saves_30d': 2}
    assert rows['ops'] == {**rows['ops'], 'members': 1, 'records': 0, 'saves_30d': 0, 'last_activity': None}
    assert sum(company['trend_30d']) == 2 and company['active_users'] == len(USERS)


def test_system_overview_health_providers_invites_and_logins(world):
    client, registry, accounts = world['client'], world['registry'], world['accounts']
    registry.add_user('newbie', 'Newbie')
    accounts.issue_invite('newbie')
    for _ in range(3):
        client.post('/dashboard/api/login', json={'user_id': 'dev', 'password': 'wrong password!!'})
    headers = sign_in(client, 'root')
    system = client.get('/dashboard/api/overview/system').json()
    assert system['workers'] == {'max': 2, 'running': 0, 'busy': 0} and system['uptime_seconds'] >= 0
    assert system['version'] and [i['user_id'] for i in system['pending_invites']] == ['newbie']
    assert system['logins']['failures'] == 3 and system['logins']['targeted_users'] == 1
    assert sum(system['logins']['failures_by_hour']) == 3 and len(system['logins']['failures_by_hour']) == 24
    assert {(p['target'], p['id']) for p in system['providers']} == {('llm', 'ollama'), ('embed', 'fastembed')}
    assert system['users']['without_password'] == 1
    assert system['recent_audit'][0]['action'] == 'invite_issued'
    client.post('/dashboard/api/admin/settings', headers=headers, json={'values': {'OPENAI_API_KEY': 'sk-test-key-000000001234'}})
    tested = client.post('/dashboard/api/admin/settings/test', headers=headers, json={'target': 'llm', 'provider': 'openai'})
    assert tested.json() == {**tested.json(), 'provider': 'openai', 'ok': False, 'detail': 'HTTP 401 (authentication failed)'}
    assert client.post('/dashboard/api/admin/settings/test', headers=headers,
                       json={'target': 'embed', 'provider': 'anthropic'}).status_code == 400
    view = client.get('/dashboard/api/admin/settings').json()
    cards = {(p['target'], p['id']): p for p in view['providers']}
    assert cards[('llm', 'openai')]['configured'] and cards[('llm', 'openai')]['check']['ok'] is False
    assert cards[('llm', 'anthropic')] == {**cards[('llm', 'anthropic')], 'configured': False, 'problem': 'No API key configured',
                                           'check': None, 'active': False}
    assert cards[('llm', 'ollama')]['active'] and cards[('embed', 'fastembed')]['configured']
    assert view['provider_keys'] == {'llm': 'MEMORY_LLM_PROVIDER', 'embed': 'MEMORY_EMBED_PROVIDER'}
    assert 'sk-test-key' not in json.dumps(view)


def test_audit_filters_and_like_escaping(world):
    client, registry = world['client'], world['registry']
    with registry.acting_as('root'):
        registry.add_team('qa_team', 'QA')
        registry.rename_team('qa_team', 'Quality')
    sign_in(client, 'root')
    by_actor = client.get('/dashboard/api/admin/audit?actor=root').json()
    assert {e['subject'] for e in by_actor['events']} == {'qa_team'} and 'team_renamed' in by_actor['actions']
    assert [e['action'] for e in client.get('/dashboard/api/admin/audit?action=team_').json()['events']] == \
        ['team_renamed', 'team_created', 'team_created', 'team_created']
    assert client.get('/dashboard/api/admin/audit?action=%25').json()['events'] == []
    assert {e['subject'] for e in client.get('/dashboard/api/admin/audit?subject=qa_').json()['events']} == {'qa_team'}
    assert client.get('/dashboard/api/admin/audit?subject=a%25t').json()['events'] == []


def test_static_assets_fonts_icons_and_csp(world):
    client = world['client']
    font = client.get('/dashboard/static/fonts/inter-latin-wght-normal.woff2')
    assert font.status_code == 200 and font.headers['content-type'] == 'font/woff2'
    assert 'max-age' in font.headers['cache-control']
    icons = client.get('/dashboard/static/icons.svg')
    assert icons.headers['content-type'] == 'image/svg+xml' and 'i-home' in icons.text
    assert client.get('/dashboard/static/fonts/inter-license.txt').text.count('SIL Open Font License') >= 1
    assert client.get('/dashboard/static/fonts/missing.woff2').status_code == 404
    assert client.get('/dashboard/static/other/app.js').status_code == 404
    assert client.get('/dashboard/static/..%2F..%2Fregistry.py').status_code == 404
    page = client.get('/dashboard/')
    assert "font-src 'self'" in page.headers['content-security-policy']
    assert '<style' not in page.text and ' style=' not in page.text
    css = client.get('/dashboard/static/app.css').text
    assert re.search(r'border-(left|right)(-width)?:\s*[2-9]', css) is None
    assert 'background-clip:text' not in css.replace(' ', '')


def test_section_groups_and_icons_default_by_capability():
    section = Section(id='learning', title='Onboarding', capability=Capability.team_people,
                      script='/learning/static/learning.js', mount='TamLearning.mount')
    assert (section.group, section.icon) == ('department', 'spark')
    assert Section(id='x', title='X', capability='authenticated', script='/x.js', mount='X.y', icon='book').group == 'personal'
    with pytest.raises(ValueError):
        Section(id='x', title='X', capability='authenticated', script='/x.js', mount='X.y', group='sales')
    with pytest.raises(ValueError):
        Section(id='x', title='X', capability='authenticated', script='/x.js', mount='X.y', icon='<svg>')


def test_day_keys_are_contiguous_utc_days():
    keys = day_keys(TREND_DAYS)
    assert len(keys) == TREND_DAYS and keys[-1] == datetime.now(UTC).date().isoformat()

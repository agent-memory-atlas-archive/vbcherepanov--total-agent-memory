"""Dashboard + setup-wizard database endpoints (plan 4.9/4.10) against Protocol fakes.

The fakes stand in for W4's DatabaseConfigService and W5's MigrationRunner/MaintenanceGate;
they audit like the real services (host/db/sslmode only) so the secrecy assertions cover the
whole HTTP path: the password must never reach a response, a log line or admin_events.
"""
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from starlette.testclient import TestClient

from tam_db.contracts import Backend
from team_memory.accounts import AccountPolicy, Accounts
from team_memory.app import MAINTENANCE_MESSAGES, create_app
from team_memory.contracts import Conflict, Forbidden, NotFound, Unavailable
from team_memory.dashboard import CSP
from team_memory.dashboard_service import DashboardService
from team_memory.database_contracts import (
    CHECK_ORDER,
    DATABASE_API,
    DATABASE_MIGRATE_API,
    DATABASE_MIGRATION_API,
    DATABASE_MIGRATION_CANCEL_API,
    DATABASE_PLAN_API,
    DATABASE_REPOINT_API,
    DATABASE_ROLLBACK_API,
    DATABASE_TEST_API,
    MAINTENANCE_RETRY_AFTER_SECONDS,
    SETUP_DATABASE_API,
    SETUP_DATABASE_TEST_API,
    CheckId,
    CheckReport,
    CheckStatus,
    ConfigSource,
    DatabaseCheck,
    DatabaseConfig,
    DatabaseConfigView,
    DatabaseDsn,
    DatabaseEstimate,
    DatabaseKind,
    DsnOrigin,
    ErrorCategory,
    MaintenanceReason,
    MaintenanceState,
    MigrationPhase,
    MigrationPlan,
    MigrationProgress,
    QuarantineEstimate,
    SetupDatabaseRequest,
    SqliteArchive,
    TableEstimate,
    TargetState,
)
from team_memory.metrics import Metrics
from team_memory.registry import Registry
from team_memory.service import MemoryService
from team_memory.settings import SettingsStore, load_cipher
from team_memory.setup import SetupPolicy, SetupService
from team_memory.setup_database import SetupDatabaseService
from team_memory.worker import WorkerPool

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = 'correct horse battery staple'
SECRET = 'S3cr3t-pg-p@ss:w/rd'
ENCODED_SECRET = SECRET.replace('@', '%40').replace(':', '%3A').replace('/', '%2F')
DSN = 'postgresql://tam:' + ENCODED_SECRET + '@db.internal:5433/tam_prod?sslmode=require'
NEW_SECRET = 'rotated-Pg-Secret-9f2'
NEW_DSN = 'postgresql://tam:' + NEW_SECRET + '@db2.internal:5432/tam_prod?sslmode=verify-full'
QUARANTINE = (QuarantineEstimate(database='team_' + 'a' * 64, table='knowledge_nodes', rows=2,
                                 reasons=('fk:knowledge_nodes.node_id->graph_nodes.id',), sample_pks=('[7]', '[9]')),
              QuarantineEstimate(database='team_' + 'a' * 64, table='tam_history', rows=1,
                                 reasons=('fk:tam_history.record_id->knowledge.id',), sample_pks=('[3]',), audit=True))
INSTANCE = UUID('11111111-2222-4333-8444-555555555555')
CODE = re.compile(r'Setup code: ([A-Z0-9-]+)')
USERS = {'root': 'superadmin', 'dev': 'member', 'audit': 'company_viewer'}
SECRETS = (SECRET, NEW_SECRET, 'S3cr3t-pg-p%40ss', 'rotated-Pg')


def now():
    return datetime.now(UTC)


def report(dsn: DatabaseDsn, ok=True, target=TargetState.EMPTY) -> CheckReport:
    checks = []
    for check_id in CHECK_ORDER:
        if not ok and check_id is CheckId.PRIVILEGES:
            checks.append(DatabaseCheck(id=check_id, status=CheckStatus.FAILED, category=ErrorCategory.PERMISSION,
                                        message='Role tam lacks CREATEROLE',
                                        dba_sql=('ALTER ROLE "tam" CREATEROLE;',)))
        else:
            checks.append(DatabaseCheck(id=check_id, status=CheckStatus.PASSED, message=check_id.value + ' ok'))
    return CheckReport(dsn_masked=dsn.masked(), checks=tuple(checks), target_state=target, server_version='18.1',
                       warnings=dsn.warnings(), checked_at=now(), duration_ms=12)


class FakeDatabase:
    """DatabaseConfigService: holds the effective DSN like database.json would, audits like W4."""

    def __init__(self, registry: Registry):
        self.registry = registry
        self.dsn: DatabaseDsn | None = None
        self.last: CheckReport | None = None
        self.tested: list[str] = []
        self.runner: FakeRunner | None = None
        self.checker: FakeChecker | None = None
        self.fail_checks = False

    def view(self) -> DatabaseConfigView:
        archive = self.runner.archive if self.runner else None
        job = self.runner.job if self.runner else None
        if self.dsn is None:
            return DatabaseConfigView(backend=Backend.SQLITE, source=ConfigSource.DEFAULT, last_check=self.last,
                                      migration=job)
        return DatabaseConfigView(backend=Backend.POSTGRES, source=ConfigSource.WEB, dsn_masked=self.dsn.masked(),
                                  host_db=self.dsn.host_db(), sslmode=self.dsn.effective_sslmode,
                                  warnings=self.dsn.warnings(), instance_id=INSTANCE, generation=2,
                                  updated_at=now(), updated_by='root', last_check=self.last, migration=job,
                                  archive=archive, rollback_available=archive is not None)

    def test(self, dsn: DatabaseDsn, actor: str) -> CheckReport:
        self.tested.append(dsn.to_uri())
        self.last = report(dsn, ok=not self.fail_checks)
        with self.registry.acting_as(actor):
            self.registry.record_event('database_tested', dsn.host_db(), json.dumps(dsn.audit().model_dump(mode='json')))
        logging.getLogger('team_memory.database_config').info(json.dumps({'event': 'database_tested',
                                                                          **dsn.audit().model_dump(mode='json')}))
        return self.last

    def repoint(self, dsn: DatabaseDsn, actor: str) -> DatabaseConfigView:
        if self.dsn is None:
            raise Conflict('The server runs on SQLite; migrate instead of repointing')
        self.dsn = dsn
        with self.registry.acting_as(actor):
            self.registry.record_event('database_repointed', dsn.host_db())
        return self.view()


class FakeRunner:
    """MigrationRunner + MaintenanceGate: a job that finishes after ``steps`` progress polls."""

    def __init__(self, database: FakeDatabase, steps=2):
        self.database, self.steps = database, steps
        database.runner = self
        self.plans: dict[UUID, tuple[MigrationPlan, DatabaseDsn]] = {}
        self.job: MigrationProgress | None = None
        self.archive: SqliteArchive | None = None
        self.polls = 0
        self.maintenance: MaintenanceState | None = None
        self.rollbacks: list[tuple[str, str]] = []
        self.fail_with: str | None = None

    def state(self):
        return self.maintenance

    def enter(self, reason, job_id):
        self.maintenance = MaintenanceState(reason=reason, job_id=job_id, since=now())
        return self.maintenance

    def leave(self):
        self.maintenance = None

    def plan(self, dsn: DatabaseDsn, actor: str) -> MigrationPlan:
        checked = report(dsn, ok=not self.database.fail_checks)
        created = now()
        plan = MigrationPlan(plan_id=uuid4(), created_at=created, expires_at=created + timedelta(minutes=15),
                             created_by=actor, target=dsn.masked(), report=checked, estimated_seconds=42,
                             blockers=() if checked.ok else ('Role tam lacks CREATEROLE',), quarantine=QUARANTINE,
                             databases=(DatabaseEstimate(kind=DatabaseKind.IDENTITY, name='identity', tables=(
                                 TableEstimate(name='users', rows=3, bytes=4096),)),
                                 DatabaseEstimate(kind=DatabaseKind.WORKSPACE, name='team_' + 'a' * 64, tables=(
                                     TableEstimate(name='knowledge', rows=120, bytes=65536),))))
        self.plans[plan.plan_id] = (plan, dsn)
        return plan

    def start(self, plan_id: UUID, actor: str) -> MigrationProgress:
        if plan_id not in self.plans:
            raise NotFound('Unknown or expired plan')
        if self.job is not None and not self.job.terminal:
            raise Conflict('A migration is already running')
        plan, _dsn = self.plans[plan_id]
        if not plan.ready:
            raise Conflict('The plan is blocked: ' + '; '.join(plan.blockers))
        self.polls = 0
        self.job = MigrationProgress(job_id=uuid4(), plan_id=plan_id, phase=MigrationPhase.COPY_WORKSPACES,
                                     started_at=now(), updated_at=now(), started_by=actor, target=plan.target,
                                     workspace_index=1, workspace_total=2, database='identity', table='users',
                                     rows_copied=3, rows_total=plan.total_rows, quarantined_rows=1,
                                     quarantine=plan.quarantine[:1])
        self.enter(MaintenanceReason.MIGRATION, self.job.job_id)
        return self.job

    def progress(self) -> MigrationProgress | None:
        if self.job is None or self.job.terminal:
            return self.job
        self.polls += 1
        if self.polls >= self.steps:
            self._finish()
        return self.job

    def _finish(self):
        plan, dsn = self.plans[self.job.plan_id]
        if self.fail_with:
            self.job = self.job.model_copy(update={'phase': MigrationPhase.FAILED, 'finished_at': now(),
                                                   'error': self.fail_with,
                                                   'error_category': ErrorCategory.UNEXPECTED})
        else:
            self.job = self.job.model_copy(update={'phase': MigrationPhase.DONE, 'finished_at': now(),
                                                   'rows_copied': plan.total_rows, 'workspace_index': 2})
            self.database.dsn = dsn
            self.archive = SqliteArchive(path='archive/sqlite-20260925T120000Z', created_at=now(), bytes=1_234_567)
        self.leave()

    def cancel(self, actor: str) -> MigrationProgress:
        if self.job is None or not self.job.cancellable:
            raise Conflict('No running migration to cancel')
        self.job = self.job.model_copy(update={'phase': MigrationPhase.CANCELLED, 'finished_at': now(),
                                               'cancel_requested': True})
        self.leave()
        return self.job

    def rollback(self, organization: str, actor: str) -> DatabaseConfig:
        self.rollbacks.append((organization, actor))
        self.database.dsn = None
        self.archive = None
        return DatabaseConfig(backend=Backend.SQLITE, instance_id=INSTANCE, generation=3, updated_at=now(),
                              updated_by=actor)


class FakeChecker:
    """DatabaseChecker for the wizard's Test button (the real one is PgDatabaseChecker(isolated=True))."""

    def __init__(self, database: FakeDatabase):
        self.database = database
        self.calls: list[tuple[str, UUID | None]] = []

    def check(self, dsn: DatabaseDsn, *, instance_id: UUID | None) -> CheckReport:
        self.calls.append((dsn.to_uri(), instance_id))
        return report(dsn, ok=not self.database.fail_checks)


def build(root: Path, with_database=True, steps=2, real_setup_checker=False):
    registry = Registry(root)
    accounts = Accounts(registry, AccountPolicy())
    settings = SettingsStore(registry, load_cipher(root, {}), {})
    pool = WorkerPool(registry.root, maximum=1, environment=settings.overrides)
    database = FakeDatabase(registry) if with_database else None
    runner = FakeRunner(database, steps) if with_database else None
    checker = FakeChecker(database) if with_database and not real_setup_checker else None
    if database is not None:
        database.checker = checker
    metrics = Metrics()
    dashboard = DashboardService(registry, accounts, MemoryService(registry, pool), settings, metrics,
                                 database=database, migration=runner)
    setup = SetupService(registry, accounts, metrics, SetupPolicy())
    app = create_app(dashboard.memory, dashboard, setup=setup, base_url='http://tam.test:3737', maintenance=runner,
                     setup_checker=checker)
    return registry, accounts, pool, database, runner, metrics, app


def seed(registry: Registry, accounts: Accounts):
    for user_id, role in USERS.items():
        registry.add_user(user_id, user_id.title())
        if role != 'member':
            registry.set_org_role(user_id, role)
        invite = accounts.issue_invite(user_id)
        accounts.redeem_invite(user_id, invite.code, PASSWORD, '127.0.0.1')
    registry.set_organization({'name': 'Acme Corp', 'setup_state': 'complete'})


def sign_in(client, user_id):
    client.cookies.clear()
    reply = client.post('/dashboard/api/login', json={'user_id': user_id, 'password': PASSWORD})
    assert reply.status_code == 200, reply.text
    return {'X-CSRF-Token': reply.json()['csrf']}


@pytest.fixture
def world(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    registry, accounts, pool, database, runner, metrics, app = build(tmp_path / 'root')
    seed(registry, accounts)
    with TestClient(app) as client:
        yield {'registry': registry, 'pool': pool, 'database': database, 'runner': runner, 'client': client,
               'caplog': caplog, 'metrics': metrics}


@pytest.fixture
def fresh(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    registry, accounts, _pool, database, runner, _metrics, app = build(tmp_path / 'root', steps=1)
    with TestClient(app) as client:
        match = CODE.search(caplog.text)
        assert match, caplog.text
        yield {'registry': registry, 'accounts': accounts, 'database': database, 'runner': runner, 'client': client,
               'token': match.group(1), 'caplog': caplog}


def assert_no_secret(*texts):
    for text in texts:
        for secret in SECRETS:
            assert secret not in text, secret


def assert_world_clean(world, responses):
    assert_no_secret(*(reply.text for reply in responses), world['caplog'].text,
                     json.dumps(world['registry'].audit_events(limit=500)))


ADMIN_CALLS = (('get', DATABASE_API, None), ('post', DATABASE_TEST_API, {'dsn': DSN}),
               ('post', DATABASE_PLAN_API, {'dsn': DSN}),
               ('post', DATABASE_MIGRATE_API, {'plan_id': str(uuid4()), 'confirm': True}),
               ('get', DATABASE_MIGRATION_API, None), ('post', DATABASE_MIGRATION_CANCEL_API, {}),
               ('post', DATABASE_REPOINT_API, {'dsn': DSN}), ('post', DATABASE_ROLLBACK_API, {'organization': 'Acme Corp'}))


def call(client, method, path, payload, headers=None):
    if method == 'get':
        return client.get(path)
    return client.post(path, json=payload, headers=headers or {})


def test_endpoints_are_superadmin_only_and_need_csrf(world):
    client = world['client']
    client.cookies.clear()
    for method, path, payload in ADMIN_CALLS:
        assert call(client, method, path, payload).status_code == 401, path
    for user in ('dev', 'audit'):
        headers = sign_in(client, user)
        for method, path, payload in ADMIN_CALLS:
            reply = call(client, method, path, payload, headers)
            assert reply.status_code == 403, (user, path, reply.text)
    sign_in(client, 'root')
    for method, path, payload in ADMIN_CALLS:
        if method == 'post':
            reply = client.post(path, json=payload)
            assert reply.status_code == 403 and 'CSRF' in reply.json()['error'], path
    assert world['database'].tested == [] and world['runner'].plans == {}


def test_database_section_is_listed_for_superadmins_only(world):
    client = world['client']
    sign_in(client, 'root')
    sections = client.get('/dashboard/api/session').json()['sections']
    ids = [section['id'] for section in sections]
    database = next(section for section in sections if section['id'] == 'database')
    assert database == {'id': 'database', 'title': 'Database', 'script': '/dashboard/static/database.js',
                        'stylesheet': None, 'mount': 'TamSections.database', 'group': 'administration',
                        'icon': 'database'}
    assert ids.index('database') + 1 == ids.index('settings')
    for user in ('dev', 'audit'):
        sign_in(client, user)
        assert 'database' not in [s['id'] for s in client.get('/dashboard/api/session').json()['sections']]
    assert 'id="i-database"' in client.get('/dashboard/static/icons.svg').text


def test_view_and_test_show_only_the_mask(world):
    client = world['client']
    headers = sign_in(client, 'root')
    view = client.get(DATABASE_API)
    assert view.status_code == 200 and view.json()['backend'] == 'sqlite' and view.json()['dsn_masked'] is None
    assert view.json()['maintenance'] is None
    tested = client.post(DATABASE_TEST_API, headers=headers, json={'dsn': DSN})
    assert tested.status_code == 200, tested.text
    body = tested.json()
    assert body['ok'] is True and [check['id'] for check in body['checks']] == [c.value for c in CHECK_ORDER]
    assert body['dsn_masked'] == 'postgresql://tam:••••@db.internal:5433/tam_prod?sslmode=require'
    assert world['database'].tested == [DatabaseDsn.parse(DSN).to_uri()]
    assert DatabaseDsn.parse(world['database'].tested[0]).password.get_secret_value() == SECRET
    again = client.get(DATABASE_API).json()
    assert again['last_check']['dsn_masked'] == body['dsn_masked']
    events = [(e['action'], e['actor']) for e in world['registry'].audit_events(limit=50)]
    assert ('database_tested', 'root') in events
    assert_world_clean(world, [view, tested])


def test_failed_checks_carry_dba_sql(world):
    client = world['client']
    headers = sign_in(client, 'root')
    world['database'].fail_checks = True
    body = client.post(DATABASE_TEST_API, headers=headers, json={'dsn': DSN}).json()
    assert body['ok'] is False
    failed = [check for check in body['checks'] if check['status'] == 'failed']
    assert failed == [{'id': 'privileges', 'status': 'failed', 'message': 'Role tam lacks CREATEROLE',
                       'category': 'permission', 'dba_sql': ['ALTER ROLE "tam" CREATEROLE;']}]
    plan = client.post(DATABASE_PLAN_API, headers=headers, json={'dsn': DSN}).json()
    assert plan['ready'] is False and plan['blockers'] == ['Role tam lacks CREATEROLE']
    blocked = client.post(DATABASE_MIGRATE_API, headers=headers, json={'plan_id': plan['plan_id'], 'confirm': True})
    assert blocked.status_code == 409 and world['runner'].job is None


@pytest.mark.parametrize('dsn, fragment', [
    ('postgresql://tam:' + ENCODED_SECRET + '@db/x?options=-csearch_path%3Dws_other', 'options parameter is not allowed'),
    ('host=db user=tam password=' + SECRET, 'key=value connection strings are not accepted'),
    ('mysql://tam:' + SECRET + '@db/x', 'only postgresql://'),
    ('postgresql://tam:' + ENCODED_SECRET + '@db/x?sslmode=sometimes', 'sslmode'),
    ('postgresql://tam:' + SECRET + '@db/x', 'port must be a number'),
    ('', 'must not be empty'),
])
def test_invalid_dsn_is_rejected_without_echoing_it(world, dsn, fragment):
    client = world['client']
    headers = sign_in(client, 'root')
    replies = [client.post(path, headers=headers, json={'dsn': dsn})
               for path in (DATABASE_TEST_API, DATABASE_PLAN_API, DATABASE_REPOINT_API)]
    for reply in replies:
        assert reply.status_code == 400 and fragment in reply.json()['error'], reply.text
    assert world['database'].tested == [] and world['runner'].plans == {}
    assert_world_clean(world, replies)


WEB_ONLY_REJECTED = [
    ('postgresql://tam:' + ENCODED_SECRET + '@%2Fvar%2Frun%2Fpostgresql/tam', 'unix-socket hosts are accepted only'),
    ('postgresql://tam:' + ENCODED_SECRET + '@db/tam?sslkey=/etc/x', 'sslkey names a file on the server'),
]


def assert_web_rule_rejections(replies, fragment):
    for reply in replies:
        assert reply.status_code == 400 and fragment in reply.json()['error'], reply.text
        assert 'TAM_TEAM_DATABASE_URL' in reply.json()['error']


@pytest.mark.parametrize('dsn, fragment', WEB_ONLY_REJECTED)
def test_admin_endpoints_apply_web_dsn_rules(world, dsn, fragment):
    client = world['client']
    headers = sign_in(client, 'root')
    replies = [client.post(path, headers=headers, json={'dsn': dsn})
               for path in (DATABASE_TEST_API, DATABASE_PLAN_API, DATABASE_REPOINT_API)]
    assert_web_rule_rejections(replies, fragment)
    assert world['database'].tested == [] and world['runner'].plans == {}
    assert_world_clean(world, replies)


@pytest.mark.parametrize('dsn, fragment', WEB_ONLY_REJECTED)
def test_setup_endpoints_apply_web_dsn_rules(fresh, dsn, fragment):
    client, token = fresh['client'], fresh['token']
    replies = [client.post(SETUP_DATABASE_TEST_API, json={'token': token, 'dsn': dsn}),
               client.post(SETUP_DATABASE_API, json={'token': token, 'backend': 'postgres', 'dsn': dsn})]
    assert_web_rule_rejections(replies, fragment)
    assert fresh['database'].checker.calls == [] and fresh['runner'].plans == {}
    assert_no_secret(*(reply.text for reply in replies), fresh['caplog'].text)


def test_non_string_dsn_and_unknown_fields_are_rejected(world):
    client = world['client']
    headers = sign_in(client, 'root')
    replies = [client.post(DATABASE_TEST_API, headers=headers, json={'dsn': 42}),
               client.post(DATABASE_TEST_API, headers=headers, json={'dsn': DSN, 'password': SECRET}),
               client.post(DATABASE_MIGRATE_API, headers=headers, json={'plan_id': str(uuid4()), 'confirm': False}),
               client.post(DATABASE_ROLLBACK_API, headers=headers, json={'organization': ''})]
    assert [reply.status_code for reply in replies] == [400, 400, 400, 400]
    assert_world_clean(world, replies)


def test_dry_run_migrate_progress_and_switch(world):
    client = world['client']
    headers = sign_in(client, 'root')
    plan = client.post(DATABASE_PLAN_API, headers=headers, json={'dsn': DSN})
    assert plan.status_code == 200, plan.text
    body = plan.json()
    assert body['ready'] is True and body['total_rows'] == 123 and body['estimated_seconds'] == 42
    assert body['quarantined_rows'] == 3 and [q['audit'] for q in body['quarantine']] == [False, True]
    assert body['quarantine'][0]['sample_pks'] == ['[7]', '[9]']
    assert body['target'] == DatabaseDsn.parse(DSN).masked() and 'dsn' not in body
    unknown = client.post(DATABASE_MIGRATE_API, headers=headers, json={'plan_id': str(uuid4()), 'confirm': True})
    assert unknown.status_code == 404
    started = client.post(DATABASE_MIGRATE_API, headers=headers, json={'plan_id': body['plan_id'], 'confirm': True})
    assert started.status_code == 200, started.text
    assert started.json()['phase'] == 'copy_workspaces' and started.json()['started_by'] == 'root'
    twice = client.post(DATABASE_MIGRATE_API, headers=headers, json={'plan_id': body['plan_id'], 'confirm': True})
    assert twice.status_code == 503 and twice.headers['retry-after'] == str(MAINTENANCE_RETRY_AFTER_SECONDS)
    assert client.get(DATABASE_API).json()['maintenance']['reason'] == 'migration'
    first = client.get(DATABASE_MIGRATION_API)
    assert first.status_code == 200 and first.json()['terminal'] is False and first.json()['cancellable'] is True
    assert first.json()['quarantined_rows'] == 1 and first.json()['quarantine'][0]['table'] == 'knowledge_nodes'
    done = client.get(DATABASE_MIGRATION_API).json()
    assert done['phase'] == 'done' and done['percent'] == 100.0 and done['terminal'] is True
    view = client.get(DATABASE_API).json()
    assert view['backend'] == 'postgres' and view['dsn_masked'] == DatabaseDsn.parse(DSN).masked()
    assert view['rollback_available'] is True and view['archive']['path'].startswith('archive/sqlite-')
    backup = client.get('/dashboard/api/admin/backup').json()
    assert backup['backend'] == 'postgres' and backup['online'] is True and 'pg_dump' in backup['reason']
    assert '--dsn-env VAR' in backup['reason'] and str(world['registry'].root) in backup['reason']
    assert_world_clean(world, [plan, started, first, client.get(DATABASE_MIGRATION_API)])


def test_progress_is_null_before_any_job_and_cancel(world):
    client, runner = world['client'], world['runner']
    headers = sign_in(client, 'root')
    assert client.get(DATABASE_MIGRATION_API).json() is None
    assert client.post(DATABASE_MIGRATION_CANCEL_API, headers=headers).status_code == 409
    runner.steps = 99
    plan = client.post(DATABASE_PLAN_API, headers=headers, json={'dsn': DSN}).json()
    client.post(DATABASE_MIGRATE_API, headers=headers, json={'plan_id': plan['plan_id'], 'confirm': True})
    cancelled = client.post(DATABASE_MIGRATION_CANCEL_API, headers=headers)
    assert cancelled.status_code == 200 and cancelled.json()['phase'] == 'cancelled'
    assert client.get(DATABASE_API).json()['backend'] == 'sqlite'
    assert client.post('/dashboard/api/admin/teams', headers=headers, json={'id': 'eng', 'name': 'Eng'}).status_code == 200


def test_maintenance_gate_blocks_mcp_and_dashboard_writes(world):
    client, runner, registry = world['client'], world['runner'], world['registry']
    headers = sign_in(client, 'root')
    token = registry.issue_token('dev', 'codex')
    runner.enter(MaintenanceReason.MIGRATION, uuid4())
    blocked = [client.post('/mcp/', headers={'Authorization': 'Bearer ' + token}, json={}),
               client.post('/api/call', headers={'Authorization': 'Bearer ' + token}, json={'name': 'memory_recall'}),
               client.post('/dashboard/api/admin/teams', headers=headers, json={'id': 'eng', 'name': 'Eng'}),
               client.post('/dashboard/api/memory', headers=headers, json={'name': 'memory_recall', 'arguments': {}}),
               client.post(DATABASE_ROLLBACK_API, headers=headers, json={'organization': 'Acme Corp'}),
               client.post('/learning/api/progress', headers=headers, json={}),
               client.post('/reports/api/report', headers=headers, json={})]
    for reply in blocked:
        assert reply.status_code == 503, reply.request.url
        assert reply.headers['retry-after'] == str(MAINTENANCE_RETRY_AFTER_SECONDS)
        assert reply.json()['code'] == 'unavailable' and reply.json()['maintenance']['reason'] == 'migration'
    assert client.get('/dashboard/api/admin/teams').status_code == 200
    assert client.get(DATABASE_MIGRATION_API).status_code == 200
    assert client.get('/healthz').status_code == 200
    assert client.get('/dashboard/static/database.js').status_code == 200
    assert client.post(DATABASE_MIGRATION_CANCEL_API, headers=headers).status_code == 409
    paused = [client.post('/dashboard/api/logout', headers=headers),
              client.post('/dashboard/api/login', json={'user_id': 'root', 'password': PASSWORD}),
              client.post('/dashboard/api/login/token', json={'token': token})]
    assert [reply.status_code for reply in paused] == [503, 503, 503]
    assert client.get('/dashboard/api/session').status_code == 200
    blocked += paused
    runner.leave()
    headers = sign_in(client, 'root')
    assert client.post('/dashboard/api/admin/teams', headers=headers, json={'id': 'eng', 'name': 'Eng'}).status_code == 200
    rejected = [c for c in world['metrics'].snapshot()['counters'] if c['name'] == 'maintenance_rejected']
    assert rejected and sum(c['value'] for c in rejected) == len(blocked)


def test_lost_lease_blocks_writes_with_its_own_message(world):
    client, runner = world['client'], world['runner']
    headers = sign_in(client, 'root')
    runner.enter(MaintenanceReason.LEASE_LOST, None)
    reply = client.post('/dashboard/api/admin/teams', headers=headers, json={'id': 'eng', 'name': 'Eng'})
    maintenance = reply.json()['maintenance']
    assert reply.status_code == 503 and maintenance['reason'] == 'lease_lost' and maintenance['job_id'] is None
    assert 'Another TAM server is using this database' in reply.json()['error']
    view = client.get(DATABASE_API)
    assert view.status_code == 200 and view.json()['maintenance']['reason'] == 'lease_lost'
    assert client.get(DATABASE_MIGRATION_API).json() is None
    assert set(MaintenanceReason) <= set(MAINTENANCE_MESSAGES)


def test_repoint_and_rollback_confirms_organization(world):
    client, database, runner = world['client'], world['database'], world['runner']
    headers = sign_in(client, 'root')
    refused = client.post(DATABASE_REPOINT_API, headers=headers, json={'dsn': NEW_DSN})
    assert refused.status_code == 409
    database.dsn = DatabaseDsn.parse(DSN)
    runner.archive = SqliteArchive(path='archive/sqlite-20260925T120000Z', created_at=now(), bytes=10)
    moved = client.post(DATABASE_REPOINT_API, headers=headers, json={'dsn': NEW_DSN})
    assert moved.status_code == 200, moved.text
    assert moved.json()['dsn_masked'] == 'postgresql://tam:••••@db2.internal:5432/tam_prod?sslmode=verify-full'
    assert database.dsn.password.get_secret_value() == NEW_SECRET
    rolled = client.post(DATABASE_ROLLBACK_API, headers=headers, json={'organization': 'Acme Corp'})
    assert rolled.status_code == 200, rolled.text
    assert runner.rollbacks == [('Acme Corp', 'root')]
    assert rolled.json()['backend'] == 'sqlite' and 'dsn_token' not in rolled.text and 'previous' not in rolled.json()
    actions = [e['action'] for e in world['registry'].audit_events(limit=50)]
    assert 'database_repointed' in actions
    assert_world_clean(world, [refused, moved, rolled])


def test_backup_info_stays_sqlite_without_postgres(world):
    client = world['client']
    sign_in(client, 'root')
    backup = client.get('/dashboard/api/admin/backup').json()
    assert backup['backend'] == 'sqlite' and backup['online'] is False and 'stop the server' in backup['reason']
    assert backup['command'].startswith('tam-team --root ')


def test_database_management_unavailable_without_services(tmp_path):
    registry, accounts, _pool, _database, _runner, _metrics, app = build(tmp_path / 'root', with_database=False)
    seed(registry, accounts)
    with TestClient(app) as client:
        headers = sign_in(client, 'root')
        for method, path, payload in ADMIN_CALLS:
            reply = call(client, method, path, payload, headers)
            assert reply.status_code == 503 and reply.json()['code'] == 'unavailable', path
        assert client.get('/dashboard/api/admin/backup').json()['backend'] == 'sqlite'
        assert client.post('/dashboard/api/admin/teams', headers=headers, json={'id': 'a', 'name': 'A'}).status_code == 200


def test_setup_wizard_database_step(fresh):
    client, token, runner = fresh['client'], fresh['token'], fresh['runner']
    status = client.get('/dashboard/api/setup').json()
    assert status['database'] == {'backend': 'sqlite', 'source': 'default'}
    assert client.post(SETUP_DATABASE_TEST_API, json={'token': 'WRONG-CODE', 'dsn': DSN}).status_code == 401
    assert client.post(SETUP_DATABASE_TEST_API, json={'dsn': DSN}).status_code == 400
    tested = client.post(SETUP_DATABASE_TEST_API, json={'token': token, 'dsn': DSN})
    assert tested.status_code == 200 and tested.json()['ok'] is True
    assert fresh['database'].checker.calls == [(DatabaseDsn.parse(DSN).to_uri(), None)]
    assert fresh['database'].tested == []
    tested_events = [e for e in fresh['registry'].audit_events(limit=50) if e['action'] == 'database_tested']
    assert [(e['actor'], e['subject']) for e in tested_events] == [('setup', 'database')]
    assert json.loads(tested_events[0]['detail']) == {'host': 'db.internal:5433', 'database': 'tam_prod',
                                                      'sslmode': 'require', 'ok': True, 'target_state': 'empty'}
    assert client.post(SETUP_DATABASE_API, json={'token': token, 'backend': 'postgres'}).status_code == 400
    assert client.post(SETUP_DATABASE_API, json={'token': token, 'backend': 'sqlite', 'dsn': DSN}).status_code == 400
    kept = client.post(SETUP_DATABASE_API, json={'token': token, 'backend': 'sqlite'})
    assert kept.status_code == 200 and kept.json()['backend'] == 'sqlite' and runner.job is None
    switched = client.post(SETUP_DATABASE_API, json={'token': token, 'backend': 'postgres', 'dsn': DSN})
    assert switched.status_code == 200, switched.text
    assert switched.json()['backend'] == 'postgres' and switched.json()['dsn_masked'] == DatabaseDsn.parse(DSN).masked()
    assert runner.job.started_by == 'setup' and runner.job.phase is MigrationPhase.DONE
    assert client.get('/dashboard/api/setup').json()['database'] == {'backend': 'postgres', 'source': 'web'}
    back = client.post(SETUP_DATABASE_API, json={'token': token, 'backend': 'sqlite'})
    assert back.status_code == 409
    done = client.post('/dashboard/api/setup/complete', json={
        'token': token, 'company_name': 'Acme Corp', 'user_id': 'alice', 'name': 'Alice', 'password': PASSWORD})
    assert done.status_code == 200, done.text
    closed = [client.post(SETUP_DATABASE_TEST_API, json={'token': token, 'dsn': DSN}),
              client.post(SETUP_DATABASE_API, json={'token': token, 'backend': 'postgres', 'dsn': DSN})]
    assert [reply.status_code for reply in closed] == [403, 403]
    assert_no_secret(tested.text, switched.text, *(reply.text for reply in closed), fresh['caplog'].text,
                     json.dumps(fresh['registry'].audit_events(limit=500)))


def test_setup_step_refuses_blocked_target_and_reports_failed_job(fresh):
    client, token, database, runner = fresh['client'], fresh['token'], fresh['database'], fresh['runner']
    database.fail_checks = True
    blocked = client.post(SETUP_DATABASE_API, json={'token': token, 'backend': 'postgres', 'dsn': DSN})
    assert blocked.status_code == 409 and 'CREATEROLE' in blocked.json()['error'] and runner.job is None
    database.fail_checks = False
    runner.fail_with = 'copy failed: disk full'
    failed = client.post(SETUP_DATABASE_API, json={'token': token, 'backend': 'postgres', 'dsn': DSN})
    assert failed.status_code == 409 and failed.json()['error'] == 'copy failed: disk full'
    assert client.get('/dashboard/api/setup').json()['database']['backend'] == 'sqlite'


def test_setup_step_is_closed_once_the_organization_is_set_up(tmp_path):
    registry, _accounts, _pool, database, runner, _metrics, app = build(tmp_path / 'root')
    registry.set_organization({'setup_state': 'admin_created'})
    with TestClient(app) as client:
        replies = [client.post(SETUP_DATABASE_TEST_API, json={'token': 'ANY-CODE', 'dsn': DSN}),
                   client.post(SETUP_DATABASE_API, json={'token': 'ANY-CODE', 'backend': 'sqlite'})]
        assert [reply.status_code for reply in replies] == [403, 403]
    assert database.checker.calls == [] and runner.plans == {}


def test_setup_wait_is_bounded_and_resumes_a_running_job(tmp_path):
    registry = Registry(tmp_path / 'root')
    accounts = Accounts(registry, AccountPolicy())
    setup = SetupService(registry, accounts, Metrics(), SetupPolicy())
    token = setup.issue_token().token
    database = FakeDatabase(registry)
    runner = FakeRunner(database, steps=10)
    ticks = iter(range(0, 10_000, 100))
    service = SetupDatabaseService(setup, registry, database, runner, clock=lambda: next(ticks), sleep=lambda _s: None,
                                   wait_seconds=250)
    request = SetupDatabaseRequest(token=token, backend=Backend.POSTGRES, dsn=DSN)
    with pytest.raises(Unavailable):
        service.choose(request, '127.0.0.1')
    first_job = runner.job.job_id
    runner.steps = runner.polls + 1
    view = service.choose(request, '127.0.0.1')
    assert view.backend is Backend.POSTGRES and runner.job.job_id == first_job and len(runner.plans) == 1
    registry.set_organization({'setup_state': 'complete'})
    with pytest.raises(Forbidden):
        service.choose(request, '127.0.0.1')


def test_setup_service_without_database_support(tmp_path):
    registry = Registry(tmp_path / 'root')
    setup = SetupService(registry, Accounts(registry, AccountPolicy()), Metrics(), SetupPolicy())
    token = setup.issue_token().token
    service = SetupDatabaseService(setup, registry, None, None)
    assert service.current() is None
    with pytest.raises(Unavailable):
        service.choose(SetupDatabaseRequest(token=token, backend=Backend.POSTGRES, dsn=DSN), '127.0.0.1')


def test_static_assets_wire_the_database_ui_under_strict_csp(world):
    client = world['client']
    page = client.get('/dashboard/')
    assert page.headers['content-security-policy'] == CSP
    assert page.text.index('/dashboard/static/database.js') < page.text.index('/dashboard/static/setup.js')
    for name in ('database.js', 'settings.js', 'setup.js'):
        script = client.get('/dashboard/static/' + name)
        assert script.status_code == 200 and script.headers['content-security-policy'] == CSP
        for forbidden in ('innerHTML', 'outerHTML', 'insertAdjacentHTML', 'eval(', 'new Function', '.style.',
                          'console.log', 'localStorage', 'sessionStorage'):
            assert forbidden not in script.text, (name, forbidden)
    database_js = client.get('/dashboard/static/database.js').text
    assert 'window.TamSections.database' in database_js and 'if (window.TamDatabase) return;' in database_js
    assert 'TamDatabase' not in client.get('/dashboard/static/settings.js').text
    assert "type: 'password'" in database_js and 'admin/database/migration' in database_js
    assert 'POLL_MS = 1000' in database_js
    setup_js = client.get('/dashboard/static/setup.js').text
    steps = re.findall(r"\{id: '([a-z]+)', title:", setup_js)
    assert steps[:5] == ['code', 'database', 'company', 'admin', 'departments']
    assert "SIGNED_IN_FROM = at('departments')" in setup_js and steps.index('departments') == 4
    assert re.search(r"go\(\d|backButton\(\d", setup_js) is None


def test_setup_wizard_tests_web_dsns_in_an_isolated_process(tmp_path):
    from team_memory.db_check import PgDatabaseChecker
    registry = Registry(tmp_path / 'root')
    setup = SetupService(registry, Accounts(registry, AccountPolicy()), Metrics(), SetupPolicy())
    checker = SetupDatabaseService(setup, registry, None, None).checker
    assert isinstance(checker, PgDatabaseChecker) and checker.isolated is True


@pytest.mark.postgres
def test_setup_test_never_borrows_the_servers_libpq_credentials(pg_database, tmp_path, caplog, monkeypatch):
    parsed = DatabaseDsn.parse(pg_database.url, origin=DsnOrigin.ENV)
    password = parsed.password.get_secret_value()
    home = tmp_path / 'home'
    home.mkdir()
    (home / '.pgpass').write_text(f'*:*:*:*:{password}\n')
    (home / '.pgpass').chmod(0o600)
    monkeypatch.setenv('PGPASSWORD', password)
    monkeypatch.setenv('HOME', str(home))
    caplog.set_level(logging.WARNING, logger='team_memory.setup')
    _registry, _accounts, _pool, _database, _runner, _metrics, app = build(tmp_path / 'root', real_setup_checker=True)
    with TestClient(app) as client:
        token = CODE.search(caplog.text).group(1)
        without = parsed.model_copy(update={'password': None}).to_uri()
        refused = client.post(SETUP_DATABASE_TEST_API, json={'token': token, 'dsn': without})
        assert refused.status_code == 400 and 'must include a password' in refused.json()['error']
        wrong = parsed.model_copy(update={'password': SecretStr('not-the-password')}).to_uri()
        borrowed = client.post(SETUP_DATABASE_TEST_API, json={'token': token, 'dsn': wrong})
        assert borrowed.status_code == 200, borrowed.text
        connect = borrowed.json()['checks'][0]
        assert connect['id'] == 'connect' and connect['status'] == 'failed' and connect['category'] == 'auth_failed'
        own = client.post(SETUP_DATABASE_TEST_API, json={'token': token, 'dsn': parsed.to_uri()})
        assert own.status_code == 200 and own.json()['checks'][0]['status'] == 'passed', own.text
        assert password not in borrowed.text + own.text

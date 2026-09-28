"""Browser checks of the Database settings card and the setup wizard's Database step.

The server runs in-process on a free port with the Protocol fakes from test_team_database_api,
so the UI flow (test, dry run, typed confirmation, 1 s progress polling, rollback) is exercised
end to end without PostgreSQL.
"""
import re
import socket
import threading
import time

import pytest

from team_memory.database_contracts import MaintenanceReason
from tests.test_team_database_api import CODE, DSN, SECRETS, build, seed

playwright = pytest.importorskip('playwright.sync_api')
uvicorn = pytest.importorskip('uvicorn')

STARTUP_SECONDS = 20
MASK = 'postgresql://tam:••••@db.internal:5433/tam_prod?sslmode=require'


class Server:
    def __init__(self, app):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        self.url = f'http://127.0.0.1:{self.port}'
        self.server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=self.port, log_level='warning'))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.monotonic() + STARTUP_SECONDS
        while not self.server.started:
            assert time.monotonic() < deadline and self.thread.is_alive(), 'server did not start'
            time.sleep(0.05)
        return self

    def __exit__(self, *_exc):
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture
def admin_server(tmp_path, caplog):
    registry, accounts, _pool, database, runner, _metrics, app = build(tmp_path / 'root', steps=3)
    seed(registry, accounts)
    with Server(app) as server:
        yield {'url': server.url, 'database': database, 'runner': runner, 'registry': registry}


@pytest.fixture
def setup_server(tmp_path, caplog):
    caplog.set_level('WARNING', logger='team_memory.setup')
    _registry, _accounts, _pool, database, runner, _metrics, app = build(tmp_path / 'root', steps=1)
    with Server(app) as server:
        match = CODE.search(caplog.text)
        assert match, caplog.text
        yield {'url': server.url, 'token': match.group(1), 'database': database, 'runner': runner}


@pytest.fixture(params=['chromium', 'firefox', 'webkit'])
def browser(request):
    with playwright.sync_playwright() as engine:
        instance = getattr(engine, request.param).launch()
        yield instance
        instance.close()


def watch(page):
    """Collect page errors and every JSON body the browser receives, to prove the secret never arrives."""
    seen = {'errors': [], 'bodies': []}
    page.on('pageerror', lambda error: seen['errors'].append(str(error)))

    def keep(response):
        if '/dashboard/api/' in response.url:
            try:
                seen['bodies'].append(response.text())
            except playwright.Error:
                seen['bodies'].append('')
    page.on('response', keep)
    return seen


def sign_in(page, url):
    page.goto(url + '/dashboard/#database')
    page.locator('#form-password [name=user_id]').fill('root')
    page.locator('#form-password [name=password]').fill('correct horse battery staple')
    page.locator('#form-password button[type=submit]').click()
    card = page.locator('section.card').filter(has=page.get_by_role('heading', name='Database', exact=True))
    playwright.expect(card).to_be_visible()
    nav = page.get_by_role('navigation')
    playwright.expect(nav.get_by_role('link', name='Database', exact=True)).to_have_attribute('aria-current', 'page')
    page.get_by_role('link', name='Providers', exact=True).click()
    playwright.expect(page.locator('#view').get_by_role('heading', name='Language model')).to_be_visible()
    playwright.expect(page.locator('#view').get_by_role('heading', name='Database', exact=True)).to_have_count(0)
    page.get_by_role('link', name='Database', exact=True).click()
    playwright.expect(card).to_be_visible()
    return card


def assert_clean(page, seen):
    assert not seen['errors'], seen['errors']
    text = page.content() + '\n'.join(seen['bodies'])
    for secret in SECRETS:
        assert secret not in text, secret


def test_migrate_then_roll_back_from_settings(browser, admin_server):
    context = browser.new_context()
    page = context.new_page()
    seen = watch(page)
    try:
        card = sign_in(page, admin_server['url'])
        playwright.expect(card.get_by_role('radio', name='SQLite')).to_have_attribute('aria-checked', 'true')
        card.get_by_role('radio', name='PostgreSQL').click()
        dsn = card.get_by_role('textbox', name='Connection string', exact=True)
        playwright.expect(dsn).to_have_attribute('type', 'password')
        playwright.expect(dsn).to_have_value('')
        dsn.fill(DSN)
        card.get_by_role('button', name='Test', exact=True).click()
        result = card.get_by_role('region', name='Connection test result')
        playwright.expect(result.locator('li')).to_have_count(6)
        playwright.expect(result).to_contain_text(MASK)
        migrate = card.get_by_role('button', name='Migrate', exact=True)
        playwright.expect(migrate).to_be_disabled()
        card.get_by_role('button', name='Dry run', exact=True).click()
        playwright.expect(card.get_by_role('region', name='Dry run result')).to_contain_text('Rows to copy')
        quarantine = card.get_by_role('region', name='Quarantined rows')
        playwright.expect(quarantine).to_contain_text('3 rows will be set aside')
        quarantine.get_by_text('Quarantined rows by table (2)').click()
        playwright.expect(quarantine).to_contain_text('Audit history (edit history and authorship)')
        playwright.expect(quarantine).to_contain_text('fk:knowledge_nodes.node_id->graph_nodes.id')
        playwright.expect(migrate).to_be_enabled()
        migrate.click()
        dialog = page.get_by_role('dialog')
        start = dialog.get_by_role('button', name='Start migration')
        playwright.expect(start).to_be_disabled()
        dialog.get_by_label(re.compile('Type the database name')).fill('tam_prod')
        playwright.expect(start).to_be_enabled()
        start.click()
        panel = card.get_by_role('region', name='Migration progress')
        playwright.expect(panel).to_be_visible()
        playwright.expect(panel.get_by_role('button', name='Cancel migration')).to_be_visible()
        playwright.expect(panel).to_contain_text('1 set aside')
        playwright.expect(card.get_by_role('radio', name='PostgreSQL')).to_have_attribute('aria-checked', 'true', timeout=15_000)
        playwright.expect(card.locator('.masked code')).to_have_text(MASK)
        playwright.expect(card.get_by_role('textbox', name='New connection string', exact=True)).to_have_attribute('placeholder', MASK)
        playwright.expect(card.get_by_role('textbox', name='New connection string', exact=True)).to_have_value('')
        assert admin_server['runner'].polls >= 3

        card.get_by_role('radio', name='SQLite').click()
        card.get_by_role('button', name='Roll back to SQLite').click()
        dialog = page.get_by_role('dialog')
        playwright.expect(dialog).to_contain_text('is lost')
        confirm = dialog.get_by_role('button', name='Roll back', exact=True)
        dialog.get_by_label(re.compile('Type the organization name')).fill('Wrong Corp')
        playwright.expect(confirm).to_be_disabled()
        dialog.get_by_label(re.compile('Type the organization name')).fill('Acme Corp')
        confirm.click()
        playwright.expect(card.get_by_role('radio', name='SQLite')).to_have_attribute('aria-checked', 'true')
        playwright.expect(card.locator('.masked')).to_have_count(0)
        assert admin_server['runner'].rollbacks == [('Acme Corp', 'root')]
        assert_clean(page, seen)
    finally:
        context.close()


def test_database_card_fits_a_phone_in_both_themes(browser, admin_server):
    context = browser.new_context(viewport={'width': 375, 'height': 812})
    page = context.new_page()
    seen = watch(page)
    try:
        page.goto(admin_server['url'] + '/dashboard/')
        page.locator('#form-password [name=user_id]').fill('root')
        page.locator('#form-password [name=password]').fill('correct horse battery staple')
        page.locator('#form-password button[type=submit]').click()
        playwright.expect(page.locator('#app')).to_be_visible()
        page.goto(admin_server['url'] + '/dashboard/#database')
        card = page.locator('section.card').filter(has=page.get_by_role('heading', name='Database', exact=True))
        playwright.expect(card).to_be_visible()
        card.get_by_role('radio', name='PostgreSQL').click()
        card.get_by_role('textbox', name='Connection string', exact=True).fill(DSN)
        card.get_by_role('button', name='Dry run', exact=True).click()
        playwright.expect(card.get_by_role('region', name='Dry run result')).to_be_visible()
        for theme in ('dark', 'light'):
            page.evaluate('(theme) => { document.documentElement.dataset.theme = theme; }', theme)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), theme
        show = card.locator('.dsn-input button')
        playwright.expect(show).to_have_accessible_name('Show connection string')
        show.click()
        playwright.expect(show).to_have_accessible_name('Hide connection string')
        playwright.expect(card.get_by_role('textbox', name='Connection string', exact=True)).to_have_attribute('type', 'text')
        playwright.expect(show).to_have_attribute('aria-pressed', 'true')
        assert page.evaluate('localStorage.getItem("tam-theme")') is None
        assert_clean(page, seen)
    finally:
        context.close()


def test_setup_wizard_database_step(browser, setup_server):
    context = browser.new_context()
    page = context.new_page()
    seen = watch(page)
    try:
        page.goto(setup_server['url'] + '/dashboard/#setup=' + setup_server['token'])
        heading = page.locator('.setup-head h2')
        playwright.expect(heading).to_have_text('Database')
        playwright.expect(page.locator('.setup-progress')).to_contain_text('Step 2 of 8')
        page.get_by_role('radio', name='PostgreSQL').click()
        page.get_by_role('textbox', name='Connection string', exact=True).fill(DSN)
        page.get_by_role('button', name='Test', exact=True).click()
        playwright.expect(page.get_by_role('region', name='Connection test result').locator('li')).to_have_count(6)
        page.get_by_role('button', name='Use PostgreSQL and continue').click()
        playwright.expect(heading).to_have_text('Company', timeout=15_000)
        assert setup_server['runner'].job.started_by == 'setup'
        page.get_by_role('button', name='Back').click()
        playwright.expect(heading).to_have_text('Database')
        playwright.expect(page.locator('.setup-panel')).to_contain_text('already uses PostgreSQL')
        assert_clean(page, seen)
    finally:
        context.close()


def test_lost_lease_shows_a_banner_instead_of_controls(browser, admin_server):
    context = browser.new_context()
    page = context.new_page()
    seen = watch(page)
    try:
        card = sign_in(page, admin_server['url'])
        admin_server['runner'].enter(MaintenanceReason.LEASE_LOST, None)
        page.reload()
        banner = card.get_by_role('alert')
        playwright.expect(banner).to_contain_text('Another TAM server is using this database')
        playwright.expect(banner).to_contain_text('restart this server')
        playwright.expect(card.get_by_role('radio')).to_have_count(0)
        playwright.expect(card.get_by_role('region', name='Migration progress')).to_have_count(0)
        playwright.expect(card.get_by_role('button', name='Cancel migration')).to_have_count(0)
        assert_clean(page, seen)
    finally:
        context.close()

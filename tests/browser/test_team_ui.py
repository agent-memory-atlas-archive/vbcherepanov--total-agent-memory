import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from uuid import uuid4

import pytest

from tests.smoke.installed_runtime import Scenario

playwright = pytest.importorskip('playwright.sync_api')


@pytest.fixture(scope='module')
def server(tmp_path_factory):
    scenario = Scenario(tmp_path_factory.mktemp('browser-server'), 'wheel')
    scenario.seed()
    with scenario.server():
        yield scenario


@pytest.fixture(params=['chromium', 'firefox', 'webkit'])
def browser(request):
    with playwright.sync_playwright() as engine:
        instance = getattr(engine, request.param).launch()
        yield instance
        instance.close()


def login(page, server, user):
    page.goto(server.url)
    page.locator('#token').fill((server.root / f'{user}.token').read_text().strip())
    page.get_by_role('button', name='Подключиться', exact=True).click()
    playwright.expect(page.locator('#main')).to_be_visible()


def save(page, scope, content):
    page.locator('#writeScope').select_option(label=scope)
    page.locator('#content').fill(content)
    page.get_by_role('button', name='Сохранить', exact=True).click()
    playwright.expect(page.locator('#results')).to_contain_text(content, timeout=60_000)


def browse(page, scope):
    page.locator('#searchScope').select_option(label=scope)
    page.get_by_role('button', name='Просмотреть выбранную область').click()
    playwright.expect(page.locator('#status')).to_have_text('Готово', timeout=60_000)


def test_authorship_scopes_edit_history_and_search(browser, server):
    marker = uuid4().hex
    context = browser.new_context()
    vasya, petya = context.new_page(), context.new_page()
    errors = []
    for page in (vasya, petya):
        page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        login(vasya, server, 'vasya')
        login(petya, server, 'petya')
        from version import RELEASE_DATE, VERSION
        playwright.expect(vasya.get_by_role('heading', name='total-agent-memory', exact=True)).to_be_visible()
        playwright.expect(vasya.locator('small')).to_have_text(f'{VERSION} · {RELEASE_DATE} · Панель компании')
        dashboard_link = vasya.locator('small').get_by_role('link', name='Панель компании', exact=True)
        playwright.expect(dashboard_link).to_have_attribute('href', '/dashboard/')
        private = f'Личный секрет Васи {marker}: сервер стоит в кабинете 402.'
        team = f'Команда {marker}: выпуск согласован на пятницу.'
        shared = f'Общая инструкция {marker}: проверять резервную копию ежедневно.'
        save(vasya, 'Личная', private)
        save(vasya, 'Команда: engineering', team)
        save(vasya, 'Общая', shared)
        browse(petya, 'Личная')
        playwright.expect(petya.locator('#results')).not_to_contain_text(private)
        browse(petya, 'Общая')
        playwright.expect(petya.locator('#results')).to_contain_text(shared)
        browse(petya, 'Команда: engineering')
        card = petya.locator('article').filter(has_text=team)
        playwright.expect(card).to_contain_text('Автор: Вася')
        card.get_by_role('button', name='Редактировать', exact=True).click()
        changed = f'Команда {marker}: выпуск перенесён на понедельник.'
        card.get_by_label('Новый текст').fill(changed)
        card.get_by_label('Причина изменения').fill('Петя согласовал новый срок')
        card.get_by_role('button', name='Сохранить правку').click()
        playwright.expect(petya.locator('#results')).to_contain_text(changed, timeout=60_000)
        playwright.expect(petya.locator('#results')).to_contain_text('Автор: Вася · Последняя правка: Петя')
        petya.get_by_role('button', name='История', exact=True).click()
        playwright.expect(petya.locator('#results')).to_contain_text('Петя согласовал новый срок')
        playwright.expect(petya.locator('#results')).to_contain_text(team)
        vasya.locator('#query').fill(marker)
        vasya.get_by_role('button', name='Найти', exact=True).click()
        playwright.expect(vasya.locator('#results')).to_contain_text(changed, timeout=60_000)
        assert not errors, errors
    finally:
        context.close()


def test_mobile_safe_rendering_and_logout(browser, server):
    context = browser.new_context(viewport={'width': 375, 'height': 812})
    page = context.new_page()
    try:
        login(page, server, 'vasya')
        content = f'Проверка HTML {uuid4().hex}: <img src=x onerror="window.injected=true">'
        save(page, 'Личная', content)
        assert page.evaluate('window.injected') is None
        assert page.locator('#results img').count() == 0
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert page.evaluate('localStorage.length + sessionStorage.length') == 0
        page.get_by_role('button', name='Выйти', exact=True).click()
        playwright.expect(page.locator('#main')).to_be_hidden()
        playwright.expect(page.locator('#results')).to_be_empty()
        playwright.expect(page.locator('#token')).to_have_value('')
        login(page, server, 'petya')
        browse(page, 'Личная')
        playwright.expect(page.locator('#results')).not_to_contain_text(content)
    finally:
        context.close()


def test_reader_cannot_edit_team_records(browser, server):
    user = 'reader_' + uuid4().hex
    server.cli('user-add', user, 'Читатель')
    server.cli('member', user, 'engineering', 'reader')
    server.cli('token-create', user, '--client', 'browser', '--out', server.root / f'{user}.token')
    server.call('vasya', 'memory_save', {'scope': {'kind': 'team', 'team_id': 'engineering'},
                'content': f'Доступ читателя {user}: договор подписан.'})
    context = browser.new_context()
    page = context.new_page()
    try:
        login(page, server, user)
        writable = page.locator('#writeScope option').evaluate_all('(items)=>items.map(x=>JSON.parse(x.value))')
        assert all(scope['kind'] != 'team' for scope in writable), json.dumps(writable)
        browse(page, 'Команда: engineering')
        playwright.expect(page.locator('#results')).to_contain_text(user)
        assert page.get_by_role('button', name='Редактировать', exact=True).count() == 0
        server.cli('token-revoke', '--file', server.root / f'{user}.token')
        page.get_by_role('button', name='Просмотреть выбранную область').click()
        playwright.expect(page.locator('#status')).to_contain_text('Invalid or revoked token')
    finally:
        context.close()


@pytest.fixture(scope='module')
def dashboard(server):
    import total_agent_memory
    from team_memory.registry import Registry
    registry = Registry(server.root)
    actor = registry.authenticate((server.root / 'vasya.token').read_text().strip())
    workspace = next(w for w in registry.workspaces(actor) if w.scope.kind.value == 'personal')
    server.call('vasya', 'memory_save', {'content': 'Browser dashboard fixture: launch checklist approved.'})
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    url = f'http://127.0.0.1:{port}'
    env = {**os.environ, 'TAM_MEMORY_DIR': str(server.root / 'workspaces' / workspace.key),
           'DASHBOARD_PORT': str(port), 'DASHBOARD_BIND': '127.0.0.1'}
    with tempfile.TemporaryFile() as log:
        script = Path(total_agent_memory.__file__).resolve().parent.parent / 'src/dashboard.py'
        process = subprocess.Popen([sys.executable, str(script)],
                                   env=env, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 30
            while True:
                try:
                    with urllib.request.urlopen(url + '/api/release', timeout=2) as response:
                        assert response.status == 200
                    break
                except OSError:
                    if process.poll() is not None or time.monotonic() >= deadline:
                        log.seek(0)
                        raise AssertionError(log.read().decode()) from None
                    time.sleep(0.1)
            yield url
        finally:
            process.terminate()
            process.wait(timeout=10)


def test_local_dashboard_tabs_and_record_details(browser, dashboard):
    from version import RELEASE_DATE, VERSION
    context = browser.new_context()
    page = context.new_page()
    errors, failed = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('response', lambda response: failed.append((response.url, response.status))
            if '/api/' in response.url and response.status >= 400 else None)
    try:
        page.goto(dashboard)
        playwright.expect(page.get_by_role('heading', name='total-agent-memory', exact=True)).to_be_visible()
        playwright.expect(page.locator('body')).to_contain_text(VERSION)
        playwright.expect(page.locator('body')).to_contain_text(RELEASE_DATE)
        page.locator('#search-input').fill('Browser dashboard fixture')
        row = page.locator('#knowledge-body tr').filter(has_text='Browser dashboard fixture')
        playwright.expect(row).to_have_count(1)
        row.click()
        playwright.expect(page.locator('#detail-modal')).to_be_visible()
        playwright.expect(page.locator('#detail-modal')).to_contain_text('launch checklist approved')
        page.locator('#modal-close').click()
        for tab in page.locator('button[data-tab]').all():
            name = tab.get_attribute('data-tab')
            tab.click()
            playwright.expect(page.locator('#tab-' + name)).to_be_visible()
            page.wait_for_load_state('networkidle')
        assert not errors, errors
        assert not failed, failed
    finally:
        context.close()

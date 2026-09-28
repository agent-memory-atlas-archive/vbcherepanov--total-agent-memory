"""The local dashboard settings page in real browsers: edit, save encrypted, scan and redact."""

import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

playwright = pytest.importorskip('playwright.sync_api')

ROOT = Path(__file__).resolve().parents[2]
KEY = 'sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789'


def _port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


@pytest.fixture
def dashboard(tmp_path):
    sys.path.insert(0, str(ROOT / 'src'))
    env = {**os.environ, 'TAM_MEMORY_DIR': str(tmp_path), 'MCP_TRANSPORT': 'stdio'}
    seed = ('import server; s = server.Store(); '
            's.db.execute("INSERT INTO knowledge (session_id,type,content,created_at,last_confirmed) '
            "VALUES ('s','fact','old key " + KEY + "','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')\"); "
            's.db.commit()')
    subprocess.run([sys.executable, '-c', seed], cwd=ROOT / 'src', env=env, check=True, timeout=180)
    port = _port()
    process = subprocess.Popen([sys.executable, str(ROOT / 'src' / 'dashboard.py')],
                               env={**env, 'DASHBOARD_PORT': str(port)}, cwd=ROOT / 'src')
    url = f'http://127.0.0.1:{port}'
    for _ in range(120):
        try:
            urllib.request.urlopen(url + '/api/release', timeout=1)
            break
        except OSError:
            time.sleep(0.5)
    yield url, tmp_path
    process.terminate()
    process.wait(30)


@pytest.fixture(params=['chromium', 'firefox', 'webkit'])
def browser(request):
    with playwright.sync_playwright() as engine:
        instance = getattr(engine, request.param).launch()
        yield instance
        instance.close()


def test_settings_page_saves_encrypted_and_cleans_stored_keys(browser, dashboard):
    url, root = dashboard
    page = browser.new_page(viewport={'width': 390, 'height': 900})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(url + '/settings')
    playwright.expect(page.get_by_role('heading', name='Search answers')).to_be_visible()
    page.locator('#f-MEMORY_LLM_PROVIDER').select_option('anthropic')
    playwright.expect(page.locator('#f-MEMORY_LLM_MODEL')).to_be_visible()
    page.locator('#f-MEMORY_RECALL_MAX_RESULT_CHARS').fill('2500')
    page.locator('section', has_text='Language model').get_by_role('button', name='Add key').first.click()
    page.locator('#f-ANTHROPIC_API_KEY').fill(KEY)
    playwright.expect(page.locator('#pending')).to_have_text('3 unsaved changes')
    page.get_by_role('button', name='Save changes').click()
    playwright.expect(page.locator('.msg.good')).to_contain_text('Saved')
    playwright.expect(page.locator('#f-MEMORY_RECALL_MAX_RESULT_CHARS')).to_have_value('2500')
    playwright.expect(page.locator('section', has_text='Language model')).to_contain_text('••••6789')
    assert KEY not in (root / 'settings.json').read_text()
    assert KEY not in page.content()

    page.get_by_role('button', name='Scan memory').click()
    playwright.expect(page.locator('#privacy-msg')).to_contain_text('Found: 1 row(s)', timeout=60_000)
    redact = page.get_by_role('button', name='Back up and redact')
    redact.click()
    page.get_by_role('button', name='Click again to back up and redact').click()
    playwright.expect(page.locator('#privacy-msg')).to_contain_text('Redacted: 1 row(s)', timeout=60_000)
    playwright.expect(page.locator('#privacy-msg')).to_contain_text('pre-redact-')
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
    assert errors == []


def test_no_null_or_undefined_text_leaks_into_the_page(browser, dashboard):
    url, _ = dashboard
    page = browser.new_page()
    page.goto(url + '/settings')
    playwright.expect(page.get_by_role('heading', name='Storage')).to_be_visible()
    page.get_by_role('button', name='Scan memory').click()
    playwright.expect(page.locator('#privacy-msg')).to_contain_text('Found', timeout=60_000)
    text = page.locator('body').inner_text()
    assert 'null' not in text and 'undefined' not in text

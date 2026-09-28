"""Local dashboard settings: encrypted saves, masked reads, and writes only from the page itself."""

from __future__ import annotations

import http.client
import json
import socket
import sys
import threading
from http.server import HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import dashboard
import stored_secrets

KEY = "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def served(tmp_path, monkeypatch):
    import server
    for sub in ("raw", "blobs", "chroma", "backups"):
        (tmp_path / sub).mkdir(exist_ok=True)
    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    store = server.Store()
    store.db.execute("INSERT INTO knowledge (session_id, type, content, created_at, last_confirmed) "
                     "VALUES ('s','fact',?, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')", (f"old key {KEY}",))
    store.db.commit()
    store.db.close()
    monkeypatch.setattr(dashboard, "DB_PATH", tmp_path / "memory.db")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    class _ThreadedHTTP(HTTPServer):
        daemon_threads = True

    httpd = _ThreadedHTTP(("127.0.0.1", _free_port()), dashboard.DashboardHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1], tmp_path
    httpd.shutdown()
    httpd.server_close()


def call(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    try:
        payload = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        conn.request(method, path, body=payload, headers={"Host": f"127.0.0.1:{port}", **(headers or {})})
        response = conn.getresponse()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
    finally:
        conn.close()


def page_headers(port):
    return {"Origin": f"http://127.0.0.1:{port}", "X-TAM-CSRF": dashboard.CSRF_TOKEN,
            "Content-Type": "application/json"}


def test_settings_page_carries_the_token_and_a_strict_csp(served):
    port, _ = served
    status, headers, body = call(port, "GET", "/settings")
    html = body.decode()
    assert status == 200
    assert f'content="{dashboard.CSRF_TOKEN}"' in html
    assert "default-src 'none'" in headers["content-security-policy"]
    assert "http://" not in html and "https://" not in html


def test_save_encrypts_the_key_and_reads_back_only_a_hint(served):
    port, root = served
    status, _, body = call(port, "POST", "/api/settings",
                           {"values": {"ANTHROPIC_API_KEY": KEY, "MEMORY_RECALL_MAX_RESULT_CHARS": "2500"}},
                           page_headers(port))
    assert status == 200, body
    assert json.loads(body)["changed"] == ["ANTHROPIC_API_KEY", "MEMORY_RECALL_MAX_RESULT_CHARS"]
    assert KEY not in (root / "settings.json").read_text()
    status, _, body = call(port, "GET", "/api/settings")
    assert KEY.encode() not in body
    views = {v["key"]: v for v in json.loads(body)["settings"]}
    assert views["ANTHROPIC_API_KEY"]["hint"] == "••••6789" and views["ANTHROPIC_API_KEY"]["value"] is None
    assert views["MEMORY_RECALL_MAX_RESULT_CHARS"]["value"] == "2500"


@pytest.mark.parametrize(("drop", "override", "status"), [
    ("Origin", {}, 403),
    ("X-TAM-CSRF", {}, 403),
    (None, {"X-TAM-CSRF": "stale"}, 403),
    (None, {"Origin": "http://evil.example"}, 403),
    (None, {"Content-Type": "text/plain"}, 415),
])
def test_writes_from_anywhere_but_the_page_are_refused(served, drop, override, status):
    port, root = served
    headers = {**page_headers(port), **override}
    headers.pop(drop, None)
    got, _, _ = call(port, "POST", "/api/settings", {"values": {"MEMORY_FLAG_INSTRUCTIONS": "false"}}, headers)
    assert got == status
    assert not (root / "settings.json").exists()


def test_invalid_values_and_bodies_are_a_400(served):
    port, root = served
    for body in ({"values": {"MEMORY_RECALL_MAX_RESULT_CHARS": "lots"}}, {"values": {}}, {"nope": 1}, b"{broken"):
        status, _, _ = call(port, "POST", "/api/settings", body, page_headers(port))
        assert status == 400, body
    assert not (root / "settings.json").exists()


def test_other_post_paths_stay_read_only(served):
    port, _ = served
    assert call(port, "POST", "/api/knowledge", {}, page_headers(port))[0] == 405


def test_scan_then_redact_from_the_page(served):
    port, root = served
    status, _, body = call(port, "GET", "/api/privacy/scan")
    assert status == 200 and json.loads(body)["rows"] == 1
    assert call(port, "POST", "/api/privacy/redact", {}, {"Origin": f"http://127.0.0.1:{port}"})[0] == 403
    status, _, body = call(port, "POST", "/api/privacy/redact", {}, page_headers(port))
    result = json.loads(body)
    assert status == 200 and result["rows"] == 1
    assert Path(result["backup"]).parent == root / "backups"
    assert stored_secrets.scan(root)["rows"] == 0

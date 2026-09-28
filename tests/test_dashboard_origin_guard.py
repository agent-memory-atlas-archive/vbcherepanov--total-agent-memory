"""The local dashboard has no auth, so it must not be readable by other web sites.

No CORS header is ever sent, a Host that is not this machine (DNS rebinding) is
refused with 421, and a foreign Origin is refused with 403.
"""

from __future__ import annotations

import http.client
import socket
import sqlite3
import sys
import threading
from http.server import HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import dashboard

SAME_ORIGIN_PATHS = ("/", "/api/stats", "/api/release", "/healthz", "/api/knowledge/abc", "/api/nope")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def port(tmp_path_factory: pytest.TempPathFactory):
    db_path = tmp_path_factory.mktemp("dash_guard") / "memory.db"
    with sqlite3.connect(db_path) as db:
        db.executescript("""
            CREATE TABLE knowledge (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, type TEXT, content TEXT,
                context TEXT DEFAULT '', project TEXT DEFAULT 'general', tags TEXT DEFAULT '[]',
                status TEXT DEFAULT 'active', superseded_by INTEGER, confidence REAL DEFAULT 1.0,
                source TEXT DEFAULT 'explicit', created_at TEXT, last_confirmed TEXT,
                recall_count INTEGER DEFAULT 0, last_recalled TEXT, branch TEXT DEFAULT ''
            );
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, started_at TEXT, ended_at TEXT, project TEXT DEFAULT 'general',
                status TEXT DEFAULT 'open', summary TEXT, log_count INTEGER DEFAULT 0, branch TEXT DEFAULT ''
            );
            INSERT INTO knowledge (session_id, type, content, created_at)
                VALUES ('s1', 'fact', 'secret memory', '2026-09-01T00:00:00Z');
        """)
    original = dashboard.DB_PATH
    dashboard.DB_PATH = db_path

    class _ThreadedHTTP(HTTPServer):
        daemon_threads = True

    server = _ThreadedHTTP(("127.0.0.1", _free_port()), dashboard.DashboardHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()
    dashboard.DB_PATH = original


def request(port: int, method: str, path: str, headers: dict[str, str],
            host: str | None = "default") -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        if host == "default":
            host = f"127.0.0.1:{port}"
        if host is not None:
            conn.putheader("Host", host)
        for name, value in headers.items():
            conn.putheader(name, value)
        conn.endheaders()
        response = conn.getresponse()
        head = {k.lower(): v for k, v in response.getheaders()}
        body = b"" if head.get("content-type", "").startswith("text/event-stream") else response.read()
        return response.status, head, body
    finally:
        conn.close()


@pytest.mark.parametrize("path", SAME_ORIGIN_PATHS)
def test_no_cors_header_on_any_response(port: int, path: str) -> None:
    status, headers, _ = request(port, "GET", path, {})
    assert status in (200, 400, 404)
    assert not any(name.startswith("access-control-") for name in headers)


@pytest.mark.parametrize("host", ("127.0.0.1:{port}", "localhost:{port}", "[::1]:{port}", "LOCALHOST:{port}",
                                  "localhost.:{port}", "127.0.0.1:8080", "127.0.0.1"))
def test_loopback_hosts_are_served(port: int, host: str) -> None:
    status, _, _ = request(port, "GET", "/api/stats", {}, host=host.format(port=port))
    assert status == 200


@pytest.mark.parametrize("host", ("evil.example:{port}", "127.0.0.1.evil.example:{port}", "localhost.evil:{port}",
                                  "10.0.0.7:{port}", "", None))
@pytest.mark.parametrize("path", ("/", "/api/knowledge", "/api/events", "/healthz"))
def test_foreign_host_is_refused(port: int, host: str | None, path: str) -> None:
    status, headers, body = request(port, "GET", path, {}, host=None if host is None else host.format(port=port))
    assert status == 421
    assert b"secret memory" not in body
    assert not any(name.startswith("access-control-") for name in headers)


@pytest.mark.parametrize("origin", ("http://evil.example", "null", "http://localhost:1", "file://",
                                    "http://127.0.0.1.evil.example:{port}"))
@pytest.mark.parametrize("method", ("GET", "POST", "OPTIONS", "DELETE"))
def test_foreign_origin_is_refused(port: int, origin: str, method: str) -> None:
    status, headers, body = request(port, method, "/api/knowledge", {"Origin": origin.format(port=port)})
    assert status == 403
    assert b"secret memory" not in body
    assert not any(name.startswith("access-control-") for name in headers)


def test_same_origin_requests_still_work(port: int) -> None:
    origin = {"Origin": f"http://127.0.0.1:{port}"}
    status, _, body = request(port, "GET", "/api/knowledge", origin)
    assert status == 200 and b"secret memory" in body
    assert request(port, "POST", "/api/knowledge", origin)[0] == 405
    assert request(port, "POST", "/api/knowledge", {})[0] == 405


def test_sse_same_origin_streams_and_foreign_is_refused(port: int) -> None:
    status, headers, _ = request(port, "GET", "/api/events", {"Origin": f"http://localhost:{port}"},
                                 host=f"localhost:{port}")
    assert status == 200 and headers["content-type"] == "text/event-stream"
    assert not any(name.startswith("access-control-") for name in headers)
    assert request(port, "GET", "/api/events", {"Origin": "http://evil.example"})[0] == 403
    assert request(port, "GET", "/api/events", {}, host="evil.example")[0] == 421


def test_extra_hosts_come_from_bind_and_allow_list(port: int, monkeypatch: pytest.MonkeyPatch) -> None:
    assert dashboard.allowed_hosts({}) == {"127.0.0.1", "localhost", "::1"}
    assert dashboard.allowed_hosts({"DASHBOARD_BIND": "0.0.0.0"}) == {"127.0.0.1", "localhost", "::1"}
    assert dashboard.allowed_hosts({"DASHBOARD_BIND": "::"}) == {"127.0.0.1", "localhost", "::1"}
    assert "192.168.1.5" in dashboard.allowed_hosts({"DASHBOARD_BIND": "192.168.1.5"})
    assert "fd00::5" in dashboard.allowed_hosts({"DASHBOARD_BIND": "fd00::5"})
    extra = dashboard.allowed_hosts({"DASHBOARD_ALLOWED_HOSTS": " mem.lan , Other.Example.:8080,,"})
    assert {"mem.lan", "other.example"} <= extra

    assert request(port, "GET", "/api/stats", {}, host=f"mem.lan:{port}")[0] == 421
    monkeypatch.setattr(dashboard, "ALLOWED_HOSTS", extra)
    assert request(port, "GET", "/api/stats", {}, host=f"mem.lan:{port}")[0] == 200
    assert request(port, "GET", "/api/stats", {"Origin": f"http://mem.lan:{port}"}, host=f"mem.lan:{port}")[0] == 200

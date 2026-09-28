"""Local dashboard serves its browser libraries same-origin under a strict CSP.

No CDN: every <script src> must resolve to a vendored file under
src/dashboard_static, served with the right content type, and inline scripts
only run with the per-response nonce.
"""

from __future__ import annotations

import hashlib
import re
import socket
import sys
import threading
import tomllib
import urllib.error
import urllib.request
from fnmatch import fnmatch
from http.server import HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "src" / "dashboard_static"

PAGES = (
    "/",
    "/graph",
    "/graph/live",
    "/graph/hive",
    "/graph/matrix",
    "/knowledge/1",
    "/session/abc-123",
    "/settings",
)

# sha256 of the unmodified npm registry files (integrity-checked on download).
VENDORED = {
    "vendor/vis-network-9.1.6/vis-network.min.js":
        "576bb887733eb01bb52ee75b90ef46d818454de5fddb5b616fb8a298d307ca12",
    "vendor/three-0.155.0/three.min.js":
        "ec0a84377f1dce9d55b98f04ac7057376fa5371c33ab1cd907b85ae5f18fab7e",
    "vendor/3d-force-graph-1.73.0/3d-force-graph.min.js":
        "5f5344882ec803b1ba6f769b96527ebc843cc13a62300d92e44d1b7ff5f33260",
    "vendor/d3-7.9.0/d3.min.js":
        "f2094bbf6141b359722c4fe454eb6c4b0f0e42cc10cc7af921fc158fceb86539",
}
LICENSES = (
    "vendor/vis-network-9.1.6/LICENSE-MIT.txt",
    "vendor/vis-network-9.1.6/LICENSE-APACHE-2.0.txt",
    "vendor/three-0.155.0/LICENSE.txt",
    "vendor/3d-force-graph-1.73.0/LICENSE.txt",
    "vendor/d3-7.9.0/LICENSE.txt",
)

CDN_HOSTS = re.compile(
    r"unpkg\.com|jsdelivr\.net|cdnjs\.cloudflare\.com|googleapis\.com|gstatic\.com|esm\.sh|skypack\.dev"
)
SCRIPT_TAG = re.compile(r"<script\b([^>]*)>", re.IGNORECASE)
INLINE_HANDLER = re.compile(r"""\son[a-z]+\s*=\s*["']""", re.IGNORECASE)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def base_url(tmp_path_factory: pytest.TempPathFactory):
    sys.path.insert(0, str(ROOT / "src"))
    import dashboard

    dashboard.DB_PATH = tmp_path_factory.mktemp("dash_vendor") / "memory.db"

    class _ThreadedHTTP(HTTPServer):
        daemon_threads = True

    server = _ThreadedHTTP(("127.0.0.1", _free_port()), dashboard.DashboardHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def _get(url: str) -> tuple[int, dict[str, str], bytes]:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()


def _csp_directives(header: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for part in header.split(";"):
        tokens = part.split()
        if tokens:
            out[tokens[0]] = tokens[1:]
    return out


@pytest.mark.parametrize("page", PAGES)
def test_page_has_no_external_hosts(base_url: str, page: str) -> None:
    status, _, body = _get(base_url + page)
    assert status == 200
    html = body.decode("utf-8")
    assert "http://" not in html and "https://" not in html
    assert not re.search(r"""(?:src|href)\s*=\s*["']?//""", html)
    assert not CDN_HOSTS.search(html)


@pytest.mark.parametrize("page", PAGES)
def test_page_csp_is_strict_and_matches_nonce(base_url: str, page: str) -> None:
    _, headers, body = _get(base_url + page)
    csp = _csp_directives(headers["content-security-policy"])
    assert csp["default-src"] == ["'none'"]
    assert csp["object-src"] == ["'none'"]
    assert csp["base-uri"] == ["'none'"]
    assert csp["frame-ancestors"] == ["'none'"]
    assert csp["connect-src"] == ["'self'"]
    script_src = csp["script-src"]
    assert "'unsafe-inline'" not in script_src and "'unsafe-eval'" not in script_src
    assert not any("://" in s or s == "*" for s in script_src)
    nonces = [s for s in script_src if s.startswith("'nonce-")]
    assert script_src[0] == "'self'" and len(nonces) == 1
    nonce = nonces[0][len("'nonce-"):-1]
    assert len(nonce) >= 16

    html = body.decode("utf-8")
    for attrs in SCRIPT_TAG.findall(html):
        if "src=" in attrs:
            assert re.search(r'src="/static/vendor/[^"]+\.js"', attrs), attrs
        else:
            assert f'nonce="{nonce}"' in attrs, attrs
    assert not INLINE_HANDLER.search(html), INLINE_HANDLER.search(html)
    assert headers["x-content-type-options"] == "nosniff"


def test_nonce_is_fresh_per_response(base_url: str) -> None:
    first = _get(base_url + "/graph/live")[1]["content-security-policy"]
    second = _get(base_url + "/graph/live")[1]["content-security-policy"]
    assert first != second


def test_every_referenced_script_is_vendored(base_url: str) -> None:
    referenced: set[str] = set()
    for page in PAGES:
        html = _get(base_url + page)[2].decode("utf-8")
        referenced.update(re.findall(r'<script[^>]*\bsrc="/static/([^"]+)"', html))
    assert referenced == set(VENDORED)


@pytest.mark.parametrize("rel,sha256", sorted(VENDORED.items()))
def test_vendored_script_served_verbatim(base_url: str, rel: str, sha256: str) -> None:
    on_disk = (STATIC / rel).read_bytes()
    assert hashlib.sha256(on_disk).hexdigest() == sha256
    status, headers, body = _get(f"{base_url}/static/{rel}")
    assert status == 200
    assert headers["content-type"] == "text/javascript; charset=utf-8"
    assert headers["x-content-type-options"] == "nosniff"
    assert "immutable" in headers["cache-control"]
    assert body == on_disk
    assert not CDN_HOSTS.search(body.decode("utf-8"))


@pytest.mark.parametrize("rel", LICENSES)
def test_license_files_served_as_text(base_url: str, rel: str) -> None:
    status, headers, body = _get(f"{base_url}/static/{rel}")
    assert status == 200
    assert headers["content-type"] == "text/plain; charset=utf-8"
    assert b"Permission" in body or b"Apache License" in body


@pytest.mark.parametrize(
    "path",
    (
        "/static/../dashboard.py",
        "/static/vendor/../../dashboard.py",
        "/static/%2e%2e/dashboard.py",
        "/static/vendor/d3-7.9.0/missing.js",
        "/static/vendor/d3-7.9.0",
        "/static//etc/passwd",
    ),
)
def test_static_rejects_anything_outside_vendor_files(base_url: str, path: str) -> None:
    status, _, body = _get(base_url + path)
    assert status == 404
    assert b"import" not in body


def test_vendored_files_ship_in_wheel_and_sdist() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    patterns = pyproject["tool"]["setuptools"]["package-data"]["src"]
    for rel in (*VENDORED, *LICENSES):
        pkg_rel = f"dashboard_static/{rel}"
        assert any(fnmatch(pkg_rel, pat) for pat in patterns), pkg_rel
    manifest = (ROOT / "MANIFEST.in").read_text().splitlines()
    assert "recursive-include src/dashboard_static *.js *.txt" in manifest
    assert "include THIRD-PARTY-LICENSES.md" in manifest


def test_third_party_licenses_lists_every_vendored_file() -> None:
    notice = (ROOT / "THIRD-PARTY-LICENSES.md").read_text()
    for rel in (*VENDORED, *LICENSES):
        assert Path(rel).name in notice, rel

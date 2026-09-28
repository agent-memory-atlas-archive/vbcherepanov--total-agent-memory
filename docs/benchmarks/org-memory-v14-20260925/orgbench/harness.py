"""Temp-dir team server, admin CLI, HTTP client, environment capture and load guard."""
import hashlib
import json
import os
import platform
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import httpx

BENCH_DIR = Path(__file__).resolve().parents[1]
REPO = BENCH_DIR.parents[2]
# ORGBENCH_SRC runs the same harness against another source tree (used for the before-fix run).
SRC = Path(os.environ.get("ORGBENCH_SRC") or REPO / "src").resolve()
PYTHON = sys.executable
SERVER_START_TIMEOUT_SECONDS = 60
HTTP_TIMEOUT_SECONDS = 300
LOAD_WAIT_SECONDS = 30
LOAD_MAX_WAITS = 40
# MEMORY_CROSS_RERANK for every server/worker; empty keeps the product default (auto). Set by run_all.
CROSS_RERANK = os.environ.get("ORGBENCH_CROSS_RERANK", "")
FASTEMBED_CACHE = Path(os.environ.get("FASTEMBED_CACHE_PATH") or Path(tempfile.gettempdir()) / "fastembed_cache")
# Team server backend (run_all --backend). For "postgres", ORGBENCH_PG_ADMIN_URL holds an admin URI
# allowed to create databases and roles; every scratch dir gets a fresh database meeting the
# prerequisites of docs/TEAM_POSTGRES.md, dropped afterwards with the workspace roles it created.
BACKEND_ENV = "ORGBENCH_BACKEND"
PG_ADMIN_URL_ENV = "ORGBENCH_PG_ADMIN_URL"
BACKENDS = ("sqlite", "postgres")
BACKEND = os.environ.get(BACKEND_ENV, "sqlite")
DATABASE_URL_ENV = "TAM_TEAM_DATABASE_URL"
_DATABASES: dict[Path, str] = {}


def base_env(scratch: Path, workers: int | None = None) -> dict:
    """Environment for server and CLI: temp HOME and memory dir, no LLM, offline model cache."""
    home = scratch / "home"
    memory = scratch / "tam-memory-dir"
    home.mkdir(parents=True, exist_ok=True)
    memory.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("MEMORY_", "TAM_", "CLAUDE_MEMORY", "OPENAI", "ANTHROPIC", "V9_"))}
    env.update(PYTHONPATH=str(SRC), HOME=str(home), TAM_MEMORY_DIR=str(memory), CLAUDE_MEMORY_DIR=str(memory),
               MEMORY_LLM_ENABLED="false", MEMORY_QUALITY_GATE_ENABLED="false", MEMORY_MODE="fast",
               HF_HUB_OFFLINE="1", FASTEMBED_CACHE_PATH=str(FASTEMBED_CACHE), PYTHONHASHSEED="0")
    if workers is not None:
        env["TAM_TEAM_MAX_WORKERS"] = str(workers)
    if CROSS_RERANK:
        env["MEMORY_CROSS_RERANK"] = CROSS_RERANK
    if scratch in _DATABASES:
        env[DATABASE_URL_ENV] = _DATABASES[scratch]
    return env


def apply_env_in_process(scratch: Path, workers: int | None = None) -> dict:
    env = base_env(scratch, workers)
    for key in [k for k in os.environ if k.startswith(("MEMORY_", "TAM_", "CLAUDE_MEMORY", "OPENAI", "ANTHROPIC", "V9_"))]:
        del os.environ[key]
    os.environ.update({k: v for k, v in env.items() if k != "PYTHONPATH"})
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    return env


def admin(root: Path, env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([PYTHON, "-m", "team_memory.cli", "--root", str(root), *args],
                          env=env, capture_output=True, text=True, check=True, timeout=120)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Server:
    def __init__(self, root: Path, env: dict, log: Path):
        self.root, self.env, self.log = root, env, log
        self.port = free_port()
        self.process = None
        self.url = f"http://127.0.0.1:{self.port}"

    def __enter__(self):
        handle = self.log.open("a")
        self.process = subprocess.Popen([PYTHON, "-m", "team_memory.cli", "--root", str(self.root), "serve",
                                         "--port", str(self.port)], env=self.env, stdout=handle, stderr=handle)
        deadline = time.monotonic() + SERVER_START_TIMEOUT_SECONDS
        while True:
            try:
                if httpx.get(self.url + "/healthz", timeout=2).status_code == 200:
                    return self
            except httpx.TransportError:
                if time.monotonic() > deadline or self.process.poll() is not None:
                    raise RuntimeError(f"team server did not start; see {self.log}")
            time.sleep(0.05)

    def __exit__(self, *exc):
        self.process.terminate()
        try:
            self.process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()


class Client:
    def __init__(self, url: str, timeout: float = HTTP_TIMEOUT_SECONDS):
        self.http = httpx.Client(base_url=url, timeout=timeout)

    def call(self, token: str, name: str, arguments: dict | None = None) -> tuple[int, dict]:
        response = self.http.post("/api/call", headers={"Authorization": "Bearer " + token},
                                  json={"name": name, "arguments": arguments or {}})
        try:
            body = response.json()
        except json.JSONDecodeError:
            body = {"error": response.text}
        return response.status_code, body

    def close(self):
        self.http.close()


def team(team_id: str) -> dict:
    return {"kind": "team", "team_id": team_id}


def loadavg() -> list[float]:
    raw = subprocess.run(["sysctl", "-n", "vm.loadavg"], capture_output=True, text=True, check=True).stdout
    return [float(x) for x in raw.strip().strip("{}").split()]


def cores() -> int:
    return os.cpu_count() or 1


def wait_for_quiet(log=print) -> dict:
    """Block until 1-min load <= cores/2. Returns the observed load record."""
    threshold = cores() / 2
    waits = 0
    while True:
        load = loadavg()
        if load[0] <= threshold:
            return {"load": load, "threshold": threshold, "waits": waits}
        if waits >= LOAD_MAX_WAITS:
            return {"load": load, "threshold": threshold, "waits": waits, "gave_up": True}
        log(f"load {load[0]:.2f} > {threshold}; waiting {LOAD_WAIT_SECONDS}s")
        waits += 1
        time.sleep(LOAD_WAIT_SECONDS)


def source_fingerprint() -> dict:
    files = sorted((SRC / "team_memory").glob("*.py"))
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.name.encode() + b"\0" + path.read_bytes())
    diff = subprocess.run(["git", "-C", str(REPO), "diff", "--stat", "--", "src/team_memory", "src/server.py"],
                          capture_output=True, text=True, check=False).stdout.strip()
    return {"src_path": str(SRC), "team_memory_sha256": digest.hexdigest(), "worktree_src_diff_stat": diff}


def environment(workers_default: int) -> dict:
    def sysctl(name):
        return subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, check=False).stdout.strip()
    head = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True, check=False).stdout.strip()
    sys.path.insert(0, str(SRC))
    import fastembed

    from version import RELEASE_DATE, VERSION
    return {
        "base_commit": head, "package_version": VERSION, "release_date": RELEASE_DATE,
        "macos": platform.mac_ver()[0], "kernel": platform.release(), "machine": platform.machine(),
        "cpu": sysctl("machdep.cpu.brand_string"), "logical_cpus": cores(),
        "ram_gib": int(sysctl("hw.memsize")) / 2 ** 30, "python": platform.python_version(),
        "fastembed": fastembed.__version__,
        "embedding_model": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 (FastEmbed, ONNX)",
        "fastembed_cache": str(FASTEMBED_CACHE), "memory_mode": "fast", "llm": "disabled (MEMORY_LLM_ENABLED=false)",
        "tam_team_max_workers_default": workers_default, **source_fingerprint(),
    }


@contextmanager
def scratch_dir(prefix: str):
    """mktemp -d equivalent; removed afterwards. On the postgres backend it also owns a fresh database."""
    import shutil
    path = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        if BACKEND == "postgres":
            with _postgres_database() as url:
                _DATABASES[path] = url
                try:
                    yield path
                finally:
                    _DATABASES.pop(path, None)
        else:
            yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@contextmanager
def _postgres_database():
    """The test suite's fresh_database (tests/pg_support.py) against ORGBENCH_PG_ADMIN_URL."""
    admin_url = os.environ.get(PG_ADMIN_URL_ENV, "").strip()
    if not admin_url:
        raise RuntimeError(f"--backend postgres needs {PG_ADMIN_URL_ENV}")
    for path in (str(REPO), str(SRC)):
        if path not in sys.path:
            sys.path.insert(0, path)
    from tests.pg_support import PgServer, fresh_database

    with fresh_database(PgServer(admin_url=admin_url)) as database:
        yield database.url


def write_jsonl(path: Path, rows) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            count += 1
    return count

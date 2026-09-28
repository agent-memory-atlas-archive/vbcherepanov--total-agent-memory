"""Replicate -> restore round trip with the real Litestream image and an S3-compatible store (RustFS).

Skipped when Docker is unavailable or the images cannot be pulled. SQLite writes happen inside a
container on a Docker volume, next to Litestream, so WAL shared memory never crosses a VM file-share
boundary (Docker Desktop). Restores go through scripts/litestream-docker into pytest's tmp_path.
"""
import json
import os
import shutil
import socket
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from team_memory import replication
from team_memory.contracts import Unavailable
from team_memory.registry import Registry
from team_memory.replica_store import Credentials, S3Store
from team_memory.replication import Litestream, ReplicationSettings

ROOT = Path(__file__).resolve().parents[1]
LITESTREAM_IMAGE = os.environ.get("TAM_LITESTREAM_IMAGE", "litestream/litestream:0.5.17")
S3_IMAGE = os.environ.get("TAM_S3_LOCAL_IMAGE", "rustfs/rustfs:1.0.0")
WRITER_IMAGE = "python:3.13-slim"
KEYS = {"AWS_ACCESS_KEY_ID": "tamroundtrip", "AWS_SECRET_ACCESS_KEY": "tam-round-trip-secret"}
DOCKER_TIMEOUT = 300
WAIT_SECONDS = 90
TEAM = "team_" + "c" * 64
PERSONAL = "personal_" + "d" * 64
LATE = "team_" + "e" * 64
ORIGINAL = ["identity.db", "learning.db", "workspaces/shared/memory.db", f"workspaces/{TEAM}/memory.db",
            f"workspaces/{PERSONAL}/memory.db"]

WRITE = """
import json, os, sqlite3, sys
paths, first, last, journal = json.loads(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
for relative in paths:
    path = os.path.join('/team-data', relative)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.execute('PRAGMA busy_timeout=5000')
    if journal != 'keep':
        db.execute('PRAGMA journal_mode=' + journal)
    db.execute('CREATE TABLE IF NOT EXISTS t(x INTEGER PRIMARY KEY, body TEXT)')
    db.executemany('INSERT INTO t VALUES (?,?)', [(i, relative + ':' + str(i)) for i in range(first, last + 1)])
    db.commit()
    db.close()
"""

DUMP = """
import glob, json, os, sqlite3
out = {}
for path in ['/team-data/identity.db', '/team-data/learning.db', *sorted(glob.glob('/team-data/workspaces/*/memory.db'))]:
    db = sqlite3.connect(path, timeout=10)
    out[os.path.relpath(path, '/team-data')] = db.execute('SELECT x, body FROM t ORDER BY x').fetchall()
    db.close()
print(json.dumps(out))
"""


def docker(*args: str, check: bool = True, timeout: int = DOCKER_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=check)


def ensure_image(image: str) -> None:
    if docker("image", "inspect", image, check=False).returncode != 0 and \
            docker("pull", "-q", image, check=False).returncode != 0:
        pytest.skip(f"Docker image {image} is not available")


@pytest.fixture(scope="module")
def docker_ready():
    if shutil.which("docker") is None:
        pytest.skip("docker CLI is not installed")
    try:
        if docker("info", "--format", "{{.ServerVersion}}", check=False, timeout=30).returncode != 0:
            pytest.skip("Docker daemon is not reachable")
    except subprocess.TimeoutExpired:
        pytest.skip("Docker daemon did not answer")
    for image in (LITESTREAM_IMAGE, S3_IMAGE, WRITER_IMAGE):
        ensure_image(image)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_for(condition, what: str):
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.5)
    raise AssertionError(f"Timed out waiting for {what}")


def rows(path: Path) -> list[list]:
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(path)) as db:
        return [list(row) for row in db.execute("SELECT x, body FROM t ORDER BY x")]


@pytest.fixture
def stack(docker_ready, tmp_path):
    suffix = uuid.uuid4().hex[:10]
    names = {"s3": f"tam-rt-s3-{suffix}", "writer": f"tam-rt-writer-{suffix}", "litestream": f"tam-rt-ls-{suffix}"}
    volume = f"tam-rt-data-{suffix}"
    port = free_port()
    try:
        docker("volume", "create", volume)
        docker("run", "-d", "--name", names["s3"], "-p", f"127.0.0.1:{port}:{port}", "-e", f"RUSTFS_ADDRESS=:{port}",
               "-e", f"RUSTFS_ACCESS_KEY={KEYS['AWS_ACCESS_KEY_ID']}",
               "-e", f"RUSTFS_SECRET_KEY={KEYS['AWS_SECRET_ACCESS_KEY']}", S3_IMAGE)
        docker("run", "-d", "--name", names["writer"], "-v", f"{volume}:/team-data", WRITER_IMAGE, "sleep", "infinity")
        settings = ReplicationSettings(url="s3://tam-roundtrip/company", endpoint=f"http://127.0.0.1:{port}",
                                       snapshot_interval="1h", retention="24h")
        store = S3Store(settings.bucket, settings.region, settings.endpoint, settings.path_style,
                        Credentials.from_env(KEYS))

        def bucket_ready():
            try:
                store.create_bucket()
            except Unavailable:
                return False
            return True

        wait_for(bucket_ready, "the S3 store to accept requests")
        yield {"names": names, "volume": volume, "settings": settings, "store": store, "tmp": tmp_path}
        store.close()
    finally:
        for name in names.values():
            docker("rm", "-f", "-v", name, check=False)
        docker("volume", "rm", "-f", volume, check=False)


def write(stack, paths: list[str], first: int, last: int, journal: str = "keep") -> None:
    docker("exec", stack["names"]["writer"], "python", "-c", WRITE, json.dumps(paths), str(first), str(last), journal)


def replicated(stack) -> dict:
    return replication.replica_databases(stack["settings"], stack["store"])


def advanced(stack, before: dict, paths: list[str]):
    now = replicated(stack)
    return now if all(p in now and (p not in before or now[p].max_txid > before[p].max_txid) for p in paths) else None


def test_litestream_replicate_and_restore_round_trip(stack):
    names, settings, tmp = stack["names"], stack["settings"], stack["tmp"]
    # identity.db / learning.db start in rollback-journal mode like the registry's; workspaces use WAL like Store.
    write(stack, ORIGINAL[:2], 1, 50, "delete")
    write(stack, ORIGINAL[2:], 1, 50, "wal")
    config = tmp / "litestream.yml"
    config.write_text(replication.render_config(Path("/team-data"), settings))
    docker("run", "-d", "--name", names["litestream"], "--network", f"container:{names['s3']}",
           "-v", f"{stack['volume']}:/team-data", "-v", f"{config}:/etc/litestream.yml:ro",
           "-e", f"AWS_ACCESS_KEY_ID={KEYS['AWS_ACCESS_KEY_ID']}",
           "-e", f"AWS_SECRET_ACCESS_KEY={KEYS['AWS_SECRET_ACCESS_KEY']}", LITESTREAM_IMAGE, "replicate")
    first = wait_for(lambda: advanced(stack, {}, ORIGINAL), "the first replica of every database")

    write(stack, ORIGINAL, 51, 100)
    second = wait_for(lambda: advanced(stack, first, ORIGINAL), "the second batch to replicate")
    time.sleep(1.5)
    moment = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    time.sleep(1.5)
    write(stack, ["identity.db", "workspaces/shared/memory.db"], 101, 150)
    write(stack, [f"workspaces/{LATE}/memory.db"], 1, 20, "wal")
    wait_for(lambda: advanced(stack, second, ["identity.db", "workspaces/shared/memory.db",
                                              f"workspaces/{LATE}/memory.db"]),
             "the third batch and the new workspace (directory watch) to replicate")
    docker("stop", "-t", "30", names["litestream"])
    expected = json.loads(docker("exec", names["writer"], "python", "-c", DUMP).stdout)
    assert set(expected) == {*ORIGINAL, f"workspaces/{LATE}/memory.db"}

    litestream = Litestream(binary=str(ROOT / "scripts" / "litestream-docker"), environ={
        **os.environ, **KEYS, "TAM_LITESTREAM_DOCKER_MOUNT": str(tmp.resolve()),
        "TAM_LITESTREAM_DOCKER_NETWORK": f"container:{names['s3']}", "TAM_LITESTREAM_IMAGE": LITESTREAM_IMAGE})
    latest = replication.restore(settings, tmp / "latest", stack["store"], litestream)
    assert sorted(latest.databases) == sorted(expected) and latest.skipped == []
    for relative, content in expected.items():
        assert rows(tmp / "latest" / relative) == content, relative

    past = replication.restore(settings, tmp / "past", stack["store"], litestream, moment)
    assert past.skipped == [f"workspaces/{LATE}/memory.db"]
    for relative in ORIGINAL:
        assert rows(tmp / "past" / relative) == [row for row in expected[relative] if row[0] <= 100], relative

    state = replication.status(tmp / "latest", settings, stack["store"], litestream)
    assert state.problems == [] and {row.state for row in state.databases} == {"replicated"}

    shutil.rmtree(tmp / "latest" / "workspaces" / PERSONAL)
    removed = replication.drop(Registry(tmp / "latest"), settings, stack["store"], PERSONAL)
    assert removed > 0
    assert f"workspaces/{PERSONAL}/memory.db" not in replicated(stack)
    assert f"workspaces/{TEAM}/memory.db" in replicated(stack)

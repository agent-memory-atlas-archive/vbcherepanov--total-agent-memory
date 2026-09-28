import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from team_memory import replication
from team_memory.contracts import Conflict, DomainError, Unavailable
from team_memory.lifecycle import ServerLease
from team_memory.registry import Registry
from team_memory.replica_store import Credentials, FileStore, S3Store, sign_v4
from team_memory.replication import Litestream, ReplicationSettings

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'
HEX = 'a' * 64
TEAM_KEY = 'team_' + 'b' * 64
S3_ENV = {'TAM_TEAM_REPLICA_URL': 's3://tam-backup/prod/team', 'TAM_TEAM_REPLICA_ENDPOINT': 'http://minio.internal.test:9000'}
KEYS = {'AWS_ACCESS_KEY_ID': 'AKIDEXAMPLE', 'AWS_SECRET_ACCESS_KEY': 'secret-example-key'}


def make_db(path: Path, rows=(1,), journal='delete') -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute(f'PRAGMA journal_mode={journal}')
        db.execute('CREATE TABLE IF NOT EXISTS t(x)')
        db.executemany('INSERT INTO t VALUES (?)', [(r,) for r in rows])
    db.close()


def server(tmp_path: Path) -> Path:
    root = tmp_path / 'server'
    Registry(root).add_user('alice', 'Alice')
    return root


def ltx(base: Path, relative: str, txid: int, level='0000') -> None:
    target = base / relative / level / f'{txid:016x}-{txid:016x}.ltx'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b'x' * txid)


def cli(root: Path, env: dict, *args: str) -> subprocess.CompletedProcess:
    environment = {'PATH': os.environ.get('PATH', ''), 'HOME': str(root.parent / 'home'), 'PYTHONPATH': str(SRC),
                   'TAM_MEMORY_DIR': str(root.parent / 'home' / '.tam'), **env}
    return subprocess.run([sys.executable, '-m', 'team_memory.cli', '--root', str(root), 'replication', *args],
                          env=environment, capture_output=True, text=True, timeout=120, check=False)


# Settings

def test_replication_is_off_without_a_replica_url():
    assert ReplicationSettings.from_env({}) is None
    assert ReplicationSettings.from_env({'TAM_TEAM_REPLICA_URL': '  '}) is None


def test_settings_defaults_and_environment_round_trip():
    settings = ReplicationSettings.from_env(S3_ENV)
    assert (settings.bucket, settings.prefix, settings.region) == ('tam-backup', 'prod/team', 'us-east-1')
    assert settings.path_style is True
    assert (settings.sync_interval, settings.snapshot_interval, settings.retention) == ('1s', '24h', '168h')
    again = ReplicationSettings.from_env(settings.environment())
    assert again.model_dump(exclude={'force_path_style'}) == settings.model_dump(exclude={'force_path_style'})
    assert again.path_style == settings.path_style
    aws = ReplicationSettings.from_env({'TAM_TEAM_REPLICA_URL': 's3://tam-backup/'})
    assert aws.path_style is False and aws.prefix == '' and aws.key('identity.db') == 'identity.db'
    assert aws.key('') == ''


@pytest.mark.parametrize('env, message', [
    ({'TAM_TEAM_REPLICA_URL': 's3://key:secret@bucket/path'}, 'credentials'),
    ({'TAM_TEAM_REPLICA_URL': 's3://bucket/path?endpoint=x'}, 'query'),
    ({'TAM_TEAM_REPLICA_URL': 'gs://bucket/path'}, 's3:// or file://'),
    ({'TAM_TEAM_REPLICA_URL': 's3://B/path'}, 'bucket'),
    ({'TAM_TEAM_REPLICA_URL': 's3://bucket/../etc'}, 'Replica path'),
    ({'TAM_TEAM_REPLICA_URL': 'file://relative/dir'}, 'absolute'),
    ({'TAM_TEAM_REPLICA_URL': 'file:///srv/replica', 'TAM_TEAM_REPLICA_ENDPOINT': 'http://x.test'}, 'only to s3'),
    ({**S3_ENV, 'TAM_TEAM_REPLICA_ENDPOINT': 'ftp://x.test'}, 'endpoint'),
    ({**S3_ENV, 'TAM_TEAM_REPLICA_RETENTION': '1h'}, 'at least the snapshot interval'),
    ({**S3_ENV, 'TAM_TEAM_REPLICA_SYNC_INTERVAL': 'soon'}, 'duration'),
    ({**S3_ENV, 'TAM_TEAM_REPLICA_FORCE_PATH_STYLE': 'maybe'}, 'true or false'),
    ({**S3_ENV, 'TAM_TEAM_REPLICA_METRICS_ADDR': 'anywhere'}, 'Metrics address'),
])
def test_invalid_settings_are_domain_errors(env, message):
    with pytest.raises(DomainError, match=message):
        ReplicationSettings.from_env(env)


def test_duration_parsing():
    assert replication.duration_seconds('1h30m') == 5400
    assert replication.duration_seconds('500ms') == 0.5
    with pytest.raises(ValueError):
        replication.duration_seconds('7d')


# Config rendering

def test_generated_config_watches_control_plane_and_workspaces(tmp_path):
    yaml = pytest.importorskip('yaml')
    settings = ReplicationSettings.from_env({**S3_ENV, 'TAM_TEAM_REPLICA_METRICS_ADDR': '127.0.0.1:9090'})
    root = tmp_path / 'srv'
    text = replication.render_config(root, settings)
    config = yaml.safe_load(text)
    assert config['snapshot'] == {'interval': '24h', 'retention': '168h'}
    assert config['logging']['type'] == 'json' and config['addr'] == '127.0.0.1:9090'
    control, workspaces = config['dbs']
    assert (control['dir'], control['pattern'], control['recursive'], control['watch']) == (str(root), '*.db', False, True)
    assert (workspaces['dir'], workspaces['pattern'], workspaces['recursive'], workspaces['watch']) == \
        (str(root / 'workspaces'), 'memory.db', True, True)
    assert control['replica'] == {'url': 's3://tam-backup/prod/team', 'endpoint': 'http://minio.internal.test:9000',
                                  'region': 'us-east-1', 'force-path-style': True, 'sync-interval': '1s'}
    assert workspaces['replica']['url'] == 's3://tam-backup/prod/team/workspaces'
    assert 'AWS_' in text and 'secret' not in text.lower().replace('aws_secret_access_key', '')


def test_file_replica_config_has_no_s3_fields(tmp_path):
    yaml = pytest.importorskip('yaml')
    settings = ReplicationSettings(url='file:///srv/tam-replica')
    config = yaml.safe_load(replication.render_config(tmp_path, settings))
    assert config['dbs'][0]['replica'] == {'url': 'file:///srv/tam-replica', 'sync-interval': '1s'}
    assert 'addr' not in config


def test_compose_template_is_the_rendered_template():
    assert (ROOT / 'docker' / 'litestream.team.yml').read_text() == replication.render_compose_template()


def test_compose_sidecar_passes_every_variable_the_template_uses():
    yaml = pytest.importorskip('yaml')
    compose = yaml.safe_load((ROOT / 'docker-compose.team.yml').read_text())
    sidecar = compose['services']['litestream']
    assert sidecar['profiles'] == ['litestream']
    assert './docker/litestream.team.yml:/etc/litestream.yml:ro' in sidecar['volumes']
    assert 'team-memory-data:/team-data' in sidecar['volumes']
    used = set(re.findall(r'\$\{([A-Z_]+)\}', replication.render_compose_template()))
    assert used <= set(sidecar['environment'])
    assert {'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY'} <= set(sidecar['environment'])
    assert compose['services']['replica-s3']['profiles'] == ['s3-local']
    assert compose['services']['replica-s3-init']['profiles'] == ['s3-local']
    assert 'profiles' not in compose['services']['team-memory']


def test_write_config_is_private_and_creates_the_watched_directory(tmp_path):
    root = server(tmp_path)
    out = tmp_path / 'etc' / 'litestream.yml'
    replication.write_config(root, ReplicationSettings.from_env(S3_ENV), out)
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert stat.S_IMODE((root / 'workspaces').stat().st_mode) == 0o700
    assert str(root.resolve() / 'workspaces') in out.read_text()
    with pytest.raises(Conflict, match='identity database'):
        replication.write_config(tmp_path / 'empty', ReplicationSettings.from_env(S3_ENV), out)


def test_restore_config_points_each_database_at_its_replica(tmp_path):
    yaml = pytest.importorskip('yaml')
    settings = ReplicationSettings.from_env(S3_ENV)
    config = yaml.safe_load(replication.render_restore_config(settings, {
        'identity.db': tmp_path / 'identity.db', f'workspaces/{TEAM_KEY}/memory.db': tmp_path / 'm.db'}))
    assert [entry['replica']['url'] for entry in config['dbs']] == [
        's3://tam-backup/prod/team/identity.db', f's3://tam-backup/prod/team/workspaces/{TEAM_KEY}/memory.db']
    assert config['dbs'][0]['path'] == str(tmp_path / 'identity.db')


# WAL handling

def test_prepare_switches_every_database_to_wal(tmp_path):
    root = server(tmp_path)
    make_db(root / 'learning.db')
    make_db(root / 'workspaces' / 'shared' / 'memory.db')
    make_db(root / 'workspaces' / TEAM_KEY / 'memory.db', journal='wal')
    before = replication.prepare(root)
    assert before == {'identity.db': 'delete', 'learning.db': 'delete', 'workspaces/shared/memory.db': 'delete',
                      f'workspaces/{TEAM_KEY}/memory.db': 'wal'}
    assert {replication.journal_mode(root / path) for path in before} == {'wal'}
    with ServerLease(root), pytest.raises(Conflict, match='stop the server'):
        replication.prepare(root)
    with ServerLease(root / 'workspaces' / 'shared'), pytest.raises(Conflict):
        replication.prepare(root)


def test_team_server_never_forces_a_wal_checkpoint():
    """Litestream owns checkpoints; a TRUNCATE/RESTART checkpoint from the app would race its WAL copy."""
    sources = [*sorted((SRC / 'team_memory').rglob('*.py')), SRC / 'server.py']
    offenders = [str(path) for path in sources
                 if re.search(r'wal_checkpoint\s*\(\s*(TRUNCATE|RESTART|FULL)', path.read_text(), re.IGNORECASE)]
    assert offenders == []


# Replica contents and status

def test_replica_listing_groups_both_litestream_layouts(tmp_path):
    base = tmp_path / 'replica'
    ltx(base, 'identity.db', 1)
    ltx(base, 'identity.db', 7, level='0001')
    ltx(base, f'workspaces/{TEAM_KEY}/memory.db', 3, level='ltx/0')
    ltx(base, 'workspaces/not-a-key/memory.db', 1)
    (base / 'identity.db' / 'notes.txt').write_text('ignored')
    found = replication.replica_databases(ReplicationSettings(url=base.as_uri()), FileStore(base))
    assert list(found) == ['identity.db', f'workspaces/{TEAM_KEY}/memory.db']
    assert found['identity.db'].files == 2 and found['identity.db'].bytes == 8
    assert found['identity.db'].max_txid == f'{7:016x}'


def test_status_reports_pending_orphaned_and_non_wal_databases(tmp_path):
    root = server(tmp_path)
    make_db(root / 'learning.db')
    replica = tmp_path / 'replica'
    ltx(replica, 'identity.db', 1)
    ltx(replica, f'workspaces/personal_{HEX}/memory.db', 1)
    replication.prepare(root)
    make_db(root / 'learning.db', journal='delete')
    litestream = Litestream(binary=str(tmp_path / 'missing-litestream'), environ={})
    result = replication.status(root, ReplicationSettings(url=replica.as_uri()), FileStore(replica), litestream)
    states = {row.path: row.state for row in result.databases}
    assert states == {'identity.db': 'replicated', 'learning.db': 'pending',
                      f'workspaces/personal_{HEX}/memory.db': 'orphaned'}
    assert any('learning.db has no replica' in p for p in result.problems)
    assert any('learning.db is in delete journal mode' in p for p in result.problems)
    assert any(f'--workspace personal_{HEX}' in w for w in result.warnings)
    assert result.litestream is None and any('not available' in w for w in result.warnings)


# Restore

def fake_litestream(tmp_path: Path, sources: dict[str, Path], calls: list) -> Litestream:
    binary = tmp_path / 'bin' / 'litestream'
    binary.parent.mkdir()
    binary.write_text('#!/bin/sh\nexit 0\n')
    binary.chmod(0o755)

    def run(command, **kwargs):
        calls.append(command)
        if command[1] == 'version':
            return subprocess.CompletedProcess(command, 0, 'v0.5.17\n', '')
        output, database = Path(command[command.index('-o') + 1]), command[-1]
        relative = next(rel for rel in sources if database.endswith(rel))
        if sources[relative] is None:
            return subprocess.CompletedProcess(command, 0, '', '')
        if sources[relative] == 'fail':
            return subprocess.CompletedProcess(command, 1, '', 'Error: no matching backup files available')
        shutil.copyfile(sources[relative], output)
        return subprocess.CompletedProcess(command, 0, '', '')

    return Litestream(binary=str(binary), environ={}, run=run)


def test_restore_rebuilds_a_server_from_the_replica(tmp_path):
    source = tmp_path / 'source'
    Registry(source).add_user('alice', 'Alice')
    make_db(source / 'workspaces' / TEAM_KEY / 'memory.db', rows=(1, 2, 3))
    replica = tmp_path / 'replica'
    for relative in ('identity.db', f'workspaces/{TEAM_KEY}/memory.db', f'workspaces/personal_{HEX}/memory.db'):
        ltx(replica, relative, 1)
    calls = []
    litestream = fake_litestream(tmp_path, {'identity.db': source / 'identity.db',
                                            f'workspaces/{TEAM_KEY}/memory.db': source / 'workspaces' / TEAM_KEY / 'memory.db',
                                            f'workspaces/personal_{HEX}/memory.db': None}, calls)
    destination = tmp_path / 'restored'
    result = replication.restore(ReplicationSettings(url=replica.as_uri()), destination, FileStore(replica),
                                 litestream, '2026-09-25T16:30:00+02:00')
    assert result.timestamp == '2026-09-25T14:30:00Z'
    assert result.databases == ['identity.db', f'workspaces/{TEAM_KEY}/memory.db']
    assert result.skipped == [f'workspaces/personal_{HEX}/memory.db']
    assert Registry(destination).list_users()[0]['id'] == 'alice'
    with sqlite3.connect(destination / 'workspaces' / TEAM_KEY / 'memory.db') as db:
        assert [row[0] for row in db.execute('SELECT x FROM t')] == [1, 2, 3]
    assert not (destination / 'workspaces' / f'personal_{HEX}').exists()
    assert stat.S_IMODE((destination / 'identity.db').stat().st_mode) == 0o600
    marker = json.loads((destination / 'restored-from.json').read_text())
    assert marker['timestamp'] == '2026-09-25T14:30:00Z' and marker['replica'] == replica.as_uri()
    identity_call = calls[0]
    assert identity_call[identity_call.index('-timestamp') + 1] == '2026-09-25T14:30:00Z'
    assert '-if-replica-exists' not in identity_call and '-if-replica-exists' in calls[1]
    assert identity_call[-1] == str(destination.resolve() / 'identity.db')
    assert not list(tmp_path.glob('.tam-restore-*'))
    with pytest.raises(FileExistsError):
        replication.restore(ReplicationSettings(url=replica.as_uri()), destination, FileStore(replica), litestream)


def test_restore_failure_publishes_nothing(tmp_path):
    source = tmp_path / 'source'
    Registry(source)
    replica = tmp_path / 'replica'
    ltx(replica, 'identity.db', 1)
    ltx(replica, 'learning.db', 1)
    litestream = fake_litestream(tmp_path, {'identity.db': source / 'identity.db', 'learning.db': 'fail'}, [])
    destination = tmp_path / 'restored'
    with pytest.raises(Conflict, match='no matching backup files'):
        replication.restore(ReplicationSettings(url=replica.as_uri()), destination, FileStore(replica), litestream)
    assert not destination.exists() and not list(tmp_path.glob('.tam-restore-*'))


def test_restore_requires_identity_and_a_litestream_binary(tmp_path):
    replica = tmp_path / 'replica'
    ltx(replica, 'learning.db', 1)
    settings = ReplicationSettings(url=replica.as_uri())
    missing = Litestream(binary=str(tmp_path / 'nope'), environ={})
    with pytest.raises(Conflict, match='holds no identity.db'):
        replication.restore(settings, tmp_path / 'out', FileStore(replica), missing)
    ltx(replica, 'identity.db', 1)
    with pytest.raises(Conflict, match='not installed'):
        replication.restore(settings, tmp_path / 'out', FileStore(replica), missing)
    identity_missing_then = fake_litestream(tmp_path, {'identity.db': None, 'learning.db': None}, [])
    with pytest.raises(Conflict, match='later --timestamp'):
        replication.restore(settings, tmp_path / 'out', FileStore(replica), identity_missing_then, '2020-01-01T00:00:00Z')
    assert not (tmp_path / 'out').exists()


@pytest.mark.parametrize('value, expected', [('2026-09-25T14:30:00Z', '2026-09-25T14:30:00Z'),
                                             ('2026-09-25T17:30:00.5+03:00', '2026-09-25T14:30:00.500000Z')])
def test_timestamps_are_normalized_to_utc(value, expected):
    assert replication.normalize_timestamp(value) == expected


@pytest.mark.parametrize('value', ['2026-09-25T14:30:00', 'yesterday'])
def test_timestamps_need_iso_format_and_zone(value):
    with pytest.raises(DomainError):
        replication.normalize_timestamp(value)


# Drop

def test_drop_removes_only_a_purged_workspace_replica(tmp_path):
    root = server(tmp_path)
    registry = Registry(root)
    replica = tmp_path / 'replica'
    personal = f'personal_{HEX}'
    for relative in ('identity.db', f'workspaces/{personal}/memory.db', f'workspaces/{TEAM_KEY}/memory.db'):
        ltx(replica, relative, 1)
        ltx(replica, relative, 2)
    settings = ReplicationSettings(url=replica.as_uri())
    (root / 'workspaces' / personal).mkdir(parents=True)
    with pytest.raises(Conflict, match='still exists'):
        replication.drop(registry, settings, FileStore(replica), personal)
    (root / 'workspaces' / personal).rmdir()
    assert replication.drop(registry, settings, FileStore(replica), personal) == 2
    assert set(replication.replica_databases(settings, FileStore(replica))) == {'identity.db',
                                                                                f'workspaces/{TEAM_KEY}/memory.db'}
    assert replication.drop(registry, settings, FileStore(replica), personal) == 0
    events = [(e['subject'], e['detail']) for e in registry.audit_events(action='replica_dropped')]
    assert events == [(personal, 'objects=0'), (personal, 'objects=2')]
    for bad in ('../identity.db', 'identity.db', 'team_xyz'):
        with pytest.raises(DomainError):
            replication.drop(registry, settings, FileStore(replica), bad)


def test_file_store_refuses_paths_outside_the_replica(tmp_path):
    store = FileStore(tmp_path / 'replica')
    with pytest.raises(Conflict):
        store.delete_prefix('../outside/')
    with pytest.raises(Conflict):
        store.delete_prefix('no-trailing-slash')


# S3 client

def test_signature_matches_the_aws_documentation_examples():
    credentials = Credentials(access_key='AKIAIOSFODNN7EXAMPLE', secret_key='wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY')
    moment = datetime(2013, 5, 24, tzinfo=UTC)
    empty = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'
    get = sign_v4('GET', 'examplebucket.s3.amazonaws.com', '/test.txt', {}, {'Range': 'bytes=0-9'}, empty,
                  credentials, 'us-east-1', moment)
    assert get['authorization'].endswith('Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41')
    listing = sign_v4('GET', 'examplebucket.s3.amazonaws.com', '/', {'max-keys': '2', 'prefix': 'J'}, {}, empty,
                      credentials, 'us-east-1', moment)
    assert listing['authorization'].endswith('Signature=34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7')


def listing_page(keys, token=None):
    contents = ''.join(f'<Contents><Key>{k}</Key><LastModified>2026-09-25T10:00:0{i}.000Z</LastModified>'
                       f'<Size>{i + 1}</Size></Contents>' for i, k in enumerate(keys))
    truncated = f'<IsTruncated>true</IsTruncated><NextContinuationToken>{token}</NextContinuationToken>' if token \
        else '<IsTruncated>false</IsTruncated>'
    return (f'<?xml version="1.0"?><ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            f'{contents}{truncated}</ListBucketResult>').encode()


def test_s3_store_pages_listing_and_deletes_each_object():
    requests = []
    pages = {None: listing_page(['p/workspaces/k/memory.db/0000/a.ltx'], token='next'),
             'next': listing_page(['p/workspaces/k/memory.db/0001/b.ltx'])}
    deleted = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers['authorization'].startswith('AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/')
        assert request.url.host == 'minio.test' and request.url.path.startswith('/tam/')
        if request.method == 'DELETE':
            deleted.append(request.url.path)
            return httpx.Response(204)
        if deleted:
            return httpx.Response(200, content=listing_page([]))
        return httpx.Response(200, content=pages[request.url.params.get('continuation-token')])

    store = S3Store('tam', 'us-east-1', 'http://minio.test:9000', True, Credentials(access_key='AKIDEXAMPLE',
                    secret_key='s'), httpx.MockTransport(handler))
    assert [o.key for o in store.list('p/')] == ['p/workspaces/k/memory.db/0000/a.ltx',
                                                   'p/workspaces/k/memory.db/0001/b.ltx']
    assert store.delete_prefix('p/workspaces/k/memory.db/') == 2
    assert deleted == ['/tam/p/workspaces/k/memory.db/0000/a.ltx', '/tam/p/workspaces/k/memory.db/0001/b.ltx']
    assert requests[0].url.params['list-type'] == '2' and requests[0].url.params['prefix'] == 'p/'


def test_s3_store_errors_and_bucket_creation():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == 'PUT':
            return httpx.Response(409, content=b'<Error><Code>BucketAlreadyOwnedByYou</Code></Error>')
        return httpx.Response(403, content=b'<Error><Code>AccessDenied</Code></Error>')

    store = S3Store('tam', 'eu-west-1', None, False, Credentials(access_key='a', secret_key='b'),
                    httpx.MockTransport(handler))
    with pytest.raises(Unavailable, match='403 AccessDenied'):
        store.list('')
    store.create_bucket()
    assert seen[-1].url.host == 'tam.s3.eu-west-1.amazonaws.com'
    assert b'<LocationConstraint>eu-west-1</LocationConstraint>' in seen[-1].content

    def refused(request):
        raise httpx.ConnectError('refused', request=request)

    down = S3Store('tam', 'us-east-1', 'http://minio.test', True, Credentials(access_key='a', secret_key='b'),
                   httpx.MockTransport(refused))
    with pytest.raises(Unavailable, match='unreachable'):
        down.list('')


def test_s3_credentials_come_from_the_environment():
    with pytest.raises(Conflict, match='AWS_ACCESS_KEY_ID'):
        Credentials.from_env({})
    assert Credentials.from_env({**KEYS, 'AWS_SESSION_TOKEN': 't'}).session_token == 't'


# CLI

def test_cli_is_off_until_configured(tmp_path):
    root = server(tmp_path)
    result = cli(root, {}, 'status')
    assert result.returncode == 1 and 'Continuous backup is off' in result.stderr


def test_cli_config_status_and_drop_with_a_directory_replica(tmp_path):
    root = server(tmp_path)
    replica = tmp_path / 'replica'
    env = {'TAM_TEAM_REPLICA_URL': replica.as_uri(), 'TAM_TEAM_LITESTREAM_BIN': str(tmp_path / 'absent')}
    shown = cli(root, env, 'config')
    assert shown.returncode == 0 and f'url: "{replica.as_uri()}/workspaces"' in shown.stdout
    written = cli(root, env, 'config', '--out', str(tmp_path / 'litestream.yml'))
    assert written.returncode == 0 and (tmp_path / 'litestream.yml').is_file()
    assert cli(root, env, 'init-bucket').returncode == 0 and replica.is_dir()
    pending = cli(root, env, 'status')
    assert pending.returncode == 1 and 'PROBLEM: identity.db has no replica yet' in pending.stdout
    prepared = cli(root, env, 'prepare')
    assert prepared.returncode == 0 and 'switched now: identity.db' in prepared.stdout
    ltx(replica, 'identity.db', 1)
    ltx(replica, 'workspaces/personal_' + Registry.digest('bob') + '/memory.db', 1)
    healthy = cli(root, env, 'status', '--json')
    assert healthy.returncode == 0, healthy.stdout + healthy.stderr
    assert json.loads(healthy.stdout)['databases'][0]['state'] == 'replicated'
    mismatch = cli(root, env, 'drop', '--user', 'bob', '--confirm', 'alice')
    assert mismatch.returncode == 1 and '--confirm must repeat' in mismatch.stderr
    dropped = cli(root, env, 'drop', '--user', 'bob', '--confirm', 'bob')
    assert dropped.returncode == 0 and 'Deleted 1 replica objects' in dropped.stdout
    missing = cli(root, env, 'restore', '--to', str(tmp_path / 'restored'))
    assert missing.returncode == 1 and 'not installed' in missing.stderr


# Storage backend

def test_replication_applies_only_to_the_sqlite_backend(tmp_path):
    root = server(tmp_path)
    assert replication.storage_backend(root) == 'sqlite'
    replication.require_sqlite(root)
    with pytest.raises(Conflict, match='PostgreSQL tooling'):
        replication.require_sqlite(root, lambda _root: 'postgres')


def test_cli_refuses_replication_commands_on_postgres(tmp_path, monkeypatch, capsys):
    from team_memory import cli as team_cli

    root = server(tmp_path)
    monkeypatch.setattr(replication, 'storage_backend', lambda _root: 'postgres')
    monkeypatch.setenv('TAM_TEAM_REPLICA_URL', (tmp_path / 'replica').as_uri())
    monkeypatch.setattr(sys, 'argv', ['tam-team', '--root', str(root), 'replication', 'config'])
    with pytest.raises(SystemExit) as exit_info:
        team_cli.main()
    assert exit_info.value.code == 1 and 'PostgreSQL tooling' in capsys.readouterr().err

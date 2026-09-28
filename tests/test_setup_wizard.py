import hashlib
import io
import json
import os
import plistlib
import re
import signal
import sqlite3
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import httpx

from setup_wizard import clients, company, extras
from setup_wizard.cli import USAGE_ERROR, main, should_autorun
from setup_wizard.contracts import Answer
from setup_wizard.prompts import TerminalPrompter
from setup_wizard.steps import Context, Step
from setup_wizard.upgrade import adopt_existing
from setup_wizard.wizard import CANCELLED, Wizard
from team_memory.accounts import Accounts
from team_memory.registry import Registry
from team_memory.settings import SettingsStore, load_cipher
from team_memory.setup import SUPPORT_LINE, SUPPORT_URL

ROOT = Path(__file__).resolve().parents[1]
SECRET = 'sk-wizard-test-secret-123456'
PASSWORD = 'correct horse battery staple'


class Script:
    """Feeds answers to TerminalPrompter; hidden answers go through the no-echo reader."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts, self.hidden = [], []

    def _next(self):
        if not self.answers:
            raise EOFError
        answer = self.answers.pop(0)
        if answer is KeyboardInterrupt:
            raise KeyboardInterrupt
        return answer

    def read(self, prompt):
        self.prompts.append(prompt)
        return self._next()

    def read_hidden(self, prompt):
        self.hidden.append(prompt)
        return self._next()


class World:
    def __init__(self, tmp_path: Path, system='Linux', dirs=('.claude', '.codex')):
        self.home = tmp_path / 'home'
        self.home.mkdir()
        for name in dirs:
            (self.home / name).mkdir(parents=True)
        self.env = {'HOME': str(self.home), 'TAM_MEMORY_DIR': str(self.home / '.tam')}
        self.host = clients.Host(self.home, system, self.env, lambda _binary: None)
        self.record = self.home / '.tam' / 'setup.json'

    def run(self, argv=(), script=None, env=None):
        out = io.StringIO()
        environ = {**self.env, **(env or {})}
        host = clients.Host(self.home, self.host.system, environ, self.host.which)
        prompter = TerminalPrompter(out, read=script.read, read_hidden=script.read_hidden, environ={}) if script else None
        code = main(list(argv), environ=environ, prompter=prompter, host=host, out=out)
        return code, out.getvalue()

    def json(self, relative):
        return json.loads((self.home / relative).read_text())


def saved_settings(world):
    from local_settings import LocalSettings
    return LocalSettings(world.home / '.tam', {}).overrides()


def test_personal_interactive_registers_clients_hides_key_and_writes_record(tmp_path):
    world = World(tmp_path)
    script = Script('1', '', '1', '3', SECRET, '', '', 'n', 'y', 'n', 'y')
    code, out = world.run(['--skip-verify'], script)
    assert code == 0, out
    entry = world.json('.claude.json')['mcpServers']['memory']
    assert 'OPENAI_API_KEY' not in entry['env'] and 'MEMORY_LLM_PROVIDER' not in entry['env']
    assert SECRET not in (world.home / '.claude.json').read_text()
    assert entry['env']['TAM_MEMORY_DIR'] == str((world.home / '.tam').resolve())
    assert entry['env']['MEMORY_TEXT_EMBED_MODEL'] == 'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'
    codex = tomllib.loads((world.home / '.codex' / 'config.toml').read_text())['mcp_servers']['memory']
    assert 'OPENAI_API_KEY' not in codex['env'] and codex['command'] == entry['command']
    assert SECRET not in (world.home / '.tam' / 'settings.json').read_text()
    saved = saved_settings(world)
    assert saved['OPENAI_API_KEY'] == SECRET and saved['MEMORY_LLM_PROVIDER'] == 'openai'
    assert stat.S_IMODE((world.home / '.tam' / 'master.key').stat().st_mode) == 0o600
    record = json.loads(world.record.read_text())
    assert record['mode'] == 'personal' and record['personal']['clients'] == ['claude-code', 'codex']
    assert record['personal']['llm_key_set'] is True and record['personal']['hooks'] is True
    assert SECRET not in world.record.read_text() and SECRET not in out
    assert len(script.hidden) == 1 and 'hidden' in script.hidden[0]
    settings = world.json('.claude/settings.json')
    commands = [h['command'] for blocks in settings['hooks'].values() for b in blocks for h in b['hooks']]
    assert f"{world.home}/.claude/hooks/session-start.sh" in commands
    assert os.access(world.home / '.claude' / 'hooks' / 'session-start.sh', os.X_OK)
    assert not (world.home / '.claude' / 'skills').exists()
    assert SUPPORT_URL not in out


def test_existing_configs_keep_other_servers_and_unmanaged_env(tmp_path):
    world = World(tmp_path, dirs=('.cursor',))
    cursor = world.home / '.cursor' / 'mcp.json'
    cursor.write_text(json.dumps({'mcpServers': {'github': {'command': 'gh-mcp'}, 'memory': {
        'command': 'old', 'env': {'CUSTOM_FLAG': '1', 'OPENAI_API_KEY': 'stale-key-value-0000'}}}, 'theme': 'dark'}))
    code, out = world.run(['--non-interactive', '--mode', 'personal', '--clients', 'cursor', '--llm', 'none',
                           '--skip-verify'])
    assert code == 0, out
    data = json.loads(cursor.read_text())
    assert data['theme'] == 'dark' and data['mcpServers']['github'] == {'command': 'gh-mcp'}
    env = data['mcpServers']['memory']['env']
    assert env['CUSTOM_FLAG'] == '1' and 'OPENAI_API_KEY' not in env and 'MEMORY_LLM_ENABLED' not in env
    assert saved_settings(world)['MEMORY_LLM_ENABLED'] == 'false'


def test_unparseable_config_aborts_before_any_write(tmp_path):
    world = World(tmp_path, dirs=('.cursor', '.claude'))
    cursor = world.home / '.cursor' / 'mcp.json'
    cursor.write_text('{"mcpServers": {broken')
    code, out = world.run(['--non-interactive', '--mode', 'personal', '--clients', 'claude-code,cursor',
                           '--skip-verify'])
    assert code == 1 and 'not valid JSON' in out and 'Nothing was changed' in out
    assert cursor.read_text() == '{"mcpServers": {broken'
    assert not (world.home / '.claude.json').exists() and not world.record.exists()


def test_non_interactive_flags_and_usage_errors(tmp_path):
    world = World(tmp_path, dirs=())
    missing = world.run(['--non-interactive', '--mode', 'personal', '--clients', 'claude-code', '--llm', 'anthropic',
                         '--skip-verify'])
    assert missing[0] == USAGE_ERROR and '--llm-api-key-env' in missing[1]
    empty = world.run(['--non-interactive', '--mode', 'personal', '--llm', 'openai', '--llm-api-key-env', 'NOPE'])
    assert empty[0] == USAGE_ERROR and 'NOPE' in empty[1]
    unknown = world.run(['--non-interactive', '--mode', 'personal', '--clients', 'emacs', '--skip-verify'])
    assert unknown[0] == USAGE_ERROR and 'emacs' in unknown[1]
    interactive_flags = world.run(['--llm', 'none'])
    assert interactive_flags[0] == USAGE_ERROR
    assert not world.record.exists()
    code, out = world.run(['--non-interactive', '--json', '--mode', 'personal', '--clients', 'claude-desktop,opencode',
                           '--embed-preset', 'multilingual-large', '--llm', 'anthropic', '--llm-api-key-env', 'MY_KEY',
                           '--skip-verify'], env={'MY_KEY': SECRET})
    assert code == 0
    result = json.loads(out)
    assert result['ok'] is True and result['record']['personal']['embed_preset'] == 'multilingual-large'
    assert SECRET not in out
    desktop = world.json('.config/Claude/claude_desktop_config.json')['mcpServers']['memory']
    assert 'ANTHROPIC_API_KEY' not in desktop['env']
    assert desktop['env']['MEMORY_TEXT_EMBED_MODEL'] == 'intfloat/multilingual-e5-large'
    opencode = world.json('.config/opencode/opencode.json')['mcp']['memory']
    assert opencode['type'] == 'local' and opencode['enabled'] is True
    assert 'ANTHROPIC_API_KEY' not in opencode['environment']
    assert saved_settings(world)['ANTHROPIC_API_KEY'] == SECRET


def test_reconfigure_shows_current_values_and_keeps_the_key(tmp_path):
    world = World(tmp_path, dirs=('.claude',))
    first = world.run(['--non-interactive', '--mode', 'personal', '--clients', 'claude-code', '--llm', 'openai',
                       '--llm-api-key-env', 'K', '--llm-model', 'gpt-4.1-mini', '--no-hooks', '--skip-verify'],
                      env={'K': SECRET})
    assert first[0] == 0, first[1]
    again = world.run()
    assert again[0] == 0 and 'already set up' in again[1] and '--reconfigure' in again[1]
    script = Script('', '', '', '', '', '', '', 'n', '', '', 'y')
    code, out = world.run(['--reconfigure', '--skip-verify'], script)
    assert code == 0, out
    assert 'Choose [3]' in ''.join(script.prompts)
    assert any('[gpt-4.1-mini]' in prompt for prompt in script.prompts)
    assert 'Enter keeps the current key' in script.hidden[0]
    saved = saved_settings(world)
    assert saved['OPENAI_API_KEY'] == SECRET and saved['MEMORY_LLM_MODEL'] == 'gpt-4.1-mini'


def test_ctrl_c_or_eof_during_questions_changes_nothing(tmp_path):
    world = World(tmp_path)
    code, out = world.run(['--skip-verify'], Script('1', '', '1', KeyboardInterrupt))
    assert code == CANCELLED and 'Nothing was changed' in out
    code, out = world.run(['--skip-verify'], Script('1', ''))
    assert code == CANCELLED
    assert not world.record.exists() and not (world.home / '.claude.json').exists()
    assert not (world.home / '.codex' / 'config.toml').exists()


def test_ctrl_c_during_apply_waits_for_a_complete_write(tmp_path, monkeypatch):
    world = World(tmp_path)
    real_write = clients.write
    calls = []

    def interrupted_write(change):
        calls.append(change.path)
        if len(calls) == 1:
            os.kill(os.getpid(), signal.SIGINT)
        real_write(change)

    monkeypatch.setattr(clients, 'write', interrupted_write)
    code, out = world.run(['--non-interactive', '--mode', 'personal', '--clients', 'claude-code,codex', '--no-hooks',
                           '--no-skills'])
    assert code == CANCELLED and 'applied' in out and 'Checking' not in out
    assert len(calls) == 2
    assert (world.home / '.claude.json').exists() and (world.home / '.codex' / 'config.toml').exists()
    assert json.loads(world.record.read_text())['personal']['clients'] == ['claude-code', 'codex']


def test_autorun_only_for_a_person_at_a_terminal(tmp_path, monkeypatch):
    class Stream:
        def __init__(self, tty):
            self.tty = tty

        def isatty(self):
            return self.tty

    env = {'TAM_MEMORY_DIR': str(tmp_path / 'mem')}
    tty = Stream(True)
    assert should_autorun(env, tty, tty) is True
    assert should_autorun(env, Stream(False), tty) is False
    assert should_autorun(env, tty, Stream(False)) is False
    for extra in ({'CI': 'true'}, {'GITHUB_ACTIONS': 'true'}, {'MCP_TRANSPORT': 'stdio'}, {'TAM_NO_SETUP': '1'}):
        assert should_autorun({**env, **extra}, tty, tty) is False, extra
    assert should_autorun({**env, 'CI': 'false'}, tty, tty) is True
    (tmp_path / 'mem').mkdir()
    (tmp_path / 'mem' / 'setup.json').write_text('{}')
    assert should_autorun(env, tty, tty) is False


def _entry_point(tmp_path, argv, stdin_text):
    home = tmp_path / 'home'
    home.mkdir(exist_ok=True)
    env = {key: value for key, value in os.environ.items() if key not in ('CI', 'GITHUB_ACTIONS', 'MCP_TRANSPORT')}
    env.update(HOME=str(home), TAM_MEMORY_DIR=str(home / '.tam'), PATH='/usr/bin:/bin', MEMORY_ASYNC_ENRICHMENT='false')
    code = 'import sys; sys.argv = ["tam", *sys.argv[1:]]; from total_agent_memory.server import main_sync; main_sync()'
    return subprocess.run([sys.executable, '-c', code, *argv], input=stdin_text, capture_output=True, text=True,
                          env=env, cwd=str(ROOT), timeout=120, check=False), home


def test_tam_in_mcp_stdio_mode_never_starts_the_wizard(tmp_path):
    frames = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
        'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 't', 'version': '1'}}}]
    completed, home = _entry_point(tmp_path, [], ''.join(json.dumps(f) + '\n' for f in frames))
    replies = [json.loads(line) for line in completed.stdout.splitlines() if line.startswith('{')]
    assert any(reply.get('id') == 1 and 'result' in reply for reply in replies), completed.stderr[-2000:]
    assert 'Welcome' not in completed.stdout and not (home / '.tam' / 'setup.json').exists()


def test_tam_setup_subcommand_runs_the_wizard(tmp_path):
    completed, home = _entry_point(tmp_path, ['setup', '--non-interactive', '--mode', 'personal', '--clients', 'none',
                                              '--skip-verify'], '')
    assert completed.returncode == 0, completed.stderr
    assert json.loads((home / '.tam' / 'setup.json').read_text())['personal']['clients'] == []


def test_verification_starts_the_real_server(tmp_path):
    world = World(tmp_path, dirs=())
    code, out = world.run(['--non-interactive', '--mode', 'personal', '--clients', 'none'])
    assert code == 0, out
    assert 'OK: the server started and offered' in out


def _company_flags(world, *extra):
    return ['--non-interactive', '--mode', 'company', '--data-dir', str(world.home / 'srv'), '--port', '3999',
            '--public-url', 'https://memory.acme.test', '--company-name', 'Acme Corp', '--admin-id', 'alice',
            '--admin-name', 'Alice Admin', '--skip-verify', *extra]


def test_company_interactive_creates_admin_departments_providers_and_service(tmp_path):
    world = World(tmp_path, dirs=())
    script = Script('2', str(world.home / 'srv'), '0.0.0.0', '3999', 'https://memory.acme.test', '1',
                    'Acme Corp', 'alice', 'Alice Admin', 'Engineering', '', 'Sales & Marketing', 'sales', '',
                    '3', SECRET, '', '', '1', '', '', 'y')
    code, out = world.run(['--skip-verify'], script)
    assert code == 0, out
    root = world.home / 'srv'
    registry = Registry(root)
    assert registry.org_role('alice') == 'superadmin'
    assert registry.list_teams() == [('engineering', 'Engineering'), ('sales', 'Sales & Marketing')]
    assert registry.organization() == {'name': 'Acme Corp', 'public_url': 'https://memory.acme.test',
                                       'setup_state': 'complete'}
    with registry.connect() as db:
        stored = dict(db.execute('SELECT key,value FROM settings').fetchall())
    assert stored['MEMORY_LLM_PROVIDER'] == 'openai' and stored['OPENAI_API_KEY'] != SECRET
    assert SettingsStore(registry, load_cipher(root, {}), {}).overrides()['OPENAI_API_KEY'] == SECRET
    invite = re.search(r'^ {4}([A-Z0-9]{4}(?:-[A-Z0-9]{4}){4}) ', out, re.MULTILINE).group(1)
    Accounts(registry).redeem_invite('alice', invite, PASSWORD, 'ip')
    assert out.count(invite) == 1 and SECRET not in out
    unit = (root / 'deploy' / 'tam-team.service').read_text()
    assert f'--root {root}' in unit and '--host 0.0.0.0 --port 3999' in unit and 'Restart=on-failure' in unit
    assert 'https://memory.acme.test/dashboard/' in out and 'https://memory.acme.test/mcp/' in out
    assert out.count(SUPPORT_URL) == 1 and SUPPORT_LINE in out
    assert not list(root.parent.glob('.tam-setup-*'))
    record = json.loads(world.record.read_text())['company']
    assert record['departments'] == ['engineering', 'sales'] and record['deploy'] == 'service'


def test_company_rerun_keeps_existing_admin_and_settings(tmp_path):
    world = World(tmp_path, dirs=())
    assert world.run(_company_flags(world, '--deploy', 'manual', '--department', 'eng=Engineering'))[0] == 0
    code, out = world.run(_company_flags(world, '--reconfigure', '--deploy', 'manual', '--company-name', 'Acme Inc',
                                         '--department', 'eng=Engineering', '--department', 'ops=Operations'))
    assert code == 0, out
    registry = Registry(world.home / 'srv')
    assert [u['id'] for u in registry.list_users()] == ['alice']
    assert registry.organization()['name'] == 'Acme Inc'
    assert registry.list_teams() == [('eng', 'Engineering'), ('ops', 'Operations')]
    assert 'already exists' in out and 'shown only now' not in out


def test_company_failure_during_apply_leaves_no_data_dir(tmp_path, monkeypatch):
    world = World(tmp_path, dirs=())

    def broken(*_args, **_kwargs):
        raise OSError('disk full')

    monkeypatch.setattr(company.SettingsStore, 'update', broken)
    code, out = world.run(_company_flags(world, '--deploy', 'manual', '--llm', 'ollama'))
    assert code == 1 and 'disk full' in out and 'Already applied: nothing' in out
    assert not (world.home / 'srv').exists() and not list(world.home.glob('.tam-setup-*'))
    assert not world.record.exists()


def test_company_ctrl_c_in_questions_creates_nothing(tmp_path):
    world = World(tmp_path, dirs=())
    code, _out = world.run(['--skip-verify'], Script('2', str(world.home / 'srv'), '127.0.0.1', KeyboardInterrupt))
    assert code == CANCELLED and not (world.home / 'srv').exists() and not world.record.exists()


def test_company_compose_hands_over_to_the_web_wizard(tmp_path):
    world = World(tmp_path, dirs=())
    code, out = world.run(['--non-interactive', '--mode', 'company', '--data-dir', str(world.home / 'srv'),
                           '--port', '3999', '--deploy', 'compose', '--skip-verify'])
    assert code == 0, out
    env_file = world.home / 'srv' / 'deploy' / 'compose.env'
    assert 'TAM_TEAM_PORT=3999' in env_file.read_text()
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert 'setup code' in out and not (world.home / 'srv' / 'identity.db').exists()


def test_company_launchd_agent_on_macos(tmp_path):
    world = World(tmp_path, system='Darwin', dirs=())
    assert world.run(_company_flags(world, '--deploy', 'service'))[0] == 0
    agent = plistlib.loads((world.home / 'srv' / 'deploy' / f'{company.LAUNCHD_LABEL}.plist').read_bytes())
    assert agent['ProgramArguments'][-4:] == ['--host', '127.0.0.1', '--port', '3999']
    assert agent['EnvironmentVariables']['TAM_TEAM_DIR'] == str((world.home / 'srv').resolve())


def test_company_continuous_backup_to_s3_writes_litestream_service(tmp_path):
    world = World(tmp_path, dirs=())
    code, out = world.run(_company_flags(world, '--deploy', 'service', '--backup', 's3', '--backup-url',
                                         's3://acme-tam/prod', '--backup-endpoint', 'https://s3.acme.test',
                                         '--backup-retention', '72h'))
    assert code == 0, out
    root = (world.home / 'srv').resolve()
    config = (root / 'deploy' / 'litestream.yml').read_text()
    assert f'dir: "{root}"' in config and f'dir: "{root / "workspaces"}"' in config
    assert 'url: "s3://acme-tam/prod/workspaces"' in config and 'retention: "72h"' in config
    env = (root / 'deploy' / 'replication.env').read_text()
    assert 'TAM_TEAM_REPLICA_URL=s3://acme-tam/prod\n' in env and 'TAM_TEAM_REPLICA_ENDPOINT=https://s3.acme.test' in env
    for name in ('litestream.yml', 'replication.env'):
        assert stat.S_IMODE((root / 'deploy' / name).stat().st_mode) == 0o600
    unit = (root / 'deploy' / 'tam-team-litestream.service').read_text()
    assert f'EnvironmentFile={root}/deploy/replica-credentials.env' in unit
    assert f'ExecStart=litestream replicate -config {root}/deploy/litestream.yml' in unit
    assert 'AWS_SECRET_ACCESS_KEY=...' in out and 'replication status' in out
    assert 'Continuous backup' in out and 's3://acme-tam/prod' in out
    assert json.loads(world.record.read_text())['company']['backup_replica'] == 's3://acme-tam/prod'


def test_company_continuous_backup_with_compose_goes_to_the_env_file(tmp_path):
    world = World(tmp_path, dirs=())
    code, out = world.run(['--non-interactive', '--mode', 'company', '--data-dir', str(world.home / 'srv'),
                           '--port', '3999', '--deploy', 'compose', '--skip-verify', '--backup', 's3',
                           '--backup-url', 's3://acme-tam/prod'])
    assert code == 0, out
    env = (world.home / 'srv' / 'deploy' / 'compose.env').read_text()
    assert 'TAM_TEAM_REPLICA_URL=s3://acme-tam/prod' in env and 'AWS_SECRET_ACCESS_KEY=\n' in env
    assert '--profile litestream up -d' in out
    assert not (world.home / 'srv' / 'deploy' / 'litestream.yml').exists()
    (tmp_path / 'other').mkdir()
    other = World(tmp_path / 'other', dirs=())
    code, out = other.run(['--non-interactive', '--mode', 'company', '--data-dir', str(other.home / 'srv'),
                           '--deploy', 'compose', '--skip-verify', '--backup', 'file', '--backup-url', '/mnt/nas'])
    assert code == USAGE_ERROR and '--backup' in out and not (other.home / 'srv').exists()


def test_company_continuous_backup_to_a_directory_rejects_the_data_directory(tmp_path):
    world = World(tmp_path, system='Darwin', dirs=())
    code, out = world.run(_company_flags(world, '--deploy', 'manual', '--backup', 'file', '--backup-url',
                                         str(world.home / 'srv' / 'replica')))
    assert code == USAGE_ERROR and 'outside the server' in out and not (world.home / 'srv').exists()
    replica = world.home / 'nas' / 'tam'
    code, out = world.run(_company_flags(world, '--deploy', 'manual', '--backup', 'file', '--backup-url', str(replica)))
    assert code == 0, out
    config = (world.home / 'srv' / 'deploy' / 'litestream.yml').read_text()
    assert f'url: "{replica.resolve().as_uri()}"' in config and 'endpoint' not in config
    assert 'litestream replicate -config' in out
    assert not (world.home / 'srv' / 'deploy' / 'tam-team-litestream.service').exists()


def test_company_backup_off_by_default_keeps_setup_unchanged(tmp_path):
    world = World(tmp_path, dirs=())
    assert world.run(_company_flags(world, '--deploy', 'manual'))[0] == 0
    assert not (world.home / 'srv' / 'deploy' / 'litestream.yml').exists()
    assert json.loads(world.record.read_text())['company']['backup_replica'] is None


def test_company_backup_step_does_not_apply_to_postgres(tmp_path, monkeypatch):
    world = World(tmp_path, dirs=())
    monkeypatch.setattr(company, 'storage_backend', lambda _root: 'postgres')
    code, out = world.run(_company_flags(world, '--deploy', 'manual', '--backup', 's3', '--backup-url', 's3://a-b/c'))
    assert code == 0, out
    assert 'PostgreSQL tooling' in out and not (world.home / 'srv' / 'deploy' / 'litestream.yml').exists()
    assert json.loads(world.record.read_text())['company']['backup_replica'] is None


def test_health_probe_reports_a_running_server():
    ok = httpx.MockTransport(lambda request: httpx.Response(200, json={'status': 'ok', 'version': '14.5.1'}))
    down = httpx.MockTransport(lambda request: httpx.Response(503))
    assert company.health('http://tam.test', ok) == '14.5.1'
    assert company.health('http://tam.test', down) is None


def test_a_new_step_plugs_in_without_touching_the_others(tmp_path):
    from setup_wizard.steps import MODES, registry

    class Edition(Answer):
        edition: str

        def summary(self):
            return [('Edition', self.edition)]

    seen = []
    extra = Step('edition', 'Edition', MODES, lambda ctx: Edition(edition=ctx.prompter.text('edition', 'Edition', 'community')),
                 lambda ctx, plan, answer: seen.append(answer.edition))
    world = World(tmp_path, dirs=())
    out = io.StringIO()
    prompter = TerminalPrompter(out, read=Script('1', 'none', 'enterprise', '', '', 'y').read, environ={})
    ctx = Context(prompter, world.host, ROOT, world.home / '.tam', None)
    steps = registry()
    outcome = Wizard(ctx, world.record, verify=False, steps=[*steps[:2], extra, *steps[2:]]).run()
    assert outcome.code == 0 and seen == ['enterprise']
    assert 'Edition' in out.getvalue() and 'enterprise' in out.getvalue()


def _existing_install(world):
    memory = world.home / '.tam'
    memory.mkdir()
    with sqlite3.connect(memory / 'memory.db') as db:
        db.execute('CREATE TABLE legacy_notes (id INTEGER PRIMARY KEY, content TEXT)')
        db.execute("INSERT INTO legacy_notes(content) VALUES ('keep me')")
    db.close()
    (world.home / '.claude.json').write_text(json.dumps({'mcpServers': {'memory': {
        'command': '/opt/tam/.venv/bin/python', 'args': ['/opt/tam/src/server.py'],
        'env': {'TAM_MEMORY_DIR': str(memory), 'MEMORY_LLM_PROVIDER': 'openai', 'OPENAI_API_KEY': SECRET,
                'V9_EMBED_BACKEND': 'e5-large', 'MEMORY_LLM_MODEL': 'gpt-4.1-mini'}}}}))
    return {path: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (memory / 'memory.db', world.home / '.claude.json')}


def _unchanged(snapshot):
    return all(hashlib.sha256(path.read_bytes()).hexdigest() == digest for path, digest in snapshot.items())


def test_existing_install_upgrades_silently_as_personal(tmp_path):
    world = World(tmp_path, dirs=('.claude', '.cursor'))
    (world.home / '.cursor' / 'mcp.json').write_text('{not json')
    snapshot = _existing_install(world)
    record = adopt_existing(world.env, world.host)
    assert record.mode == 'personal' and record.source == 'upgrade'
    assert record.personal.clients == ['claude-code'] and record.personal.embed_preset == 'multilingual-large'
    assert record.personal.llm_provider == 'openai' and record.personal.llm_key_set is True
    assert record.personal.llm_settings == {'MEMORY_LLM_MODEL': 'gpt-4.1-mini'}
    assert SECRET not in world.record.read_text()
    assert _unchanged(snapshot)
    assert adopt_existing(world.env, world.host) is None
    code, out = world.run()
    assert code == 0 and 'already set up' in out and 'tam setup --mode company' in out


def test_fresh_machine_is_not_adopted(tmp_path):
    world = World(tmp_path, dirs=('.claude',))
    assert adopt_existing(world.env, world.host) is None
    assert not world.record.exists()


def test_upgrade_first_start_in_mcp_mode_records_personal_without_prompt(tmp_path):
    home = tmp_path / 'home'
    world = World(tmp_path, dirs=())
    _existing_install(world)
    frames = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
        'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 't', 'version': '1'}}}]
    completed, _home = _entry_point(tmp_path, [], ''.join(json.dumps(f) + '\n' for f in frames))
    assert home == _home
    replies = [json.loads(line) for line in completed.stdout.splitlines() if line.startswith('{')]
    assert any(reply.get('id') == 1 and 'result' in reply for reply in replies), completed.stderr[-2000:]
    assert 'Welcome' not in completed.stdout
    record = json.loads((home / '.tam' / 'setup.json').read_text())
    assert record['mode'] == 'personal' and record['source'] == 'upgrade'
    with sqlite3.connect(home / '.tam' / 'memory.db') as db:
        assert db.execute('SELECT content FROM legacy_notes').fetchall() == [('keep me',)]
    db.close()


def test_company_server_is_added_next_to_personal_memory(tmp_path):
    world = World(tmp_path, dirs=('.claude',))
    snapshot = _existing_install(world)
    adopt_existing(world.env, world.host)
    overlap = world.run(['--non-interactive', '--mode', 'company', '--data-dir', str(world.home / '.tam' / 'team'),
                         '--deploy', 'manual', '--company-name', 'Acme', '--admin-id', 'alice', '--admin-name', 'A',
                         '--skip-verify'])
    assert overlap[0] == USAGE_ERROR and 'personal memory' in overlap[1]
    code, out = world.run(_company_flags(world, '--deploy', 'manual'))
    assert code == 0, out
    record = json.loads(world.record.read_text())
    assert record['mode'] == 'company' and record['personal']['clients'] == ['claude-code']
    assert record['company']['data_dir'] == str((world.home / 'srv').resolve())
    assert _unchanged(snapshot)
    assert 'not automatic' in out and SUPPORT_LINE in out
    script = Script('', KeyboardInterrupt)
    world.run(['--reconfigure'], script)
    assert 'Company server' in world.run()[1]


def test_personal_install_offers_add_company_server(tmp_path):
    world = World(tmp_path, dirs=('.claude',))
    assert world.run(['--non-interactive', '--mode', 'personal', '--clients', 'claude-code', '--skip-verify'])[0] == 0
    code, out = world.run(['--reconfigure'], Script(KeyboardInterrupt))
    assert code == CANCELLED and 'Add a company server' in out and 'stays as it is' in out
    preset = Script(str(world.home / 'srv'), KeyboardInterrupt)
    code, out = world.run(['--mode', 'company'], preset)
    assert code == CANCELLED and 'Company server (from --mode)' in out


def test_hook_commands_survive_a_home_with_spaces(tmp_path):
    home = tmp_path / "Jane Doe"
    host = clients.Host(home, "Darwin", {}, lambda _name: None)
    command = extras._command(host, home / ".claude" / "hooks" / "session-start.sh")
    assert command == f"'{home}/.claude/hooks/session-start.sh'"
    plain = clients.Host(tmp_path / "jane", "Linux", {}, lambda _name: None)
    assert extras._command(plain, tmp_path / "jane" / "x.sh") == str(tmp_path / "jane" / "x.sh")


def test_upgrade_points_at_plain_text_keys_and_reconfigure_moves_them(tmp_path, caplog):
    world = World(tmp_path, dirs=('.claude',))
    _existing_install(world)
    with caplog.at_level('WARNING'):
        adopt_existing(world.env, world.host)
    assert any('plain text' in r.getMessage() and 'claude-code' in r.getMessage() for r in caplog.records)
    assert SECRET not in caplog.text
    code, out = world.run(['--reconfigure', '--skip-verify'], Script('', '', '', '', '', '', '', 'n', '', '', 'y'))
    assert code == 0, out
    assert SECRET not in (world.home / '.claude.json').read_text()
    assert saved_settings(world)['OPENAI_API_KEY'] == SECRET

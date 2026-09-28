import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which('powershell') or shutil.which('pwsh')
pytestmark = pytest.mark.skipif(POWERSHELL is None, reason='Requires PowerShell runtime')


def run_installer(home, *arguments):
    # HOME and XDG_CONFIG_HOME as well: with pwsh on macOS/Linux, anything that resolves the home or
    # the config dir without USERPROFILE must still land in the test home, never in real client configs.
    env = {**os.environ, 'USERPROFILE': str(home), 'HOME': str(home), 'XDG_CONFIG_HOME': str(home / '.config'),
           'INSTALL_TEST_MODE': '1',
           'TAM_MEMORY_DIR': str(home / 'current memory'),
           'CLAUDE_MEMORY_DIR': str(home / 'legacy memory')}
    return subprocess.run([POWERSHELL, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                           str(ROOT / 'install.ps1'), '-TestMode', *arguments],
                          env=env, capture_output=True, text=True, encoding='utf-8',
                          errors='replace', timeout=60, check=False)


@pytest.mark.parametrize('ide,path,parent,env_key', [
    ('claude-code', '.claude.json', 'mcpServers', 'env'),
    ('cursor', '.cursor/mcp.json', 'mcpServers', 'env'),
    ('gemini-cli', '.gemini/settings.json', 'mcpServers', 'env'),
    ('opencode', '.config/opencode/opencode.json', 'mcp', 'environment'),
])
def test_windows_repeat_install_preserves_settings_and_memory_path(tmp_path, ide, path, parent, env_key):
    from version import VERSION

    config = tmp_path / path
    config.parent.mkdir(parents=True, exist_ok=True)
    original = {'theme': 'dark', 'enabled': True, 'number': 7,
                'nested': {'text': 'Вася', 'items': ['one', 'two'], 'single': ['one'],
                           'empty': [], 'nulls': [None], 'arrays': [['nested'], []]},
                parent: {'unrelated': {'command': 'keep-me', 'args': ['a', 'b']}}}
    config.write_text(json.dumps(original, ensure_ascii=False), encoding='utf-8')
    for _ in range(2):
        result = run_installer(tmp_path, '-Ide', ide)
        assert result.returncode == 0, result.stdout + result.stderr
        assert f'v{VERSION}' in result.stdout
        updated = json.loads(config.read_text(encoding='utf-8'))
        for key in ('theme', 'enabled', 'number', 'nested'):
            assert updated[key] == original[key]
        assert updated[parent]['unrelated'] == original[parent]['unrelated']
        assert Path(updated[parent]['memory'][env_key]['TAM_MEMORY_DIR']) == (tmp_path / 'current memory').resolve()
    assert not (tmp_path / 'legacy memory').exists()


def test_windows_invalid_config_is_preserved(tmp_path):
    config = tmp_path / '.cursor/mcp.json'
    config.parent.mkdir(parents=True)
    config.write_text('{broken JSON', encoding='utf-8')
    result = run_installer(tmp_path, '-Ide', 'cursor')
    assert result.returncode != 0
    assert config.read_text() == '{broken JSON'


def test_windows_codex_upgrade_does_not_duplicate_env_table(tmp_path):
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib

    config = tmp_path / '.codex/config.toml'
    config.parent.mkdir(parents=True)
    config.write_text('model = "preserve-model"\n[mcp_servers.memory]\ncommand = "old-python"\n'
                      '[mcp_servers.memory.env]\nCLAUDE_MEMORY_DIR = "old-memory"\n'
                      '[mcp_servers.other]\ncommand = "preserve-server"\n', encoding='utf-8')
    for _ in range(2):
        result = run_installer(tmp_path, '-Ide', 'codex')
        assert result.returncode == 0, result.stdout + result.stderr
        updated = tomllib.loads(config.read_text(encoding='utf-8'))
        assert updated['model'] == 'preserve-model'
        assert updated['mcp_servers']['other']['command'] == 'preserve-server'
        assert Path(updated['mcp_servers']['memory']['env']['TAM_MEMORY_DIR']) == (tmp_path / 'current memory').resolve()


def test_windows_dashboard_launcher_preserves_unicode_paths(tmp_path):
    home = tmp_path / 'Вася & Петя'
    result = run_installer(home, '-Ide', 'claude-code')
    assert result.returncode == 0, result.stdout + result.stderr
    launcher = home / 'current memory/start-dashboard.py'
    script = '''
import json, os, runpy, sys
def inspect(path, run_name):
    sys.stdout.write(json.dumps({"path": path, "name": run_name,
        "memory": os.environ["TAM_MEMORY_DIR"], "port": os.environ["DASHBOARD_PORT"]}))
runpy.run_path = inspect
with open(sys.argv[1], encoding="utf-8") as source:
    exec(compile(source.read(), sys.argv[1], "exec"))
'''
    result = subprocess.run([sys.executable, '-c', script, str(launcher)],
                            capture_output=True, text=True, timeout=30, check=True)
    captured = json.loads(result.stdout)
    assert captured['path'] == str(ROOT / 'src/dashboard.py')
    assert captured['memory'] == str(home / 'current memory')
    assert captured['port'] == os.environ.get('DASHBOARD_PORT', '37737')
    assert captured['name'] == '__main__'


def test_windows_background_launcher_keeps_memory_and_arguments(tmp_path):
    home = tmp_path / 'Вася & Петя'
    env = {**os.environ, 'USERPROFILE': str(home), 'HOME': str(home), 'XDG_CONFIG_HOME': str(home / '.config'),
           'TAM_MEMORY_DIR': str(home / 'memory'),
           'INSTALL_TEST_MODE': '1', 'TAM_CHECK_INSTALLER': str(ROOT / 'install.ps1')}
    command = (
        '. $env:TAM_CHECK_INSTALLER -TestMode -Ide cursor; '
        '$null = Write-PythonLauncher -FileName "reflection-check.py" '
        '-ScriptPath ([IO.Path]::Combine($InstallDir,"src","tools","run_reflection.py")) '
        '-ScriptArgs @("--scope=auto", "Вася & Петя")'
    )
    result = subprocess.run([POWERSHELL, '-NoProfile', '-ExecutionPolicy', 'Bypass',
                             '-Command', command], env=env, capture_output=True,
                            text=True, encoding='utf-8', errors='replace', timeout=60, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    launcher = home / 'memory/reflection-check.py'
    probe = '''
import json, os, runpy, sys
def inspect(path, run_name):
    sys.stdout.write(json.dumps({"path": path, "args": sys.argv[1:],
        "memory": os.environ["TAM_MEMORY_DIR"], "legacy": os.environ["CLAUDE_MEMORY_DIR"]}))
runpy.run_path = inspect
with open(sys.argv[1], encoding="utf-8") as source:
    exec(compile(source.read(), sys.argv[1], "exec"))
'''
    result = subprocess.run([sys.executable, '-c', probe, str(launcher)],
                            capture_output=True, text=True, timeout=30, check=True)
    captured = json.loads(result.stdout)
    assert captured['path'] == str(ROOT / 'src/tools/run_reflection.py')
    assert captured['args'] == ['--scope=auto', 'Вася & Петя']
    assert captured['memory'] == captured['legacy'] == str(home / 'memory')

"""Entry point for pip-installed package: `tam setup` runs the wizard, `tam report` prints an activity report,
`tam redact-existing` finds and redacts stored credentials, anything else starts src/server.py."""

import importlib.util
import os
import sys

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_src = os.path.join(_root, "src")
_server_path = os.path.join(_src, "server.py")
_server = None


def _load_server():
    global _server
    if _server is None:
        if not os.path.exists(_server_path):
            sys.stderr.write("Error: server.py not found\n")
            sys.exit(1)
        spec = importlib.util.spec_from_file_location("_server", _server_path)
        _server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_server)
    return _server


def __getattr__(name):
    if name in ("main", "run"):
        return getattr(_load_server(), name)
    raise AttributeError(name)


def _setup_wizard():
    if _src not in sys.path:
        sys.path.insert(0, _src)
    from setup_wizard import cli
    return cli


def _report_cli():
    if _src not in sys.path:
        sys.path.insert(0, _src)
    from memory_reports import cli
    return cli


def _adopt_existing_install():
    if _src not in sys.path:
        sys.path.insert(0, _src)
    from setup_wizard.upgrade import adopt_existing
    adopt_existing(os.environ)


def main_sync():
    """Synchronous entry point for console_scripts."""
    argv = sys.argv[1:]
    if argv[:1] == ["report"]:
        sys.exit(_report_cli().main(argv[1:]))
    if argv[:1] == ["redact-existing"]:
        if _src not in sys.path:
            sys.path.insert(0, _src)
        import stored_secrets
        sys.exit(stored_secrets.main(argv[1:]))
    _adopt_existing_install()
    if argv[:2] == ["setup", "register"]:
        from setup_wizard import register
        sys.exit(register.main(argv[2:]))
    if argv[:1] == ["setup"]:
        sys.exit(_setup_wizard().main(argv[1:]))
    if not argv and sys.stdin.isatty() and sys.stdout.isatty():
        cli = _setup_wizard()
        if cli.should_autorun(os.environ):
            sys.exit(cli.main([], first_run=True))
    _load_server().run()


if __name__ == "__main__":
    main_sync()

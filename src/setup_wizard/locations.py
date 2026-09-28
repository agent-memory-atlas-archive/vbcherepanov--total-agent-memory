from collections.abc import Mapping
from pathlib import Path

RECORD_NAME = "setup.json"
RECORD_ENV = "TAM_SETUP_FILE"


def locate_memory_dir(environ: Mapping[str, str]) -> Path:
    """Same order as paths.memory_dir(), without creating or migrating anything."""
    for name in ("TAM_MEMORY_DIR", "CLAUDE_MEMORY_DIR"):
        if environ.get(name):
            return Path(environ[name]).expanduser()
    new, old = Path.home() / ".tam", Path.home() / ".claude-memory"
    return new if new.exists() or not old.exists() else old


def record_path(environ: Mapping[str, str], memory_dir: Path | None = None) -> Path:
    if environ.get(RECORD_ENV):
        return Path(environ[RECORD_ENV]).expanduser()
    return (memory_dir or locate_memory_dir(environ)) / RECORD_NAME


def install_root() -> Path:
    """The checkout (or site-packages) directory that holds src/, hooks/ and skills/."""
    return Path(__file__).resolve().parents[2]

import os
import signal
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

PRIVATE_MODE = 0o600


def atomic_write(path: Path, text: str, mode: int | None = None) -> None:
    """Replace ``path`` in one rename; keeps the current permissions unless ``mode`` is given."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode is None:
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


@contextmanager
def deferred_interrupts() -> Iterator[None]:
    """Hold Ctrl-C until the block ends so an apply phase never stops half-way."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    received: list[int] = []
    previous = signal.signal(signal.SIGINT, lambda signum, _frame: received.append(signum))
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)
    if received:
        raise KeyboardInterrupt

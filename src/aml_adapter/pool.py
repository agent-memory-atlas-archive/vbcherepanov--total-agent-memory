"""Pool of per-user worker processes (spawned, least-recently-used eviction).

A user's requests are serialised on that user's worker; different users run
in parallel up to `maximum` workers. When every slot is busy with other users
a caller waits up to `wait_seconds` and then gets `Busy` (HTTP 429).
"""

from __future__ import annotations

import json
import logging
import multiprocessing
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from aml_adapter.errors import ERRORS_BY_CODE, AdapterError, Busy, Unavailable

LOGGER = logging.getLogger("aml_adapter.pool")
STOP_TIMEOUT_SECONDS = 5.0


@dataclass
class _Worker:
    process: object | None = None
    connection: object | None = None
    busy: bool = True
    last_used: float = field(default_factory=time.monotonic)


class WorkerPool:
    def __init__(self, users_dir: Path, *, maximum: int, timeout: float, wait_seconds: float,
                 settings_for, tam_env: dict[str, str]):
        if maximum < 1 or timeout <= 0 or wait_seconds <= 0:
            raise ValueError("worker count, timeout and wait must be positive")
        self.users_dir = users_dir
        self.maximum, self.timeout, self.wait_seconds = maximum, timeout, wait_seconds
        self.settings_for = settings_for
        self.tam_env = dict(tam_env)
        self.workers: OrderedDict[str, _Worker] = OrderedDict()
        self.locked: set[str] = set()
        self.condition = threading.Condition()
        self.context = multiprocessing.get_context("spawn")
        self.closed = False

    def invoke(self, ns: str, work: dict) -> object:
        worker = self._acquire(ns)
        try:
            if worker.process is None:
                try:
                    self._start(ns, worker)
                except OSError as exc:
                    self._retire(ns, worker)
                    worker = None
                    raise Unavailable("could not start a worker process") from exc
            try:
                worker.connection.send(json.dumps(work))
                if not worker.connection.poll(self.timeout):
                    raise Unavailable("worker timed out; the request was rolled back, retry it")
                reply = json.loads(worker.connection.recv())
            except (EOFError, OSError, Unavailable) as exc:
                self._retire(ns, worker)
                worker = None
                if isinstance(exc, Unavailable):
                    raise
                raise Unavailable("worker exited; the request was rolled back, retry it") from exc
            if not reply.get("ok"):
                if reply.get("code") == "fatal":
                    self._retire(ns, worker)
                    worker = None
                    raise Unavailable(reply.get("error") or "worker failed to start")
                raise ERRORS_BY_CODE.get(reply.get("code"), AdapterError)(reply.get("error", "worker error"))
            return reply.get("data")
        finally:
            if worker is not None:
                self._release(ns, worker)

    def _acquire(self, ns: str) -> _Worker:
        deadline = time.monotonic() + self.wait_seconds
        victim: _Worker | None = None
        with self.condition:
            while True:
                if self.closed:
                    raise Unavailable("server is shutting down")
                worker = self.workers.get(ns)
                if ns not in self.locked:
                    if worker is not None and not worker.busy:
                        worker.busy = True
                        self.workers.move_to_end(ns)
                        return worker
                    if worker is None:
                        if len(self.workers) >= self.maximum:
                            victim = self._evict_idle()
                        if len(self.workers) < self.maximum:
                            worker = _Worker()
                            self.workers[ns] = worker
                            break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Busy("all workers are busy; retry later")
                self.condition.wait(remaining)
        if victim is not None:
            self._stop(victim)
        return worker

    def _evict_idle(self) -> _Worker | None:
        for key, worker in self.workers.items():
            if not worker.busy:
                del self.workers[key]
                return worker
        return None

    def _start(self, ns: str, worker: _Worker) -> None:
        user_dir = self.users_dir / ns
        user_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        from aml_adapter.runtime import serve

        parent, child = self.context.Pipe()
        process = self.context.Process(
            target=serve, args=(child, str(user_dir), self.settings_for(ns).to_json(), self.tam_env), daemon=True)
        process.start()
        child.close()
        worker.process, worker.connection = process, parent
        LOGGER.info(json.dumps({"event": "aml_worker_started", "user_ns": ns, "pid": process.pid}))

    def _release(self, ns: str, worker: _Worker) -> None:
        with self.condition:
            worker.busy = False
            worker.last_used = time.monotonic()
            self.condition.notify_all()

    def _retire(self, ns: str, worker: _Worker) -> None:
        with self.condition:
            if self.workers.get(ns) is worker:
                del self.workers[ns]
            self.condition.notify_all()
        self._stop(worker)

    @contextmanager
    def exclusive(self, ns: str):
        """Stop the user's worker and keep new requests for `ns` out while the block runs."""
        deadline = time.monotonic() + self.wait_seconds
        with self.condition:
            while ns in self.locked or (ns in self.workers and self.workers[ns].busy):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Busy(f"user {ns} is busy")
                self.condition.wait(remaining)
            self.locked.add(ns)
            worker = self.workers.pop(ns, None)
        try:
            if worker is not None:
                self._stop(worker)
            yield
        finally:
            with self.condition:
                self.locked.discard(ns)
                self.condition.notify_all()

    @staticmethod
    def _stop(worker: _Worker) -> None:
        process, connection = worker.process, worker.connection
        if connection is not None:
            try:
                connection.send(None)
            except (OSError, ValueError):
                LOGGER.info(json.dumps({"event": "aml_worker_pipe_already_closed"}))
        if process is not None:
            process.join(STOP_TIMEOUT_SECONDS)
            if process.is_alive():
                process.terminate()
                process.join(STOP_TIMEOUT_SECONDS)
            if process.is_alive():
                process.kill()
                process.join()
            process.close()
        if connection is not None:
            connection.close()

    def active(self) -> int:
        with self.condition:
            return len(self.workers)

    def close(self) -> None:
        with self.condition:
            self.closed = True
            workers = list(self.workers.values())
            self.workers.clear()
            self.condition.notify_all()
        for worker in workers:
            self._stop(worker)

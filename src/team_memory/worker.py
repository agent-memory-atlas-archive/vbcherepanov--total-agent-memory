import hashlib
import json
import logging
import multiprocessing
import os
import sqlite3
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, ExitStack, nullcontext
from pathlib import Path
from typing import TYPE_CHECKING

import recall_output
from tam_db.contracts import Backend, ControlPlane, StoreDatabase, WorkspaceProvisioner
from team_memory.audit import (
    SNAPSHOT_COLUMNS,
    AuditedConnection,
    authorship,
    install,
    pg_audited_connection,
)
from team_memory.contracts import (
    Actor,
    Conflict,
    DomainError,
    Reply,
    Unavailable,
    Work,
    Workspace,
)
from team_memory.database_contracts import DATABASE_URL_ENV, MaintenanceGate
from team_memory.lifecycle import ServerLease

DEFAULT_WORKERS = 3
DEFAULT_OPERATION_TIMEOUT = 120
STOP_TIMEOUT_SECONDS = 5
MAINTENANCE_MESSAGE = "Maintenance in progress; retry shortly"
LOGIN_FAILED_CODE = "login_failed"
# sysexits EX_TEMPFAIL: the worker stopped because its workspace lease was lost.
LEASE_LOST_EXIT_CODE = 75
# Domain error messages are fixed texts; the cap only keeps log lines bounded.
PROVISIONING_REASON_CHARS = 300
LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from team_memory.registry import Registry


class Runtime:
    def __init__(self, root: str, database: StoreDatabase | None = None):
        os.environ.update(TAM_MEMORY_DIR=root, CLAUDE_MEMORY_DIR=root,
                          USE_BINARY_SEARCH="true", MEMORY_ASYNC_ENRICHMENT="false")
        import server
        database = database or StoreDatabase.sqlite()
        factory = pg_audited_connection() if database.backend is Backend.POSTGRES else AuditedConnection
        # background_queues=False: the team server has no consumer for the triple,
        # deep-enrichment and representations queues (decision D4).
        self.store = server.Store(connection_factory=factory, database=database, background_queues=False)
        self.recall = server.Recall(self.store)
        install(self.store.db)
        self.session = "workspace"
        self.store.session_start(self.session)

    def record(self, record_id: int) -> dict:
        row = self.store.q1("SELECT * FROM knowledge WHERE id=?", (record_id,))
        if row is None:
            raise Conflict("Record unavailable")
        return {**row, **authorship(self.store.db, record_id)}

    def execute(self, work: Work):
        args = work.arguments
        operation = work.operation
        if operation == "memory_recall":
            deferred = bool(args.get("defer_cross_rerank"))
            # A deferred search returns its fused window for the gateway's single cross-workspace
            # re-rank; which records the caller finally sees is decided there, so usage is not recorded.
            result = self.recall.search(args["query"], project=args.get("project"), limit=args["limit"],
                                        defer_cross_rerank=deferred, record_usage=not deferred)
            records = [item for group in result.get("results", {}).values() for item in group]
            recall_output.shape_records(records)
            if deferred and all("fused_rank" in item for item in records):
                records.sort(key=lambda item: item["fused_rank"])
                return [{**item, **authorship(self.store.db, item["id"])} for item in records]
            records.sort(key=lambda item: float(item.get("score", 0)), reverse=True)
            return [{**item, **authorship(self.store.db, item["id"])} for item in records[:args["limit"]]]
        if operation == "memory_get":
            return self.record(args["id"])
        if operation == "memory_history":
            self.record(args["id"])
            rows = self.store.db.execute("""
                WITH RECURSIVE predecessors(id) AS (
                    SELECT CAST(? AS BIGINT) UNION SELECT k.id FROM knowledge k JOIN predecessors p ON k.superseded_by=p.id
                ) SELECT h.* FROM tam_history h JOIN predecessors p ON p.id=h.record_id
                  WHERE h.sequence>? ORDER BY h.sequence LIMIT ?
                """, (args["id"], args["after"], args["limit"])).fetchall()
            return [{**dict(row), **{k: json.loads(row[k]) if row[k] else None
                                    for k in ("actor", "before_state", "after_state")}} for row in rows]
        if operation == "memory_export":
            ids = self.store.db.execute("SELECT id FROM knowledge WHERE id>? ORDER BY id LIMIT ?",
                                        (args["after"], args["limit"])).fetchall()
            return [self.record(row[0]) for row in ids]
        with self.store.db.transaction(work.actor, args.get("reason", "")):
            request_id = args.get("request_id")
            fingerprint = hashlib.sha256(json.dumps({"operation": operation, "arguments": args},
                                                     sort_keys=True).encode()).hexdigest()
            if request_id:
                previous = self.store.db.execute("SELECT fingerprint,result FROM tam_requests WHERE user_id=? AND request_id=?",
                                                 (work.actor.user_id, request_id)).fetchone()
                if previous:
                    if previous[0] != fingerprint:
                        raise Conflict("request_id was already used with different arguments")
                    return json.loads(previous[1])
            if operation == "memory_save":
                result = self.save(args)
            elif operation in ("memory_update", "memory_delete"):
                old = self.record(args["id"])
                if old["revision"] != args["expected_revision"] or old["status"] != "active":
                    raise Conflict("Revision changed; read the record before retrying")
                if operation == "memory_delete":
                    self.store.delete_knowledge(old["id"])
                    result = self.record(old["id"])
                else:
                    self.store.db.origin = Actor.model_validate(old["created_by"])
                    result = self.save({**args, "type": old["type"], "project": old["project"],
                                        "tags": json.loads(old["tags"]), "context": old["context"],
                                        "branch": old.get("branch", ""), "source_format": old.get("source_format", "auto"),
                                        "importance": old.get("importance", "medium")}, replacing=True)
                    if result.get("saved") is False:
                        raise Conflict("Replacement rejected by quality gate")
                    self.store.db.execute("UPDATE knowledge SET status='superseded',superseded_by=? WHERE id=?",
                                          (result["id"], old["id"]))
                    self.store._delete_embedding(old["id"])
                    result["previous_id"] = old["id"]
            else:
                raise DomainError("Unknown workspace operation")
            if request_id:
                self.store.db.execute("INSERT INTO tam_requests(user_id,request_id,fingerprint,result) VALUES (?,?,?,?)",
                                      (work.actor.user_id, request_id, fingerprint, json.dumps(result, ensure_ascii=False)))
        self.invalidate()
        return result

    def save(self, args, replacing=False):
        record_id, dedup, redacted, sections, quality = self.store.save_knowledge(
            self.session, args["content"], args["type"], project=args["project"],
            tags=args["tags"], context=args["context"], importance=args["importance"],
            branch=args["branch"], source_format=args["source_format"],
            skip_dedup=replacing, repeat="confirm")
        if record_id is None:
            return {"saved": False, "quality": quality}
        record = self.record(record_id)
        if dedup:
            state = json.dumps({key: record.get(key) for key in SNAPSHOT_COLUMNS}, ensure_ascii=False)
            self.store.db.execute("""
                INSERT INTO tam_history(record_id,operation,actor,reason,revision,before_state,after_state)
                VALUES (?,'confirm',tam_actor(),tam_reason(),?,?,?)
                """, (record_id, record["revision"], state, state))
        return {**record, "saved": True, "deduplicated": dedup,
                "privacy_redacted": redacted, "privacy_redacted_sections": sections}

    def invalidate(self):
        if self.store.cache is not None:
            self.store.cache.invalidate()
        if getattr(self.store, "v9_cache", None) is not None:
            self.store.v9_cache.invalidate_all()


def serve(connection, root: str, environment: Mapping[str, str] | None = None,
          database: StoreDatabase | None = None):
    os.environ.update(environment or {})
    # A worker reaches its workspace only through ``database`` (the workspace role on
    # PostgreSQL); the control plane's DSN is not its business.
    os.environ.pop(DATABASE_URL_ENV, None)
    with ServerLease(Path(root)), ExitStack() as stack:
        try:
            stack.enter_context(workspace_lease(Path(root).name, database))
        except (sqlite3.Error, DomainError) as exc:
            if not login_failed(exc):
                raise
            refuse_login(connection, exc)
            return
        serve_locked(connection, root, database)


def workspace_lease(key: str, database: StoreDatabase | None) -> AbstractContextManager:
    """PostgreSQL: an advisory lock so one process on any host serves a workspace (the file lease is per host)."""
    if database is None or database.backend is Backend.SQLITE:
        return nullcontext()
    from team_memory.pg_provision import workspace_lease as pg_workspace_lease

    return pg_workspace_lease(database.url, key, database.settings, on_lost=exit_on_lost_lease,
                              connect_overrides=database.connect_kwargs())


def refuse_login(connection, exc: BaseException) -> None:
    """Answer the request that caused the spawn so the pool can repair the role and retry at once."""
    LOGGER.error(json.dumps({"event": "workspace_login_failed", "error": type(exc).__name__}))
    if connection.recv() is not None:
        connection.send(Reply(error="Workspace role login failed", code=LOGIN_FAILED_CODE).model_dump_json())


class WorkspaceLoginFailed(Unavailable):
    """A new worker could not log in as its workspace role; raised inside the pool only."""


def login_failed(exc: BaseException) -> bool:
    """Whether a connect failure is an authentication failure (db_check category auth_failed).

    tam_db raises its contract error ``from None`` with a redacted message; the driver error it
    replaced is still its ``__context__`` and carries what categorize() needs."""
    from team_memory.database_contracts import ErrorCategory
    from team_memory.db_check import categorize

    seen: BaseException | None = exc
    while seen is not None:
        if categorize(seen) is ErrorCategory.AUTH_FAILED:
            return True
        seen = seen.__cause__ or seen.__context__
    return False


def exit_on_lost_lease() -> None:
    """Called by the lease heartbeat thread: another process may now serve this workspace, so this one
    must not write again, including an operation in flight. Exiting drops the connection, PostgreSQL
    rolls back any open transaction, and the pool answers Unavailable and respawns on the next call."""
    LOGGER.error(json.dumps({"event": "workspace_lease_lost", "action": "exit", "pid": os.getpid()}))
    logging.shutdown()
    os._exit(LEASE_LOST_EXIT_CODE)


def serve_locked(connection, root: str, database: StoreDatabase | None = None):
    runtime = None
    try:
        try:
            runtime = Runtime(root, database)
        except sqlite3.Error as exc:
            if not login_failed(exc):
                raise
            refuse_login(connection, exc)
            return
        while True:
            payload = connection.recv()
            if payload is None:
                break
            try:
                result = runtime.execute(Work.model_validate_json(payload))
                response = Reply(data=result)
            except DomainError as exc:
                runtime.store.db.rollback()
                runtime.invalidate()
                response = Reply(error=str(exc), code=exc.code)
            except Exception:
                LOGGER.exception('workspace_operation_failed')
                runtime.store.db.rollback()
                runtime.invalidate()
                response = Reply(error="Workspace operation failed", code="unavailable")
            connection.send(response.model_dump_json())
    except EOFError:
        LOGGER.info('workspace_connection_closed')
    finally:
        if runtime is not None:
            runtime.store.db.close()
        connection.close()


class WorkerPool:
    def __init__(self, root: Path, maximum: int = DEFAULT_WORKERS, timeout: float = DEFAULT_OPERATION_TIMEOUT,
                 environment: Callable[[], Mapping[str, str]] | None = None,
                 plane: ControlPlane | None = None, provisioner: WorkspaceProvisioner | None = None,
                 registry: "Registry | None" = None, maintenance: MaintenanceGate | None = None):
        """``registry`` should be the gateway's own: a Registry built here opens a second control plane.
        ``plane`` defaults to ``registry.plane``; ``provisioner`` defaults to the active backend's
        ``plane.workspaces`` read at every spawn, so a live switch is honoured."""
        if maximum < 1 or timeout <= 0:
            raise ValueError("Worker count and timeout must be positive")
        self.root, self.maximum, self.timeout = root, maximum, timeout
        self.environment = environment or dict
        self.workers = OrderedDict()
        self.lock = threading.Lock()
        self.context = multiprocessing.get_context("spawn")
        if registry is None:
            from team_memory.registry import Registry
            registry = Registry(root)
        self.registry = registry
        self.plane = plane if plane is not None else getattr(registry, "plane", None)
        self.provisioner = provisioner
        self.maintenance = maintenance
        self.provision_lock = threading.Lock()
        self.provisioned: set[str] = set()
        self.repair: set[str] = set()
        self.provisioned_target: tuple[str, int] | None = None
        self.key_locks: dict[str, threading.Lock] = {}

    def search_order(self, workspaces: list[Workspace]) -> list[Workspace]:
        with self.lock:
            return sorted(workspaces, key=lambda workspace: workspace.key not in self.workers)

    def invoke(self, work: Work, credential: str | Callable[[], Actor]):
        key = work.workspace.key
        target: tuple[int | None, StoreDatabase] | None = None
        repaired = False
        while True:
            with self.lock:
                actor = credential() if callable(credential) else self.registry.authenticate(credential)
                self.registry.authorize(actor, work.workspace.scope,
                                        work.operation in ("memory_save", "memory_update", "memory_delete"))
                if actor != work.actor:
                    raise Conflict("Identity changed; retry the request")
                if key in self.workers or (target is not None and target[0] == self.generation()):
                    try:
                        return self.call(key, work, None if target is None else target[1])
                    except WorkspaceLoginFailed as exc:
                        # The role exists but its password drifted (restore, manual ALTER ROLE): the
                        # fingerprint check in ensure() cannot see that, so force one repair and respawn.
                        # The work never ran, so resending it is safe.
                        if repaired:
                            raise Unavailable("Workspace unavailable; check history before retrying a write") from exc
                        repaired = True
                elif self.maintenance is not None and self.maintenance.state() is not None:
                    raise Unavailable(MAINTENANCE_MESSAGE)
                generation = self.generation()
            # Provisioning may run DDL; it must not hold the pool lock that every workspace's calls share.
            target = (generation, self.store_database(key))

    def call(self, key: str, work: Work, database: StoreDatabase | None):
        """Send ``work`` to the worker of ``key``, spawning it on ``database``; caller holds ``self.lock``."""
        if key not in self.workers:
            if self.maintenance is not None and self.maintenance.state() is not None:
                raise Unavailable(MAINTENANCE_MESSAGE)
            if len(self.workers) >= self.maximum:
                _, worker = self.workers.popitem(last=False)
                self.stop(worker)
            parent, child = self.context.Pipe()
            process = self.context.Process(target=serve, args=(child, str(self.root / "workspaces" / key),
                                                               dict(self.environment()), database), daemon=True)
            process.start()
            child.close()
            self.workers[key] = (process, parent)
        self.workers.move_to_end(key)
        process, connection = self.workers[key]
        try:
            connection.send(work.model_dump_json())
            if not connection.poll(self.timeout):
                raise Unavailable("Workspace timeout; check history before retrying a write")
            reply = Reply.model_validate_json(connection.recv())
            if reply.code == LOGIN_FAILED_CODE:
                raise WorkspaceLoginFailed(reply.error or "Workspace role login failed")
        except (EOFError, OSError, Unavailable) as exc:
            self.stop(self.workers.pop(key))
            if isinstance(exc, WorkspaceLoginFailed):
                self.forget(key, repair=True)
                raise
            # A worker that died may have lost its schema, or its role login fails (restore, changed
            # master key): provision again next time, forcing the role repair.
            self.forget(key, repair=True)
            raise Unavailable("Workspace unavailable; check history before retrying a write") from exc
        if reply.error:
            if reply.code == "conflict":
                raise Conflict(reply.error)
            raise Unavailable(reply.error)
        return reply.data

    def generation(self) -> int | None:
        return None if self.plane is None else self.plane.current().generation

    def forget(self, key: str, *, repair: bool = False) -> None:
        """Drop ``key`` from the provisioned cache (call after the workspace was dropped or purged).
        ``repair`` makes the next ``ensure`` force-reset the role (its worker could not start)."""
        with self.provision_lock:
            self.provisioned.discard(key)
            if repair:
                self.repair.add(key)

    def store_database(self, key: str) -> StoreDatabase:
        """Where a new worker for ``key`` keeps its data, read from the active control plane at spawn.

        ``ensure`` runs once per key and active target: the cache is keyed by the plane's instance and
        generation, so an activation or repoint provisions again. A per-key lock keeps two requests
        from provisioning the same workspace at once without serialising different workspaces.
        """
        if self.plane is None or self.plane.current().backend is Backend.SQLITE:
            return StoreDatabase.sqlite()
        provisioner = self.provisioner or getattr(self.plane, "workspaces", None)
        if provisioner is None:
            raise Unavailable("PostgreSQL is active but no workspace provisioner is configured")
        import psycopg

        current = self.plane.current()
        with self.provision_lock:
            if self.provisioned_target != (current.instance_id, current.generation):
                self.provisioned.clear()
                self.provisioned_target = (current.instance_id, current.generation)
            key_lock = self.key_locks.setdefault(key, threading.Lock())
        try:
            with key_lock:
                with self.provision_lock:
                    cached, force = key in self.provisioned, key in self.repair
                if not cached:
                    if force:
                        provisioner.ensure(key, force=True)
                    else:
                        provisioner.ensure(key)
                    with self.provision_lock:
                        self.repair.discard(key)
                        if self.provisioned_target == (current.instance_id, current.generation):
                            self.provisioned.add(key)
                return provisioner.store_database(key)
        except (sqlite3.Error, psycopg.Error, DomainError, OSError) as exc:
            # Only our own messages are logged verbatim; for driver errors the SQLSTATE is enough and
            # cannot carry connection details.
            reason = str(exc)[:PROVISIONING_REASON_CHARS] if isinstance(exc, DomainError) else None
            LOGGER.error(json.dumps({"event": "workspace_provisioning_failed", "error": type(exc).__name__,
                                     "sqlstate": getattr(exc, "sqlstate", None), "reason": reason}))
            raise Unavailable("Workspace unavailable; check history before retrying a write") from exc

    @staticmethod
    def stop(worker):
        process, connection = worker
        if process.is_alive():
            process.terminate()
        process.join(STOP_TIMEOUT_SECONDS)
        if process.is_alive():
            process.kill()
            process.join()
        connection.close()
        process.close()

    def recycle(self) -> int:
        with self.lock:
            stopped = len(self.workers)
            for worker in self.workers.values():
                self.stop(worker)
            self.workers.clear()
        LOGGER.info(json.dumps({"event": "worker_pool_recycled", "stopped": stopped}))
        return stopped

    def close(self):
        with self.lock:
            for worker in self.workers.values():
                self.stop(worker)
            self.workers.clear()

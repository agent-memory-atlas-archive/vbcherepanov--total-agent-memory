"""The setup wizard's Database step: choose SQLite or PostgreSQL before the first admin exists.

Every call re-verifies the one-time setup code (with SetupService's lockout) and is refused
once setup has moved past the first admin. The Test button runs in an isolated child process
(PgDatabaseChecker(isolated=True)) so a browser-supplied DSN can never borrow the server's own
PGPASSWORD, ~/.pgpass, service files or client certificates. Choosing PostgreSQL runs the same migration job
as the dashboard on the near-empty identity database, so there is no second code path.
"""
import json
import logging
import time
from collections.abc import Callable

from tam_db.contracts import Backend
from team_memory.contracts import DTO, Conflict, Forbidden, Unavailable
from team_memory.database_config import AUDIT_SUBJECT, DATABASE_TESTED
from team_memory.database_contracts import (
    CheckReport,
    ConfigSource,
    DatabaseChecker,
    DatabaseConfigService,
    DatabaseConfigView,
    DatabaseDsn,
    DsnOrigin,
    MigrationPhase,
    MigrationProgress,
    MigrationRunner,
    SetupDatabaseRequest,
    SetupDatabaseTestRequest,
)
from team_memory.registry import Registry
from team_memory.setup import SetupService

LOGGER = logging.getLogger(__name__)
SETUP_ACTOR = "setup"
SETUP_MIGRATION_WAIT_SECONDS = 300
SETUP_MIGRATION_POLL_SECONDS = 0.25
SETUP_CLOSED = "Setup is complete; change the database under Administration → Database"
DATABASE_UNAVAILABLE = "Database management is not available on this server"


class SetupDatabaseChoice(DTO):
    """What the wizard shows before the choice: no DSN, not even masked (the caller is unauthenticated)."""

    backend: Backend
    source: ConfigSource


class SetupDatabaseService:
    def __init__(self, setup: SetupService, registry: Registry, database: DatabaseConfigService | None,
                 migration: MigrationRunner | None, checker: DatabaseChecker | None = None,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 wait_seconds: float = SETUP_MIGRATION_WAIT_SECONDS):
        self.setup, self.registry = setup, registry
        self.database, self.migration = database, migration
        self.checker = checker or _isolated_checker()
        self.clock, self.sleep, self.wait_seconds = clock, sleep, wait_seconds

    def current(self) -> SetupDatabaseChoice | None:
        if self.database is None:
            return None
        view = self.database.view()
        return SetupDatabaseChoice(backend=view.backend, source=view.source)

    def test(self, request: SetupDatabaseTestRequest, ip: str) -> CheckReport:
        self._authorize(request.token, ip)
        self._database()  # same availability as the choice itself: nothing to test without database management
        dsn = DatabaseDsn.parse(request.dsn.get_secret_value(), origin=DsnOrigin.WEB)
        report = self.checker.check(dsn, instance_id=None)
        detail = {**dsn.audit().model_dump(mode="json"), "ok": report.ok, "target_state": report.target_state.value}
        with self.registry.acting_as(SETUP_ACTOR):
            self.registry.record_event(DATABASE_TESTED, AUDIT_SUBJECT, json.dumps(detail, sort_keys=True))
        LOGGER.info(json.dumps({"event": DATABASE_TESTED, "actor": SETUP_ACTOR, **detail}))
        return report

    def choose(self, request: SetupDatabaseRequest, ip: str) -> DatabaseConfigView:
        self._authorize(request.token, ip)
        database = self._database()
        current = database.view()
        if request.backend is Backend.SQLITE:
            if current.backend is not Backend.SQLITE:
                raise Conflict("This server already runs on PostgreSQL (" + current.source.value + " setting); "
                               "keep it or change the configuration and restart")
            return current
        running = self._migration().progress()
        if running is None or running.terminal:
            # A retry after a bounded wait may find the switch already finished.
            if database.view().backend is Backend.POSTGRES:
                return database.view()
            running = self._start(DatabaseDsn.parse(request.dsn.get_secret_value(), origin=DsnOrigin.WEB))
        finished = self._wait(running)
        if finished.phase is not MigrationPhase.DONE:
            raise Conflict(finished.error or "The switch to PostgreSQL was " + finished.phase.value)
        return database.view()

    def _start(self, dsn: DatabaseDsn) -> MigrationProgress:
        migration = self._migration()
        plan = migration.plan(dsn, SETUP_ACTOR)
        if not plan.ready:
            raise Conflict("PostgreSQL is not ready: " + "; ".join(plan.blockers))
        LOGGER.info(json.dumps({"event": "setup_database_migration", "plan_id": str(plan.plan_id),
                                **dsn.audit().model_dump(mode="json")}))
        return migration.start(plan.plan_id, SETUP_ACTOR)

    def _wait(self, progress: MigrationProgress) -> MigrationProgress:
        deadline = self.clock() + self.wait_seconds
        while not progress.terminal:
            if self.clock() >= deadline:
                raise Unavailable("The switch to PostgreSQL is still running; press Continue again in a minute")
            self.sleep(SETUP_MIGRATION_POLL_SECONDS)
            progress = self._migration().progress() or progress
        return progress

    def _authorize(self, token: str, ip: str) -> None:
        if self.registry.organization().get("setup_state") or self.registry.has_superadmin():
            raise Forbidden(SETUP_CLOSED)
        self.setup.verify(token, ip)

    def _database(self) -> DatabaseConfigService:
        if self.database is None:
            raise Unavailable(DATABASE_UNAVAILABLE)
        return self.database

    def _migration(self) -> MigrationRunner:
        if self.migration is None:
            raise Unavailable(DATABASE_UNAVAILABLE)
        return self.migration


def _isolated_checker() -> DatabaseChecker:
    from team_memory.db_check import PgDatabaseChecker

    return PgDatabaseChecker(isolated=True)

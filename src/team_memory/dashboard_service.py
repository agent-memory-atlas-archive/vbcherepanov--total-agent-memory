import asyncio
from collections.abc import Callable

import httpx
from pydantic import JsonValue, SecretStr

from tam_db.contracts import Backend
from team_memory import provider_check
from team_memory.accounts import Accounts, Invite, Session
from team_memory.contracts import (
    OVERSIGHT_ROLES,
    Actor,
    AuditPage,
    DomainError,
    Forbidden,
    InviteRedeem,
    MembershipChange,
    MemoryCall,
    OrganizationUpdate,
    OrgRoleChange,
    PasswordChange,
    PasswordLogin,
    ProviderTest,
    SettingsChange,
    TeamCreate,
    TeamRef,
    TeamRename,
    TokenCreate,
    TokenFilter,
    TokenLogin,
    TokenRef,
    Unavailable,
    UserActive,
    UserCreate,
    UserRef,
)
from team_memory.database_contracts import (
    CheckReport,
    DatabaseConfigService,
    DatabaseConfigView,
    DatabaseDsn,
    DatabasePlanRequest,
    DatabaseRepointRequest,
    DatabaseRollbackRequest,
    DatabaseTestRequest,
    DsnOrigin,
    MaintenanceGate,
    MigrationPlan,
    MigrationProgress,
    MigrationRunner,
    MigrationStartRequest,
)
from team_memory.metrics import Metrics
from team_memory.overview import InsightService
from team_memory.registry import Registry
from team_memory.sections import visible_sections
from team_memory.service import MemoryService
from team_memory.settings import COMMON_FIELDS, PROVIDER_KEYS, PROVIDERS, SettingsStore
from team_memory.setup import SUPPORT_LINE, SUPPORT_URL

BACKUP_COMMAND = "tam-team --root {root} backup --out /path/outside/data/backup-YYYYMMDD"
SQLITE_BACKUP_REASON = "Backups need exclusive access to every database; stop the server first."
POSTGRES_BACKUP_REASON = ("The memory lives in PostgreSQL: the backup command takes one consistent pg_dump snapshot while "
                          "the server runs. Restore goes into an empty database with `tam-team --root {root} restore "
                          "--from DIR --dsn-env VAR`. For continuous protection use your PostgreSQL provider's "
                          "point-in-time recovery.")
DATABASE_UNAVAILABLE = "Database management is not available on this server"


class DashboardService:
    def __init__(self, registry: Registry, accounts: Accounts, memory: MemoryService, settings: SettingsStore,
                 metrics: Metrics, provider_transport: httpx.BaseTransport | None = None,
                 database: DatabaseConfigService | None = None, migration: MigrationRunner | None = None,
                 maintenance: MaintenanceGate | None = None):
        self.registry, self.accounts, self.memory = registry, accounts, memory
        self.settings, self.metrics = settings, metrics
        self.provider_transport = provider_transport
        self.database, self.migration, self.maintenance = database, migration, maintenance
        self.insights = InsightService(registry, accounts, settings, memory.pool)

    def login_password(self, request: PasswordLogin, ip: str) -> Session:
        return self._login("password", lambda: self.accounts.login_password(request.user_id, request.password, ip))

    def login_token(self, request: TokenLogin, ip: str) -> Session:
        return self._login("token", lambda: self.accounts.login_token(request.token, ip))

    def redeem_invite(self, request: InviteRedeem, ip: str) -> Session:
        return self._login("invite", lambda: self.accounts.redeem_invite(request.user_id, request.code,
                                                                          request.password, ip))

    def _login(self, method: str, action: Callable[[], Session]) -> Session:
        try:
            session = action()
        except Exception as exc:
            self.metrics.count("login", method=method, outcome=getattr(exc, "code", "error"))
            raise
        self.metrics.count("login", method=method, outcome="success")
        return session

    def overview(self, session: Session) -> dict:
        actor = session.actor
        names = dict(self.registry.list_teams())
        teams = [{"team_id": team_id, "name": names.get(team_id, team_id), "role": role}
                 for team_id, role in self.registry.teams_of(actor.user_id)]
        viewable = [{"team_id": team_id, "name": name} for team_id, name in self.registry.list_teams()
                    if self.registry.can_view_team_people(actor, team_id)]
        sections = [{"id": s.id, "title": s.title, "script": s.script, "stylesheet": s.stylesheet, "mount": s.mount,
                     "group": s.group, "icon": s.icon} for s in visible_sections(self.registry, actor)]
        organization = self.registry.organization()
        pending = self.registry.org_role(actor.user_id) == "superadmin" and \
            organization.get("setup_state") == "admin_created"
        return {"user": {"user_id": actor.user_id, "display_name": actor.display_name, "org_role": actor.org_role,
                         "password_set": self.accounts.has_password(actor.user_id),
                         "role_label": self.insights.role_label(actor)},
                "csrf": session.csrf, "method": session.method, "teams": teams, "viewableTeams": viewable,
                "sections": sections, "organization": {"name": organization.get("name")},
                "setup_pending": pending,
                **({"support_line": SUPPORT_LINE, "support_url": SUPPORT_URL} if pending else {})}

    async def call_memory(self, credential: Callable[[], Actor], request: MemoryCall) -> JsonValue:
        return await self.memory.call(credential, request.name, request.arguments)

    def my_tokens(self, actor: Actor) -> list[dict]:
        return self.registry.tokens_of(actor.user_id)

    def create_token(self, actor: Actor, request: TokenCreate) -> dict:
        with self.registry.acting_as(actor.user_id):
            return {"token": self.registry.issue_token(actor.user_id, request.client), "client": request.client}

    def revoke_my_token(self, actor: Actor, request: TokenRef) -> dict:
        with self.registry.acting_as(actor.user_id):
            self.registry.revoke_token_id(request.id, owner_id=actor.user_id)
        return {"revoked": request.id}

    def change_password(self, session: Session, request: PasswordChange, ip: str) -> dict:
        with self.registry.acting_as(session.actor.user_id):
            self.accounts.change_password(session.actor, request.current, request.new, session.session_id, ip)
        return {"changed": True}

    def team_people(self, actor: Actor, request: TeamRef) -> dict:
        if not self.registry.can_view_team_people(actor, request.team_id):
            raise Forbidden("Department unavailable")
        activity = self.team_activity(request.team_id)
        members = []
        for user_id, name, role in self.registry.team_members(request.team_id):
            stats = activity.get(user_id, {})
            members.append({"user_id": user_id, "name": name, "role": role, "saves": stats.get("saves", 0),
                            "changes": stats.get("changes", 0), "last_activity": stats.get("last_activity")})
        return {"team_id": request.team_id, "name": dict(self.registry.list_teams())[request.team_id],
                "members": members}

    def company(self, actor: Actor) -> list[dict]:
        self._require_oversight(actor)
        result = []
        for team_id, name in self.registry.list_teams():
            members = self.registry.team_members(team_id)
            activity = self.team_activity(team_id).values()
            result.append({"team_id": team_id, "name": name, "members": len(members),
                           "managers": [member_name for _, member_name, role in members if role == "manager"],
                           "saves": sum(item["saves"] for item in activity),
                           "last_activity": max((item["last_activity"] for item in activity), default=None)})
        return result

    def team_activity(self, team_id: str) -> dict[str, dict]:
        return self.insights.reader.activity(self.registry.team_workspace_key(team_id))

    def users(self, actor: Actor) -> list[dict]:
        self._require_superadmin(actor)
        return [{**user, "teams": [{"team_id": t, "role": r} for t, r in self.registry.teams_of(user["id"])]}
                for user in self.registry.list_users()]

    def create_user(self, actor: Actor, request: UserCreate) -> Invite:
        self._admin(actor, "user_create")
        with self.registry.acting_as(actor.user_id):
            self.registry.add_user(request.id, request.name)
            if request.org_role != "member":
                self.registry.set_org_role(request.id, request.org_role)
            return self.accounts.issue_invite(request.id)

    def issue_invite(self, actor: Actor, request: UserRef) -> Invite:
        self._admin(actor, "invite")
        with self.registry.acting_as(actor.user_id):
            return self.accounts.issue_invite(request.user_id)

    def set_active(self, actor: Actor, request: UserActive) -> dict:
        self._admin(actor, "user_active")
        with self.registry.acting_as(actor.user_id):
            effect = self.registry.set_active(request.user_id, request.active)
        return {"user_id": request.user_id, "active": request.active, **effect}

    def set_org_role(self, actor: Actor, request: OrgRoleChange) -> dict:
        self._admin(actor, "org_role")
        with self.registry.acting_as(actor.user_id):
            self.registry.set_org_role(request.user_id, request.org_role)
        return {"user_id": request.user_id, "org_role": request.org_role}

    def teams(self, actor: Actor) -> list[dict]:
        self._require_superadmin(actor)
        return [{"team_id": team_id, "name": name,
                 "members": [{"user_id": u, "name": n, "role": r} for u, n, r in self.registry.team_members(team_id)]}
                for team_id, name in self.registry.list_teams()]

    def create_team(self, actor: Actor, request: TeamCreate) -> dict:
        self._admin(actor, "team_create")
        with self.registry.acting_as(actor.user_id):
            self.registry.add_team(request.id, request.name)
        return {"team_id": request.id}

    def rename_team(self, actor: Actor, request: TeamRename) -> dict:
        self._admin(actor, "team_rename")
        with self.registry.acting_as(actor.user_id):
            self.registry.rename_team(request.team_id, request.name)
        return {"team_id": request.team_id, "name": request.name}

    def delete_team(self, actor: Actor, request: TeamRef) -> dict:
        self._admin(actor, "team_delete")
        with self.registry.acting_as(actor.user_id):
            self.registry.delete_team(request.team_id)
        return {"deleted": request.team_id}

    def change_membership(self, actor: Actor, request: MembershipChange) -> dict:
        self._admin(actor, "membership")
        with self.registry.acting_as(actor.user_id):
            self.registry.membership(request.user_id, request.team_id, request.role)
        return request.model_dump()

    def all_tokens(self, actor: Actor, request: TokenFilter) -> list[dict]:
        self._require_superadmin(actor)
        return self.registry.tokens_of(request.user_id)

    def admin_revoke_token(self, actor: Actor, request: TokenRef) -> dict:
        self._admin(actor, "token_revoke")
        with self.registry.acting_as(actor.user_id):
            self.registry.revoke_token_id(request.id)
        return {"revoked": request.id}

    def audit(self, actor: Actor, request: AuditPage) -> dict:
        self._require_superadmin(actor)
        events = self.registry.audit_events(request.before, request.limit, request.actor, request.action, request.subject)
        return {"events": events, "next": events[-1]["id"] if len(events) == request.limit else None,
                "actions": self.registry.audit_actions()}

    def settings_view(self, actor: Actor) -> dict:
        self._require_superadmin(actor)
        return {"settings": [view.model_dump() for view in self.settings.view()],
                "providers": self.insights.provider_status(), "common": COMMON_FIELDS, "provider_keys": PROVIDER_KEYS}

    async def update_settings(self, actor: Actor, request: SettingsChange) -> dict:
        self._admin(actor, "settings_update")
        with self.registry.acting_as(actor.user_id):
            changed = self.settings.update(request.values)
        stopped = await asyncio.to_thread(self.memory.pool.recycle)
        return {"changed": changed, "workers_recycled": stopped}

    async def test_provider(self, actor: Actor, request: ProviderTest) -> dict:
        self._admin(actor, "provider_test")
        if request.provider is not None and not any(p.target == request.target and p.id == request.provider
                                                    for p in PROVIDERS):
            raise DomainError("Unknown provider for " + request.target)
        endpoint = provider_check.resolve(request.target, self.settings.effective(), request.provider)
        result = await asyncio.to_thread(provider_check.check, endpoint, self.provider_transport)
        self.settings.record_check(result.target, result.provider, result.ok, result.detail)
        with self.registry.acting_as(actor.user_id):
            self.registry.record_event("provider_tested", request.target + ":" + result.provider,
                                       "ok" if result.ok else "failed")
        return result.model_dump()

    def backup_info(self, actor: Actor) -> dict:
        self._require_superadmin(actor)
        command = BACKUP_COMMAND.format(root=self.registry.root)
        if self.database is not None and self.database.view().backend is Backend.POSTGRES:
            return {"online": True, "backend": Backend.POSTGRES.value, "reason": POSTGRES_BACKUP_REASON.format(root=self.registry.root), "command": command}
        return {"online": False, "backend": Backend.SQLITE.value, "reason": SQLITE_BACKUP_REASON, "command": command}

    def database_view(self, actor: Actor) -> DatabaseConfigView:
        self._require_superadmin(actor)
        view = self._database().view()
        return view.model_copy(update={"maintenance": self.maintenance.state() if self.maintenance else None})

    def test_database(self, actor: Actor, request: DatabaseTestRequest) -> CheckReport:
        self._admin(actor, "database_test")
        return self._database().test(self._dsn(request.dsn), actor.user_id)

    def plan_migration(self, actor: Actor, request: DatabasePlanRequest) -> MigrationPlan:
        self._admin(actor, "database_plan")
        return self._migration().plan(self._dsn(request.dsn), actor.user_id)

    def start_migration(self, actor: Actor, request: MigrationStartRequest) -> MigrationProgress:
        self._admin(actor, "database_migrate")
        return self._migration().start(request.plan_id, actor.user_id)

    def migration_progress(self, actor: Actor) -> MigrationProgress | None:
        self._require_superadmin(actor)
        return self._migration().progress()

    def cancel_migration(self, actor: Actor) -> MigrationProgress:
        self._admin(actor, "database_cancel")
        return self._migration().cancel(actor.user_id)

    def repoint_database(self, actor: Actor, request: DatabaseRepointRequest) -> DatabaseConfigView:
        self._admin(actor, "database_repoint")
        return self._database().repoint(self._dsn(request.dsn), actor.user_id)

    def rollback_database(self, actor: Actor, request: DatabaseRollbackRequest) -> DatabaseConfigView:
        self._admin(actor, "database_rollback")
        self._migration().rollback(request.organization, actor.user_id)
        return self._database().view()

    def organization(self, actor: Actor) -> dict:
        self._require_superadmin(actor)
        return self.registry.organization()

    def update_organization(self, actor: Actor, request: OrganizationUpdate) -> dict:
        self._admin(actor, "organization_update")
        with self.registry.acting_as(actor.user_id):
            self.registry.set_organization(request.model_dump(exclude_none=True))
        return self.registry.organization()

    def finish_setup(self, actor: Actor) -> dict:
        self._admin(actor, "setup_finish")
        with self.registry.acting_as(actor.user_id):
            self.registry.set_organization({"setup_state": "complete"})
        return self.registry.organization()

    def metrics_view(self, actor: Actor) -> dict:
        self._require_superadmin(actor)
        return {"gateway": self.metrics.snapshot(),
                "memory_calls": [{"tool": tool, "status": status, "value": value}
                                 for (tool, status), value in sorted(self.memory.counts.items())]}

    def _database(self) -> DatabaseConfigService:
        if self.database is None:
            raise Unavailable(DATABASE_UNAVAILABLE)
        return self.database

    def _migration(self) -> MigrationRunner:
        if self.migration is None:
            raise Unavailable(DATABASE_UNAVAILABLE)
        return self.migration

    @staticmethod
    def _dsn(secret: SecretStr) -> DatabaseDsn:
        return DatabaseDsn.parse(secret.get_secret_value(), origin=DsnOrigin.WEB)

    def _admin(self, actor: Actor, action: str) -> None:
        try:
            self._require_superadmin(actor)
        except Forbidden:
            self.metrics.count("admin_action", action=action, outcome="forbidden")
            raise
        self.metrics.count("admin_action", action=action, outcome="accepted")

    def _require_superadmin(self, actor: Actor) -> None:
        if self.registry.org_role(actor.user_id) != "superadmin":
            raise Forbidden("Superadmin role required")

    def _require_oversight(self, actor: Actor) -> None:
        if self.registry.org_role(actor.user_id) not in OVERSIGHT_ROLES:
            raise Forbidden("Company viewer role required")

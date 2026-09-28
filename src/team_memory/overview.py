import time

from team_memory import provider_check
from team_memory.accounts import Accounts
from team_memory.contracts import (
    OVERSIGHT_ROLES,
    Actor,
    Forbidden,
    ScopeKind,
    TeamRef,
    Workspace,
)
from team_memory.insights import WorkspaceReader, older_than, series
from team_memory.registry import Registry
from team_memory.settings import PROVIDERS, SettingsStore
from team_memory.worker import WorkerPool
from version import RELEASE_DATE, VERSION

TREND_DAYS = 30
INACTIVE_DAYS = 14
RECENT_LIMIT = 8
AUDIT_PREVIEW = 8
LOGIN_WINDOW_HOURS = 24
ORG_LABELS = {"superadmin": "Superadmin", "company_viewer": "Company viewer"}
TEAM_LABELS = {"manager": "Manager", "editor": "Editor", "reader": "Reader"}
TEAM_RANK = {"manager": 0, "editor": 1, "reader": 2}


def _sum(rows: list[list[int]], days: int) -> list[int]:
    return [sum(values) for values in zip(*rows)] if rows else [0] * days


class InsightService:
    def __init__(self, registry: Registry, accounts: Accounts, settings: SettingsStore, pool: WorkerPool):
        self.registry, self.accounts, self.settings, self.pool = registry, accounts, settings, pool
        self.reader = WorkspaceReader(registry.root, registry.plane)
        self.started = time.monotonic()

    def role_label(self, actor: Actor) -> str:
        role = self.registry.org_role(actor.user_id)
        if role in ORG_LABELS:
            return ORG_LABELS[role]
        memberships = self.registry.teams_of(actor.user_id)
        if not memberships:
            return "Member"
        names = dict(self.registry.list_teams())
        best = min(TEAM_RANK[r] for _, r in memberships)
        top = [names.get(t, t) for t, r in memberships if TEAM_RANK[r] == best]
        label = next(k for k, v in TEAM_RANK.items() if v == best)
        return TEAM_LABELS[label] + " · " + ", ".join(top)

    def _label(self, workspace: Workspace, names: dict[str, str]) -> str:
        if workspace.scope.kind == ScopeKind.personal:
            return "Personal"
        if workspace.scope.kind == ScopeKind.shared:
            return "Shared"
        return names.get(workspace.scope.team_id, workspace.scope.team_id)

    def me(self, actor: Actor) -> dict:
        names = dict(self.registry.list_teams())
        scopes, trends, recent, last = [], [], [], None
        for workspace in self.registry.workspaces(actor):
            label = self._label(workspace, names)
            scopes.append({"scope": workspace.scope.model_dump(mode="json"), "label": label,
                           "records": self.reader.active_records(workspace.key)})
            daily = self.reader.daily_saves(workspace.key, TREND_DAYS, actor.user_id).get(actor.user_id, {})
            trends.append(series(daily, TREND_DAYS))
            mine = self.reader.activity(workspace.key).get(actor.user_id, {})
            last = max(filter(None, (last, mine.get("last_activity"))), default=None)
            recent.extend({**item, "scope_label": label, "scope": workspace.scope.model_dump(mode="json")}
                          for item in self.reader.recent(workspace.key, RECENT_LIMIT, actor.user_id))
        trend = _sum(trends, TREND_DAYS)
        tokens = [t for t in self.registry.tokens_of(actor.user_id) if not t["revoked"]]
        return {"scopes": scopes, "last_activity": last, "active_tokens": len(tokens), "saves_30d": sum(trend),
                "trend_30d": trend, "recent": sorted(recent, key=lambda i: i["at"], reverse=True)[:RECENT_LIMIT]}

    def team(self, actor: Actor, request: TeamRef) -> dict:
        if not self.registry.can_view_team_people(actor, request.team_id):
            raise Forbidden("Department unavailable")
        key = self.registry.team_workspace_key(request.team_id)
        activity = self.reader.activity(key)
        daily = self.reader.daily_saves(key, TREND_DAYS)
        members = []
        for user_id, name, role in self.registry.team_members(request.team_id):
            stats = activity.get(user_id, {})
            trend = series(daily.get(user_id, {}), TREND_DAYS)
            members.append({"user_id": user_id, "name": name, "role": role, "saves_30d": sum(trend), "trend_30d": trend,
                            "saves": stats.get("saves", 0), "last_activity": stats.get("last_activity"),
                            "inactive": older_than(stats.get("last_activity"), INACTIVE_DAYS)})
        trend = _sum([m["trend_30d"] for m in members], TREND_DAYS)
        return {"team_id": request.team_id, "name": dict(self.registry.list_teams())[request.team_id],
                "records": self.reader.active_records(key), "saves_30d": sum(trend), "trend_30d": trend,
                "inactive_days": INACTIVE_DAYS, "members": members,
                "inactive": [m["user_id"] for m in members if m["inactive"]],
                "recent_records": self.reader.recent(key, RECENT_LIMIT, operations=("insert",))}

    def company(self, actor: Actor) -> dict:
        if self.registry.org_role(actor.user_id) not in OVERSIGHT_ROLES:
            raise Forbidden("Company viewer role required")
        departments = []
        for team_id, name in self.registry.list_teams():
            key = self.registry.team_workspace_key(team_id)
            trend = _sum([series(d, TREND_DAYS) for d in self.reader.daily_saves(key, TREND_DAYS).values()], TREND_DAYS)
            activity = self.reader.activity(key).values()
            departments.append({"team_id": team_id, "name": name, "members": len(self.registry.team_members(team_id)),
                                "records": self.reader.active_records(key), "saves_30d": sum(trend), "trend_30d": trend,
                                "last_activity": max((a["last_activity"] for a in activity if a["last_activity"]),
                                                     default=None)})
        users = self.registry.list_users()
        trend = _sum([d["trend_30d"] for d in departments], TREND_DAYS)
        return {"departments": departments, "trend_30d": trend, "saves_30d": sum(trend),
                "records": sum(d["records"] for d in departments),
                "active_users": sum(1 for u in users if u["active"])}

    def system(self, actor: Actor) -> dict:
        if self.registry.org_role(actor.user_id) != "superadmin":
            raise Forbidden("Superadmin role required")
        users = self.registry.list_users()
        return {"version": VERSION, "release_date": RELEASE_DATE,
                "uptime_seconds": round(time.monotonic() - self.started),
                "workers": {"max": self.pool.maximum, "running": len(self.pool.workers), "busy": int(self.pool.lock.locked())},
                "providers": self.provider_status(active_only=True),
                "pending_invites": self.accounts.pending_invites(),
                "recent_audit": self.registry.audit_events(limit=AUDIT_PREVIEW),
                "logins": self.accounts.login_summary(LOGIN_WINDOW_HOURS),
                "users": {"active": sum(1 for u in users if u["active"]),
                          "disabled": sum(1 for u in users if not u["active"]),
                          "without_password": sum(1 for u in users if u["active"] and not u["password_set"])}}

    def provider_status(self, active_only: bool = False) -> list[dict]:
        env, checks = self.settings.effective(), self.settings.checks()
        active = {target: provider_check.resolve(target, env).provider for target in ("llm", "embed")}
        result = []
        for spec in PROVIDERS:
            if active_only and active[spec.target] != spec.id:
                continue
            problem = provider_check.missing(provider_check.resolve(spec.target, env, spec.id))
            result.append({"target": spec.target, "id": spec.id, "label": spec.label, "fields": spec.fields,
                           "local": spec.local, "active": active[spec.target] == spec.id,
                           "configured": problem is None, "problem": problem,
                           "check": checks.get((spec.target, spec.id))})
        return result

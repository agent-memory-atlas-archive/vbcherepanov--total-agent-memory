import re
from dataclasses import dataclass
from enum import Enum

from team_memory.contracts import OVERSIGHT_ROLES, Actor
from team_memory.registry import Registry

STATIC_PREFIX = "/dashboard/static/"


class Capability(str, Enum):
    authenticated = "authenticated"
    team_people = "team_people"
    company = "company"
    superadmin = "superadmin"


GROUPS = ("personal", "department", "company", "administration")
DEFAULT_GROUP = {Capability.authenticated: "personal", Capability.team_people: "department",
                 Capability.company: "company", Capability.superadmin: "administration"}


@dataclass(frozen=True)
class Section:
    id: str
    title: str
    capability: Capability
    script: str
    mount: str
    stylesheet: str | None = None
    order: int = 100
    group: str | None = None
    icon: str = "spark"

    def __post_init__(self):
        if re.fullmatch(r"[a-z][a-z0-9-]{0,31}", self.id) is None:
            raise ValueError("Section id must be lowercase letters, digits or hyphens")
        if re.fullmatch(r"[A-Za-z_$][\w$]*(\.[A-Za-z_$][\w$]*)*", self.mount) is None:
            raise ValueError("mount must be a dotted global JavaScript name")
        if re.fullmatch(r"[a-z][a-z0-9-]{0,31}", self.icon) is None:
            raise ValueError("icon must be a sprite symbol name")
        for path in (self.script, self.stylesheet):
            if path is not None and (not path.startswith("/") or path.startswith("//")):
                raise ValueError("Section assets must be same-origin absolute paths")
        object.__setattr__(self, "capability", Capability(self.capability))
        object.__setattr__(self, "group", self.group or DEFAULT_GROUP[self.capability])
        if self.group not in GROUPS:
            raise ValueError("group must be one of " + ", ".join(GROUPS))


def _builtin(id: str, title: str, capability: Capability, order: int, icon: str, script: str | None = None) -> Section:
    name = script or id
    return Section(id=id, title=title, capability=capability, script=STATIC_PREFIX + name + ".js",
                   mount="TamSections." + name.replace("-", "_"), order=order, icon=icon)


SECTIONS: list[Section] = [
    _builtin("overview", "Overview", Capability.authenticated, 5, "home"),
    _builtin("memory", "My memory", Capability.authenticated, 10, "brain"),
    _builtin("access", "Tokens & password", Capability.authenticated, 20, "key"),
    _builtin("department", "Department", Capability.team_people, 30, "users"),
    _builtin("company", "Company", Capability.company, 40, "building"),
    _builtin("admin-users", "Users", Capability.superadmin, 80, "user-cog"),
    _builtin("admin-teams", "Departments", Capability.superadmin, 81, "layers"),
    _builtin("admin-tokens", "Access tokens", Capability.superadmin, 82, "shield"),
    _builtin("audit", "Audit log", Capability.superadmin, 83, "list"),
    _builtin("settings", "Providers", Capability.superadmin, 84, "plug"),
    _builtin("system", "System", Capability.superadmin, 85, "pulse"),
]


def register(section: Section) -> None:
    if any(existing.id == section.id for existing in SECTIONS):
        raise ValueError(f"Section {section.id!r} is already registered")
    SECTIONS.append(section)


def capabilities(registry: Registry, actor: Actor) -> set[Capability]:
    role = registry.org_role(actor.user_id)
    result = {Capability.authenticated}
    if role in OVERSIGHT_ROLES:
        result |= {Capability.company, Capability.team_people}
    if role == "superadmin":
        result.add(Capability.superadmin)
    if any(team_role == "manager" for _, team_role in registry.teams_of(actor.user_id)):
        result.add(Capability.team_people)
    return result


def visible_sections(registry: Registry, actor: Actor) -> list[Section]:
    granted = capabilities(registry, actor)
    return sorted((section for section in SECTIONS if section.capability in granted), key=lambda s: (s.order, s.id))

import asyncio
import hmac
import inspect
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from functools import wraps
from pathlib import Path
from typing import Literal, TypeVar

from pydantic import BaseModel, ValidationError
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from team_memory.accounts import Session
from team_memory.contracts import (
    DTO,
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
    Unauthorized,
    UserActive,
    UserCreate,
    UserRef,
)
from team_memory.dashboard_service import DashboardService
from team_memory.database_contracts import (
    DATABASE_API,
    DATABASE_MIGRATE_API,
    DATABASE_MIGRATION_API,
    DATABASE_MIGRATION_CANCEL_API,
    DATABASE_PLAN_API,
    DATABASE_REPOINT_API,
    DATABASE_ROLLBACK_API,
    DATABASE_TEST_API,
    DatabasePlanRequest,
    DatabaseRepointRequest,
    DatabaseRollbackRequest,
    DatabaseTestRequest,
    MigrationStartRequest,
)
from team_memory.sections import (
    SECTIONS,
    STATIC_PREFIX,
    Capability,
    Section,
    capabilities,
    register,
)

LOGGER = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).resolve().parent / "static"
API_PREFIX = "/dashboard/api"
STATIC_NAME = re.compile(r"(?:fonts/)?[a-z0-9_-]{1,64}\.(js|css|svg|woff2|txt)")
CONTENT_TYPES = {"js": "text/javascript; charset=utf-8", "css": "text/css; charset=utf-8",
                 "svg": "image/svg+xml", "woff2": "font/woff2", "txt": "text/plain; charset=utf-8"}
IMMUTABLE_TYPES = frozenset(("woff2",))
ASSET_MAX_AGE_SECONDS = 7 * 24 * 3600
COOKIE = "tam_session"
SECURE_COOKIE = "__Host-tam_session"
CSRF_HEADER = "x-csrf-token"
SAFE_METHODS = frozenset(("GET", "HEAD"))
STATUS = {"unauthorized": 401, "forbidden": 403, "conflict": 409, "unavailable": 503, "rate_limited": 429,
          "not_found": 404}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
SECURITY_HEADERS = {"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
                    "Referrer-Policy": "no-referrer", "Cross-Origin-Opener-Policy": "same-origin"}
Model = TypeVar("Model", bound=BaseModel)
# Next to Providers (order 84) under Administration; ties sort by id, so Database comes first.
DATABASE_SECTION = Section(id="database", title="Database", capability=Capability.superadmin,
                           script=STATIC_PREFIX + "database.js", mount="TamSections.database", order=84, icon="database")


class DashboardConfig(DTO):
    cookie_secure: Literal["auto", "always", "never"] = "auto"
    trust_proxy: bool = False

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "DashboardConfig":
        env = os.environ if environ is None else environ
        return cls(cookie_secure=env.get("TAM_TEAM_COOKIE_SECURE", "auto").strip().lower(),
                   trust_proxy=env.get("TAM_TEAM_TRUST_PROXY", "").strip().lower() in ("1", "true", "yes"))


def secured(response: Response) -> Response:
    response.headers.update(SECURITY_HEADERS)
    response.headers.setdefault("Cache-Control", "no-store")
    return response


def service_of(request: Request) -> DashboardService:
    return request.app.state.dashboard


def config_of(request: Request) -> DashboardConfig:
    return request.app.state.dashboard_config


def is_https(request: Request) -> bool:
    config = config_of(request)
    if config.cookie_secure != "auto":
        return config.cookie_secure == "always"
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[-1].strip().lower()
    return request.url.scheme == "https" or (config.trust_proxy and forwarded == "https")


def client_ip(request: Request) -> str:
    if config_of(request).trust_proxy and request.headers.get("x-forwarded-for"):
        return request.headers["x-forwarded-for"].split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def session_cookie(request: Request) -> str:
    return request.cookies.get(SECURE_COOKIE) or request.cookies.get(COOKIE) or ""


def session_credential(request: Request) -> Callable[[], Actor]:
    accounts, session_id = service_of(request).accounts, session_cookie(request)

    def credential() -> Actor:
        return accounts.session(session_id, touch=False).actor
    return credential


def error_response(exc: Exception) -> JSONResponse:
    if isinstance(exc, ValidationError):
        message = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors(include_input=False))
        return JSONResponse({"error": message or "Invalid request", "code": "invalid_request"}, status_code=400)
    code = getattr(exc, "code", "invalid_request")
    return JSONResponse({"error": str(exc), "code": code}, status_code=STATUS.get(code, 400))


async def _call(function: Callable, *args):
    if inspect.iscoroutinefunction(function):
        return await function(*args)
    return await asyncio.to_thread(function, *args)


async def parse(request: Request, model: type[Model]) -> Model:
    if request.method in SAFE_METHODS:
        payload: dict = dict(request.query_params)
    else:
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            raise DomainError("Content-Type must be application/json")
        try:
            payload = json.loads(await request.body() or b"{}")
        except json.JSONDecodeError as exc:
            raise DomainError("Malformed JSON body") from exc
        if not isinstance(payload, dict):
            raise DomainError("JSON body must be an object")
    return model.model_validate({**payload, **request.path_params})


async def authenticate(request: Request, capability: Capability = Capability.authenticated) -> Session:
    service = service_of(request)
    session_id = session_cookie(request)
    if not session_id:
        raise Unauthorized("Sign in required")
    session = await asyncio.to_thread(service.accounts.session, session_id)
    if request.method not in SAFE_METHODS and not hmac.compare_digest(
            request.headers.get(CSRF_HEADER, "").encode(), session.csrf.encode()):
        service.metrics.count("csrf_rejected", route=request.url.path)
        raise Forbidden("Missing or invalid CSRF token")
    granted = await asyncio.to_thread(capabilities, service.registry, session.actor)
    if capability not in granted:
        raise Forbidden("This section is not available for your role")
    request.state.session = session
    return session


def session_endpoint(capability: Capability = Capability.authenticated):
    def decorate(handler: Callable[[Request, Actor], Awaitable | object]):
        @wraps(handler)
        async def endpoint(request: Request) -> Response:
            try:
                session = await authenticate(request, capability)
                result = await _call(handler, request, session.actor)
            except (DomainError, ValidationError, ValueError) as exc:
                return secured(error_response(exc))
            return secured(JSONResponse(result.model_dump(mode="json") if isinstance(result, BaseModel) else result))
        return endpoint
    return decorate


def start_session(request: Request, session: Session, payload: dict) -> Response:
    response = secured(JSONResponse(payload))
    secure = is_https(request)
    max_age = service_of(request).accounts.policy.session_max_seconds
    response.set_cookie(SECURE_COOKIE if secure else COOKIE, session.session_id, max_age=max_age, path="/",
                        httponly=True, samesite="strict", secure=secure)
    return response


def login(model: type[BaseModel], method: str):
    async def endpoint(request: Request) -> Response:
        service = service_of(request)
        try:
            payload = await parse(request, model)
            session = await asyncio.to_thread(getattr(service, method), payload, client_ip(request))
            overview = await asyncio.to_thread(service.overview, session)
        except (DomainError, ValidationError, ValueError) as exc:
            return secured(error_response(exc))
        return start_session(request, session, overview)
    return endpoint


@session_endpoint()
async def session_info(request: Request, _actor: Actor):
    return await asyncio.to_thread(service_of(request).overview, request.state.session)


async def logout(request: Request) -> Response:
    try:
        session = await authenticate(request)
    except (DomainError, ValueError) as exc:
        return secured(error_response(exc))
    await asyncio.to_thread(service_of(request).accounts.logout, session.session_id)
    response = secured(JSONResponse({"signed_out": True}))
    for name in (COOKIE, SECURE_COOKIE):
        response.delete_cookie(name, path="/", secure=name == SECURE_COOKIE, httponly=True, samesite="strict")
    return response


@session_endpoint()
async def memory(request: Request, _actor: Actor):
    call = await parse(request, MemoryCall)
    return await service_of(request).call_memory(session_credential(request), call)


@session_endpoint()
async def change_password(request: Request, _actor: Actor):
    change = await parse(request, PasswordChange)
    return await asyncio.to_thread(service_of(request).change_password, request.state.session, change,
                                   client_ip(request))


async def index(_request: Request) -> Response:
    return secured(FileResponse(STATIC_DIR / "index.html", media_type="text/html; charset=utf-8"))


async def static(request: Request) -> Response:
    name = request.path_params["name"]
    match = STATIC_NAME.fullmatch(name)
    if match is None or not (STATIC_DIR / name).is_file():
        return secured(JSONResponse({"error": "Not found"}, status_code=404))
    response = FileResponse(STATIC_DIR / name, media_type=CONTENT_TYPES[match.group(1)])
    response.headers["Cache-Control"] = (f"public, max-age={ASSET_MAX_AGE_SECONDS}" if match.group(1) in IMMUTABLE_TYPES
                                         else "no-cache")
    return secured(response)


def route(path: str, model: type[BaseModel] | None, method: str, capability: Capability,
          verb: str = "POST") -> Route:
    async def handler(request: Request, actor: Actor):
        args = [actor] if model is None else [actor, await parse(request, model)]
        target = service_of(request)
        for part in method.split("."):
            target = getattr(target, part)
        return await _call(target, *args)
    handler.__name__ = method.replace(".", "_")
    return Route(API_PREFIX + path, session_endpoint(capability)(handler), methods=[verb])


async def to_index(_request: Request) -> Response:
    return RedirectResponse("/dashboard/", status_code=308)


def routes() -> list[Route]:
    auth, people, company, admin = (Capability.authenticated, Capability.team_people, Capability.company,
                                    Capability.superadmin)
    return [
        Route("/dashboard", to_index),
        Route("/dashboard/", index),
        Route("/dashboard/static/{name:path}", static),
        Route("/dashboard/api/login", login(PasswordLogin, "login_password"), methods=["POST"]),
        Route("/dashboard/api/login/token", login(TokenLogin, "login_token"), methods=["POST"]),
        Route("/dashboard/api/invite/redeem", login(InviteRedeem, "redeem_invite"), methods=["POST"]),
        Route("/dashboard/api/logout", logout, methods=["POST"]),
        Route("/dashboard/api/session", session_info),
        Route("/dashboard/api/memory", memory, methods=["POST"]),
        Route("/dashboard/api/password", change_password, methods=["POST"]),
        route("/tokens", None, "my_tokens", auth, "GET"),
        route("/tokens", TokenCreate, "create_token", auth),
        route("/tokens/revoke", TokenRef, "revoke_my_token", auth),
        route("/teams/{team_id}/people", TeamRef, "team_people", people, "GET"),
        route("/company", None, "company", company, "GET"),
        route("/overview/me", None, "insights.me", auth, "GET"),
        route("/overview/team/{team_id}", TeamRef, "insights.team", people, "GET"),
        route("/overview/company", None, "insights.company", company, "GET"),
        route("/overview/system", None, "insights.system", admin, "GET"),
        route("/admin/users", None, "users", admin, "GET"),
        route("/admin/users", UserCreate, "create_user", admin),
        route("/admin/users/{user_id}/invite", UserRef, "issue_invite", admin),
        route("/admin/users/{user_id}/active", UserActive, "set_active", admin),
        route("/admin/users/{user_id}/role", OrgRoleChange, "set_org_role", admin),
        route("/admin/teams", None, "teams", admin, "GET"),
        route("/admin/teams", TeamCreate, "create_team", admin),
        route("/admin/teams/{team_id}/rename", TeamRename, "rename_team", admin),
        route("/admin/teams/{team_id}/delete", TeamRef, "delete_team", admin),
        route("/admin/membership", MembershipChange, "change_membership", admin),
        route("/admin/tokens", TokenFilter, "all_tokens", admin, "GET"),
        route("/admin/tokens/revoke", TokenRef, "admin_revoke_token", admin),
        route("/admin/audit", AuditPage, "audit", admin, "GET"),
        route("/admin/settings", None, "settings_view", admin, "GET"),
        route("/admin/settings", SettingsChange, "update_settings", admin),
        route("/admin/settings/test", ProviderTest, "test_provider", admin),
        route("/admin/backup", None, "backup_info", admin, "GET"),
        route("/admin/metrics", None, "metrics_view", admin, "GET"),
        route("/admin/organization", None, "organization", admin, "GET"),
        route("/admin/organization", OrganizationUpdate, "update_organization", admin),
        route("/admin/setup/finish", None, "finish_setup", admin),
        *database_routes(),
    ]


def register_database_section() -> None:
    if not any(section.id == DATABASE_SECTION.id for section in SECTIONS):
        register(DATABASE_SECTION)


def database_routes() -> list[Route]:
    admin = Capability.superadmin

    def at(api: str) -> str:
        return api.removeprefix(API_PREFIX)
    return [
        route(at(DATABASE_API), None, "database_view", admin, "GET"),
        route(at(DATABASE_TEST_API), DatabaseTestRequest, "test_database", admin),
        route(at(DATABASE_PLAN_API), DatabasePlanRequest, "plan_migration", admin),
        route(at(DATABASE_MIGRATE_API), MigrationStartRequest, "start_migration", admin),
        route(at(DATABASE_MIGRATION_API), None, "migration_progress", admin, "GET"),
        route(at(DATABASE_MIGRATION_CANCEL_API), None, "cancel_migration", admin),
        route(at(DATABASE_REPOINT_API), DatabaseRepointRequest, "repoint_database", admin),
        route(at(DATABASE_ROLLBACK_API), DatabaseRollbackRequest, "rollback_database", admin),
    ]

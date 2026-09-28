import asyncio
import json
from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from team_memory.contracts import Actor, DomainError
from team_memory.dashboard import secured, session_credential, session_endpoint
from team_memory.learning.contracts import TeamRequest
from team_memory.learning.sources import Caller
from team_memory.learning.tools import LEARNING_TOOLS
from team_memory.sections import SECTIONS, Capability, Section, register

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_FILES = {"learning.js": "text/javascript; charset=utf-8", "learning.css": "text/css; charset=utf-8"}
STATIC_PREFIX = "/learning/static/"
API_PREFIX = "/learning/api/"
TOOL_PREFIX = "onboarding_"
SECTION = Section(id="learning", title="Onboarding", capability=Capability.authenticated,
                  script=STATIC_PREFIX + "learning.js", stylesheet=STATIC_PREFIX + "learning.css",
                  mount="TamLearning.mount", order=15, group="personal", icon="book")


def register_section() -> None:
    if not any(section.id == SECTION.id for section in SECTIONS):
        register(SECTION)


def tool_name(action: str) -> str:
    name = TOOL_PREFIX + action
    if name not in LEARNING_TOOLS:
        raise DomainError("Unknown onboarding action")
    return name


async def read_arguments(request: Request) -> dict:
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise DomainError("Content-Type must be application/json")
    try:
        payload = json.loads(await request.body() or b"{}")
    except json.JSONDecodeError as exc:
        raise DomainError("Malformed JSON body") from exc
    if not isinstance(payload, dict):
        raise DomainError("JSON body must be an object")
    return payload


def routes(service) -> list[Route]:
    """Dashboard-session API for the Onboarding section; MCP clients use the same tools via /mcp and /api/call."""

    async def asset(request: Request) -> Response:
        name = request.path_params["name"]
        if name not in STATIC_FILES:
            return secured(JSONResponse({"error": "Not found"}, status_code=404))
        response = FileResponse(STATIC_DIR / name, media_type=STATIC_FILES[name])
        response.headers["Cache-Control"] = "no-cache"
        return secured(response)

    @session_endpoint(Capability.authenticated)
    async def invoke(request: Request, _actor: Actor):
        name = tool_name(request.path_params["action"])
        return await service.call(session_credential(request), name, await read_arguments(request))

    @session_endpoint(Capability.team_people)
    async def team_kpis(request: Request, actor: Actor):
        team = TeamRequest(team_id=request.path_params["team_id"])
        return await asyncio.to_thread(service.learning.team_kpis, Caller(actor, session_credential(request)), team)

    @session_endpoint(Capability.company)
    async def company_kpis(request: Request, actor: Actor):
        return await asyncio.to_thread(service.learning.company_kpis, Caller(actor, session_credential(request)))

    return [Route(STATIC_PREFIX + "{name}", asset),
            Route(API_PREFIX + "kpis/team/{team_id}", team_kpis),
            Route(API_PREFIX + "kpis/company", company_kpis),
            Route(API_PREFIX + "{action}", invoke, methods=["POST"])]

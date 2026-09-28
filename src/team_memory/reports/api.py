import asyncio
from pathlib import Path

from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from team_memory.contracts import Actor, DomainError
from team_memory.dashboard import (
    authenticate,
    error_response,
    parse,
    secured,
    session_endpoint,
)
from team_memory.reports.contracts import TeamReportRequest
from team_memory.sections import SECTIONS, Capability, Section, register

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_FILES = {"reports.js": "text/javascript; charset=utf-8", "reports.css": "text/css; charset=utf-8"}
STATIC_PREFIX = "/reports/static/"
API_PREFIX = "/reports/api/"
SECTION = Section(id="reports", title="Reports", capability=Capability.authenticated,
                  script=STATIC_PREFIX + "reports.js", stylesheet=STATIC_PREFIX + "reports.css",
                  mount="TamReports.mount", order=12, group="personal", icon="list")


def register_section() -> None:
    if not any(section.id == SECTION.id for section in SECTIONS):
        register(SECTION)


def routes(service) -> list[Route]:
    """Dashboard-session API for the Reports section; MCP clients call the memory_report tool instead."""
    reports = service.reports

    async def asset(request: Request) -> Response:
        name = request.path_params["name"]
        if name not in STATIC_FILES:
            return secured(JSONResponse({"error": "Not found"}, status_code=404))
        response = FileResponse(STATIC_DIR / name, media_type=STATIC_FILES[name])
        response.headers["Cache-Control"] = "no-cache"
        return secured(response)

    @session_endpoint(Capability.authenticated)
    async def options(_request: Request, actor: Actor):
        return await asyncio.to_thread(reports.options, actor)

    @session_endpoint(Capability.authenticated)
    async def report(request: Request, actor: Actor):
        body = await parse(request, TeamReportRequest)
        return await asyncio.to_thread(reports.call, actor, body.model_copy(update={"format": "json"}))

    async def download(request: Request) -> Response:
        try:
            session = await authenticate(request)
            body = await parse(request, TeamReportRequest)
            name, text = await asyncio.to_thread(reports.markdown, session.actor, body)
        except (DomainError, ValidationError, ValueError) as exc:
            return secured(error_response(exc))
        return secured(Response(text, media_type="text/markdown; charset=utf-8",
                                headers={"Content-Disposition": f'attachment; filename="{name}"'}))

    return [Route(STATIC_PREFIX + "{name}", asset),
            Route(API_PREFIX + "options", options),
            Route(API_PREFIX + "report", report),
            Route(API_PREFIX + "report.md", download)]

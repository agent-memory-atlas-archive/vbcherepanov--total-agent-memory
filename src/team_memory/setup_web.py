import asyncio

from pydantic import BaseModel, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from team_memory.contracts import DomainError, SetupComplete, SetupVerify
from team_memory.dashboard import (
    client_ip,
    error_response,
    parse,
    secured,
    service_of,
    start_session,
)
from team_memory.database_contracts import (
    SETUP_DATABASE_API,
    SETUP_DATABASE_TEST_API,
    SetupDatabaseRequest,
    SetupDatabaseTestRequest,
)
from team_memory.setup import SetupService
from team_memory.setup_database import SetupDatabaseService


def setup_of(request: Request) -> SetupService:
    return request.app.state.setup


def setup_database_of(request: Request) -> SetupDatabaseService:
    return request.app.state.setup_database


async def status(request: Request) -> Response:
    try:
        result = await asyncio.to_thread(setup_of(request).status)
        database = await asyncio.to_thread(setup_database_of(request).current)
    except DomainError as exc:
        return secured(error_response(exc))
    return secured(JSONResponse({**result.model_dump(mode="json"),
                                 "database": database.model_dump(mode="json") if database else None}))


async def verify(request: Request) -> Response:
    try:
        payload = await parse(request, SetupVerify)
        await asyncio.to_thread(setup_of(request).verify, payload.token, client_ip(request))
    except (DomainError, ValidationError, ValueError) as exc:
        return secured(error_response(exc))
    return secured(JSONResponse({"valid": True}))


async def complete(request: Request) -> Response:
    try:
        payload = await parse(request, SetupComplete)
        session = await asyncio.to_thread(setup_of(request).complete, payload, client_ip(request))
        overview = await asyncio.to_thread(service_of(request).overview, session)
    except (DomainError, ValidationError, ValueError) as exc:
        return secured(error_response(exc))
    return start_session(request, session, overview)


def database_step(model: type[BaseModel], method: str):
    async def endpoint(request: Request) -> Response:
        try:
            payload = await parse(request, model)
            result = await asyncio.to_thread(getattr(setup_database_of(request), method), payload, client_ip(request))
        except (DomainError, ValidationError, ValueError) as exc:
            return secured(error_response(exc))
        return secured(JSONResponse(result.model_dump(mode="json")))
    return endpoint


def routes() -> list[Route]:
    return [Route("/dashboard/api/setup", status),
            Route("/dashboard/api/setup/verify", verify, methods=["POST"]),
            Route("/dashboard/api/setup/complete", complete, methods=["POST"]),
            Route(SETUP_DATABASE_TEST_API, database_step(SetupDatabaseTestRequest, "test"), methods=["POST"]),
            Route(SETUP_DATABASE_API, database_step(SetupDatabaseRequest, "choose"), methods=["POST"])]

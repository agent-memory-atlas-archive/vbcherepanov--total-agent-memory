"""Starlette application: GET /health, POST /add, POST /search — nothing else."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from contextlib import asynccontextmanager

from pydantic import BaseModel, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from aml_adapter.config import AdapterConfig
from aml_adapter.contracts import AddRequest, SearchRequest
from aml_adapter.errors import (
    AdapterError,
    Busy,
    ContractError,
    MalformedBody,
    PayloadTooLarge,
    Unauthorized,
)
from aml_adapter.service import AmlService

LOGGER = logging.getLogger("aml_adapter.app")
PROTECTED_PATHS = frozenset(("/add", "/search"))
AUTH_SCHEMES = ("bearer", "token")
MAX_VALIDATION_ERRORS = 5


def presented_keys(headers: dict[bytes, bytes]) -> list[str]:
    """Credentials in any of the accepted styles: Bearer, Token, X-Api-Key (and a bare Token header)."""
    keys = []
    authorization = headers.get(b"authorization", b"").decode("latin-1").strip()
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() in AUTH_SCHEMES and value.strip():
        keys.append(value.strip())
    for name in (b"x-api-key", b"token"):
        value = headers.get(name, b"").decode("latin-1").strip()
        if value:
            keys.append(value)
    return keys


def authorised(headers: dict[bytes, bytes], accepted: tuple[str, ...]) -> bool:
    return any(hmac.compare_digest(given.encode(), key.encode())
               for given in presented_keys(headers) for key in accepted)


def error_response(exc: AdapterError, retry_after: int) -> JSONResponse:
    headers = {"Retry-After": str(retry_after)} if isinstance(exc, Busy) else None
    return JSONResponse({"error": str(exc) or exc.code, "code": exc.code}, status_code=exc.status, headers=headers)


class Guard:
    """Auth, in-flight limit and body-size limit for the protected endpoints."""

    def __init__(self, app, config: AdapterConfig):
        self.app, self.config = app, config
        self.inflight = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] not in PROTECTED_PATHS:
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        if not self.config.auth_disabled and not authorised(headers, self.config.api_keys):
            await self._reject(Unauthorized("missing or invalid API key"), scope, receive, send)
            return
        if self.inflight >= self.config.max_inflight:
            await self._reject(Busy("too many requests in flight"), scope, receive, send)
            return
        self.inflight += 1
        try:
            declared = headers.get(b"content-length")
            if declared is not None and declared.isdigit() and int(declared) > self.config.max_body_bytes:
                await self._reject(PayloadTooLarge("request body exceeds the limit"), scope, receive, send)
                return
            messages, size = [], 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                size += len(message.get("body", b""))
                if size > self.config.max_body_bytes:
                    await self._reject(PayloadTooLarge("request body exceeds the limit"), scope, receive, send)
                    return
                messages.append(message)
                if not message.get("more_body", False):
                    break

            async def replay():
                return messages.pop(0) if messages else await receive()

            async def no_store(message):
                if message["type"] == "http.response.start":
                    message = {**message, "headers": [*message.get("headers", []), (b"cache-control", b"no-store")]}
                await send(message)

            await self.app(scope, replay, no_store)
        finally:
            self.inflight -= 1

    async def _reject(self, exc: AdapterError, scope, receive, send):
        await error_response(exc, self.config.retry_after_seconds)(scope, receive, send)


async def _parse(request: Request, model: type[BaseModel]):
    try:
        payload = json.loads(await request.body())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MalformedBody("request body is not valid JSON") from exc
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        problems = [f"{'.'.join(str(p) for p in error['loc'])}: {error['msg']}"
                    for error in exc.errors(include_input=False, include_url=False)[:MAX_VALIDATION_ERRORS]]
        raise ContractError("; ".join(problems)) from exc


def create_app(config: AdapterConfig, service: AmlService, version: str) -> Guard:
    async def health(_request: Request):
        return JSONResponse({"status": "ok", "service": "total-agent-memory-aml", "version": version})

    async def add(request: Request):
        response = await service.add(await _parse(request, AddRequest))
        return JSONResponse(response.model_dump())

    async def search(request: Request):
        response = await service.search(await _parse(request, SearchRequest))
        return JSONResponse(response.model_dump(exclude_none=True))

    async def adapter_error(_request: Request, exc: AdapterError):
        return error_response(exc, config.retry_after_seconds)

    @asynccontextmanager
    async def lifespan(_app):
        task = asyncio.create_task(_maintenance(config, service))
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.to_thread(service.pool.close)
            service.metrics.write_textfile(config.data_dir / "metrics.prom")
            service.registry.close()

    app = Starlette(
        routes=[Route("/health", health, methods=["GET"]), Route("/add", add, methods=["POST"]),
                Route("/search", search, methods=["POST"])],
        exception_handlers={AdapterError: adapter_error},
        lifespan=lifespan,
    )
    return Guard(app, config)


async def _maintenance(config: AdapterConfig, service: AmlService) -> None:
    metrics_path = config.data_dir / "metrics.prom"
    next_purge = time.monotonic()
    while True:
        try:
            await asyncio.to_thread(service.metrics.write_textfile, metrics_path)
            if time.monotonic() >= next_purge:
                deleted = await asyncio.to_thread(service.purge_expired)
                next_purge = time.monotonic() + config.purge_interval_seconds
                LOGGER.info(json.dumps({"event": "aml_purge_cycle", "deleted_users": len(deleted)}))
        except Exception as exc:
            LOGGER.exception(json.dumps({"event": "aml_maintenance_failed", "error_type": type(exc).__name__}))
        await asyncio.sleep(min(config.metrics_interval_seconds, config.purge_interval_seconds))

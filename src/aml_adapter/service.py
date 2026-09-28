"""Add / Search / purge orchestration between the HTTP layer and the worker pool."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import contextmanager

from aml_adapter.config import AdapterConfig
from aml_adapter.contracts import (
    AddRequest,
    AddResponse,
    SearchHit,
    SearchRequest,
    SearchResponse,
    content_text,
)
from aml_adapter.errors import AdapterError, Busy
from aml_adapter.metrics import Metrics
from aml_adapter.pool import WorkerPool
from aml_adapter.registry import Registry, UserRecord
from aml_adapter.runtime import RuntimeSettings

LOGGER = logging.getLogger("aml_adapter.service")
ID_PREFIX_LENGTH = 12


def settings_factory(config: AdapterConfig):
    def settings_for(ns: str) -> RuntimeSettings:
        return RuntimeSettings(
            fragment_max_chars=config.fragment_max_chars, content_format=config.content_format,
            query_include_options=config.query_include_options, max_top_k=config.max_top_k,
            embed_concurrency=config.embed_concurrency, require_embed_model=config.require_embed_model,
            id_prefix=ns[:ID_PREFIX_LENGTH])
    return settings_for


class AmlService:
    def __init__(self, config: AdapterConfig, registry: Registry, pool: WorkerPool, metrics: Metrics):
        self.config, self.registry, self.pool, self.metrics = config, registry, pool, metrics

    async def add(self, request: AddRequest) -> AddResponse:
        with self._observe("add") as fields:
            for message in request.messages:
                content_text(message.content)
            ns = await asyncio.to_thread(self.registry.record_write, request.user_id)
            fields["user_ns"] = ns
            result = await asyncio.to_thread(self.pool.invoke, ns, {"op": "add", "request": request.model_dump()})
            fields.update(fragments=result["fragments"], replayed=result["replayed"])
            if not result["replayed"]:
                self.metrics.count_items("fragments_stored", result["fragments"])
        return AddResponse(success=True, request_id=request.request_id, user_id=request.user_id,
                           session_id=request.session_id)

    async def search(self, request: SearchRequest) -> SearchResponse:
        with self._observe("search") as fields:
            content_text(request.query)
            ns = await asyncio.to_thread(self.registry.lookup, request.user_id)
            fields["user_ns"] = ns
            hits = [] if ns is None else await asyncio.to_thread(
                self.pool.invoke, ns, {"op": "search", "request": request.model_dump()})
            fields["results"] = len(hits)
            self.metrics.count_items("search_results_returned", len(hits))
        return SearchResponse(data=[SearchHit(**hit) for hit in hits])

    def purge_expired(self, now: float | None = None) -> list[dict]:
        return self._purge(self.registry.expired(self.config.retention_days, now), "retention")

    def purge_all(self) -> list[dict]:
        return self._purge(self.registry.users(), "manual")

    def _purge(self, users: list[UserRecord], reason: str) -> list[dict]:
        deleted = []
        for user in users:
            try:
                with self.pool.exclusive(user.ns):
                    entry = self.registry.delete(user, reason)
            except Busy:
                LOGGER.info(json.dumps({"event": "aml_purge_deferred", "user_ns": user.ns}))
                continue
            if entry is not None:
                deleted.append(entry)
        return deleted

    @contextmanager
    def _observe(self, operation: str):
        started = time.monotonic()
        fields: dict = {}
        status = "ok"
        try:
            yield fields
        except AdapterError as exc:
            status = exc.code
            raise
        except Exception:
            status = "error"
            raise
        finally:
            elapsed = time.monotonic() - started
            self.metrics.observe(operation, status, elapsed)
            LOGGER.info(json.dumps({"event": "aml_request", "operation": operation, "status": status,
                                    "duration_ms": round(elapsed * 1000, 1), **fields}))

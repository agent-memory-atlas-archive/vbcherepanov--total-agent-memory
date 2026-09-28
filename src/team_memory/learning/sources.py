import asyncio
import uuid
from collections.abc import Callable
from typing import Protocol

from pydantic import JsonValue

from team_memory.contracts import (
    Actor,
    Conflict,
    Save,
    Scope,
    ScopeKind,
    Work,
)
from team_memory.registry import Registry
from team_memory.worker import WorkerPool

EXPORT_PAGE = 50
MAX_SUPERSEDE_HOPS = 20
REMOVED_STATUSES = frozenset(("deleted", "purged"))
NOTE_NAMESPACE = uuid.UUID("6f1d8f0e-4c1b-4a55-9a61-3d7c2b0e9a10")


Credential = str | Callable[[], Actor]


class Caller:
    """Authenticated caller with the credential WorkerPool.invoke re-validates: a Bearer token or,
    for dashboard sessions, a callable that re-reads the session."""

    def __init__(self, actor: Actor, credential: Credential):
        self.actor, self.credential = actor, credential


class MemorySource(Protocol):
    async def get(self, caller: Caller, team_id: str, record_id: int) -> dict | None: ...

    async def export(self, caller: Caller, team_id: str, after: int, limit: int) -> list[dict]: ...

    async def save_personal(self, caller: Caller, content: str, tags: list[str], request_id: uuid.UUID) -> dict: ...


class PoolMemorySource:
    """Reads team workspaces and writes personal notes through the authorized worker pool."""

    def __init__(self, registry: Registry, pool: WorkerPool):
        self.registry, self.pool = registry, pool

    async def _invoke(self, caller: Caller, scope: Scope, operation: str, arguments: dict[str, JsonValue], write: bool):
        workspace = self.registry.authorize(caller.actor, scope, write)
        work = Work(actor=caller.actor, workspace=workspace, operation=operation, arguments=arguments)
        return await asyncio.to_thread(self.pool.invoke, work, caller.credential)

    async def get(self, caller: Caller, team_id: str, record_id: int) -> dict | None:
        try:
            return await self._invoke(caller, Scope(kind=ScopeKind.team, team_id=team_id), "memory_get",
                                      {"id": record_id}, False)
        except Conflict:
            return None

    async def export(self, caller: Caller, team_id: str, after: int, limit: int) -> list[dict]:
        return await self._invoke(caller, Scope(kind=ScopeKind.team, team_id=team_id), "memory_export",
                                  {"after": after, "limit": limit}, False)

    async def save_personal(self, caller: Caller, content: str, tags: list[str], request_id: uuid.UUID) -> dict:
        request = Save(request_id=request_id, content=content, type="fact", project="onboarding", tags=tags)
        return await self._invoke(caller, Scope(), "memory_save",
                                  request.model_dump(mode="json", exclude={"scope"}), True)


async def resolve(source: MemorySource, caller: Caller, team_id: str, record_id: int) -> dict | None:
    """Follow supersession to the current active version of a record; None when missing or deleted."""
    current = record_id
    for _ in range(MAX_SUPERSEDE_HOPS):
        record = await source.get(caller, team_id, current)
        if record is None or record.get("status") in REMOVED_STATUSES:
            return None
        successor = record.get("superseded_by")
        if record.get("status") == "active" or not successor:
            return record
        current = int(successor)
    return None

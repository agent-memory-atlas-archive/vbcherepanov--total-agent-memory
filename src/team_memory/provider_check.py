import json
import logging
import time
from collections.abc import Mapping
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

import httpx

import config
from team_memory.contracts import DTO

LOGGER = logging.getLogger(__name__)
CHECK_TIMEOUT_SECONDS = 8
ANTHROPIC_VERSION = "2023-06-01"


class Endpoint(DTO):
    target: Literal["llm", "embed"]
    provider: str
    base: str
    key: str | None = None
    model: str | None = None


class CheckResult(DTO):
    target: str
    provider: str
    ok: bool
    detail: str
    duration_seconds: float = 0.0


def resolve_llm(env: Mapping[str, str]) -> Endpoint:
    provider = (env.get("MEMORY_LLM_PROVIDER") or "ollama").strip().lower()
    if provider not in config._SUPPORTED_LLM_PROVIDERS:
        provider = "ollama"
    if provider == "auto":
        provider = "openai" if env.get("OPENAI_API_KEY") else "anthropic" if env.get("ANTHROPIC_API_KEY") else "ollama"
    base = env.get("MEMORY_LLM_API_BASE")
    if not base:
        base = (env.get("OLLAMA_URL") or config._DEFAULT_LLM_API_BASE_BY_PROVIDER["ollama"]) if provider == "ollama" \
            else config._DEFAULT_LLM_API_BASE_BY_PROVIDER.get(provider, "")
    key = env.get("MEMORY_LLM_API_KEY") or next(
        (env[name] for name in config._LLM_KEY_ENV_BY_PROVIDER.get(provider, ()) if env.get(name)), None)
    return Endpoint(target="llm", provider=provider, base=base.rstrip("/"), key=key,
                    model=env.get("MEMORY_LLM_MODEL") or None)


def resolve_embed(env: Mapping[str, str]) -> Endpoint:
    provider = (env.get("MEMORY_EMBED_PROVIDER") or "fastembed").strip().lower()
    if provider not in config._SUPPORTED_EMBED_PROVIDERS:
        provider = "fastembed"
    base = env.get("MEMORY_EMBED_API_BASE") or config._DEFAULT_EMBED_API_BASE_BY_PROVIDER.get(provider, "")
    key = env.get("MEMORY_EMBED_API_KEY") or next(
        (env[name] for name in config._EMBED_KEY_ENV_BY_PROVIDER.get(provider, ()) if env.get(name)), None)
    return Endpoint(target="embed", provider=provider, base=base.rstrip("/"), key=key,
                    model=env.get("MEMORY_EMBED_MODEL") or None)


def resolve(target: str, env: Mapping[str, str], provider: str | None = None) -> Endpoint:
    if provider is None:
        return resolve_llm(env) if target == "llm" else resolve_embed(env)
    key = "MEMORY_LLM_PROVIDER" if target == "llm" else "MEMORY_EMBED_PROVIDER"
    return resolve(target, {**env, key: provider})


def missing(endpoint: Endpoint) -> str | None:
    if endpoint.provider == "fastembed":
        return None
    if not endpoint.base:
        return "No API base URL configured"
    if endpoint.provider in ("openai", "anthropic", "cohere", "dashscope") and not endpoint.key:
        return "No API key configured"
    if endpoint.provider == "openai-compatible" and not endpoint.model:
        return "No model configured"
    return None


def probe_request(endpoint: Endpoint) -> tuple[str, dict[str, str]]:
    if endpoint.provider == "ollama":
        return endpoint.base + "/api/tags", {}
    if endpoint.provider == "anthropic":
        return endpoint.base + "/models", {"x-api-key": endpoint.key or "", "anthropic-version": ANTHROPIC_VERSION}
    if endpoint.provider == "cohere":
        parts = urlsplit(endpoint.base)
        return urlunsplit((parts.scheme, parts.netloc, "/v1/models", "", "")), \
            {"Authorization": "Bearer " + (endpoint.key or "")}
    headers = {"Authorization": "Bearer " + endpoint.key} if endpoint.key else {}
    return endpoint.base + "/models", headers


def check(endpoint: Endpoint, transport: httpx.BaseTransport | None = None) -> CheckResult:
    started = time.monotonic()
    result = _check(endpoint, transport)
    result = result.model_copy(update={"duration_seconds": round(time.monotonic() - started, 3)})
    LOGGER.info(json.dumps({"event": "provider_check", "target": result.target, "provider": result.provider,
                            "ok": result.ok, "detail": result.detail, "duration_seconds": result.duration_seconds}))
    return result


def _check(endpoint: Endpoint, transport: httpx.BaseTransport | None) -> CheckResult:
    base = {"target": endpoint.target, "provider": endpoint.provider}
    if endpoint.provider == "fastembed":
        return CheckResult(**base, ok=True, detail="Local provider; no network connection is required")
    problem = missing(endpoint)
    if problem is not None:
        return CheckResult(**base, ok=False, detail=problem)
    url, headers = probe_request(endpoint)
    try:
        with httpx.Client(timeout=CHECK_TIMEOUT_SECONDS, follow_redirects=False, transport=transport) as client:
            response = client.get(url, headers=headers)
    except httpx.TimeoutException:
        return CheckResult(**base, ok=False, detail="Timed out")
    except httpx.HTTPError:
        return CheckResult(**base, ok=False, detail="Connection failed")
    status = response.status_code
    if 200 <= status < 300:
        return CheckResult(**base, ok=True, detail=f"HTTP {status}")
    reason = {401: "authentication failed", 403: "access denied", 404: "endpoint not found",
              429: "rate limited"}.get(status, "unexpected response")
    return CheckResult(**base, ok=False, detail=f"HTTP {status} ({reason})")

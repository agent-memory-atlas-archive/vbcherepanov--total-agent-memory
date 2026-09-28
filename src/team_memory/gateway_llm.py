"""LLM providers for gateway-side features (onboarding grading and drafts, report summaries).

Workers receive dashboard provider settings as their process environment. The gateway serves many requests from one
process, so it never writes them into os.environ: every use resolves the settings again with the same precedence
(web > env > default) and builds the provider from that explicit configuration. A saved change applies to the next
request without a restart.
"""
import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable, Mapping

import httpx
from pydantic import SecretStr

import config
from llm_provider import AnthropicProvider, OllamaProvider, OpenAIProvider
from memory_core.telemetry import counters
from team_memory import provider_check
from team_memory.contracts import DTO
from team_memory.settings import SettingsStore

LOGGER = logging.getLogger(__name__)
DEFAULT_MODE = "auto"
DISABLED_MODES = frozenset(("false",))
FORCED_MODES = frozenset(("true", "force"))
PROBE_TTL_SECONDS = 60.0
OLLAMA_PROBE_TIMEOUT_SECONDS = 2.0

Provider = OllamaProvider | OpenAIProvider | AnthropicProvider


class GatewayLLMConfig(DTO):
    mode: str
    provider: str
    base: str
    model: str | None = None
    api_key: SecretStr | None = None
    problem: str | None = None

    def fingerprint(self) -> str:
        key = self.api_key.get_secret_value() if self.api_key else ""
        material = "\0".join((self.mode, self.provider, self.base, self.model or "", key))
        return hashlib.sha256(material.encode()).hexdigest()

    def log_fields(self) -> dict[str, str | bool | None]:
        return {"mode": self.mode, "provider": self.provider, "model": self.model, "key_set": self.api_key is not None,
                "problem": self.problem}


def resolve(env: Mapping[str, str]) -> GatewayLLMConfig:
    """Effective MEMORY_LLM_* settings, resolved the way config/llm_provider resolve a worker's environment."""
    endpoint = provider_check.resolve_llm(env)
    return GatewayLLMConfig(
        mode=(env.get("MEMORY_LLM_ENABLED") or DEFAULT_MODE).strip().lower(),
        provider=endpoint.provider,
        base=endpoint.base,
        model=endpoint.model or config._DEFAULT_LLM_MODEL_BY_PROVIDER.get(endpoint.provider),
        api_key=SecretStr(endpoint.key) if endpoint.key else None,
        problem=provider_check.missing(endpoint),
    )


def build(settings: GatewayLLMConfig) -> Provider:
    key = settings.api_key.get_secret_value() if settings.api_key else None
    match settings.provider:
        case "ollama":
            return OllamaProvider(api_base=settings.base, model=settings.model)
        case "openai":
            return OpenAIProvider(api_key=key, api_base=settings.base, model=settings.model)
        case "openai-compatible":
            return OpenAIProvider(api_key=key, api_base=settings.base, model=settings.model, require_api_key=False)
        case "anthropic":
            return AnthropicProvider(api_key=key, api_base=settings.base, model=settings.model)
        case _:
            raise ValueError(f"Unsupported LLM provider: {settings.provider}")


class GatewayLLM:
    """LLM factory for learning and reports: returns a ready provider, or None when LLM use is off or unavailable."""

    def __init__(self, settings: SettingsStore, transport: httpx.BaseTransport | None = None,
                 clock: Callable[[], float] = time.monotonic, probe_ttl: float = PROBE_TTL_SECONDS):
        self.settings, self.transport, self.clock, self.probe_ttl = settings, transport, clock, probe_ttl
        self.lock = threading.Lock()
        self.availability: dict[str, tuple[float, bool]] = {}
        self.last_fingerprint: str | None = None

    def current(self) -> GatewayLLMConfig:
        return resolve(self.settings.effective())

    def __call__(self) -> Provider | None:
        settings = self.current()
        self._note(settings)
        if settings.mode in DISABLED_MODES or settings.problem is not None:
            counters.bump("gateway_llm_unavailable")
            return None
        provider = build(settings)
        if settings.mode in FORCED_MODES or self._available(settings, provider):
            counters.bump("gateway_llm_resolved")
            return provider
        counters.bump("gateway_llm_unavailable")
        return None

    def _note(self, settings: GatewayLLMConfig) -> None:
        fingerprint = settings.fingerprint()
        with self.lock:
            if fingerprint == self.last_fingerprint:
                return
            self.last_fingerprint = fingerprint
        LOGGER.info(json.dumps({"event": "gateway_llm_settings", **settings.log_fields()}))

    def _available(self, settings: GatewayLLMConfig, provider: Provider) -> bool:
        fingerprint, now = settings.fingerprint(), self.clock()
        with self.lock:
            cached = self.availability.get(fingerprint)
        if cached is not None and cached[0] > now:
            return cached[1]
        ready = self._ollama_ready(settings) if settings.provider == "ollama" else provider.available()
        with self.lock:
            self.availability[fingerprint] = (now + self.probe_ttl, ready)
        return ready

    def _ollama_ready(self, settings: GatewayLLMConfig) -> bool:
        try:
            with httpx.Client(timeout=OLLAMA_PROBE_TIMEOUT_SECONDS, follow_redirects=False,
                              transport=self.transport) as client:
                response = client.get(settings.base + "/api/tags")
            response.raise_for_status()
            names = [str(model.get("name", "")) for model in response.json().get("models", [])]
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            LOGGER.warning(json.dumps({"event": "gateway_llm_probe_failed", "provider": settings.provider,
                                       "error": type(exc).__name__}))
            return False
        return config.model_matches(settings.model or "", names)

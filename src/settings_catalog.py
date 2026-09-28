"""Settings both dashboards can change: the catalogue, value validation and the Fernet master key.

Kept free of team-server imports so a personal MCP server can load it on every start.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from pydantic import BaseModel, ConfigDict

from paths import exposed_to_others

LOGGER = logging.getLogger(__name__)
MAX_VALUE_CHARS = 4096
MASK_VISIBLE_CHARS = 4
MIN_SECRET_CHARS_FOR_HINT = 12
LLM_PROVIDERS = ("ollama", "openai", "openai-compatible", "anthropic", "auto")
EMBED_PROVIDERS = ("fastembed", "openai", "cohere", "dashscope")


class SettingError(ValueError):
    """A value the catalogue does not accept."""


class SettingSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    group: Literal["llm", "embed", "recall", "storage"]
    label: str
    kind: Literal["choice", "text", "url", "number", "integer", "secret", "path"]
    choices: tuple[str, ...] = ()
    help: str = ""


PROVIDER_SPECS: tuple[SettingSpec, ...] = (
    SettingSpec(key="MEMORY_LLM_ENABLED", group="llm", label="LLM tasks", kind="choice",
                choices=("auto", "true", "false"), help="auto enables LLM work when the provider answers."),
    SettingSpec(key="MEMORY_LLM_PROVIDER", group="llm", label="LLM provider", kind="choice", choices=LLM_PROVIDERS),
    SettingSpec(key="MEMORY_LLM_MODEL", group="llm", label="LLM model", kind="text",
                help="Required for openai-compatible; provider default otherwise."),
    SettingSpec(key="MEMORY_LLM_API_BASE", group="llm", label="LLM API base URL", kind="url",
                help="Overrides the provider default endpoint."),
    SettingSpec(key="OLLAMA_URL", group="llm", label="Ollama URL", kind="url"),
    SettingSpec(key="MEMORY_LLM_TIMEOUT_SEC", group="llm", label="LLM timeout, seconds", kind="number"),
    SettingSpec(key="MEMORY_LLM_API_KEY", group="llm", label="LLM API key (any provider)", kind="secret",
                help="Takes precedence over the provider-specific keys below."),
    SettingSpec(key="OPENAI_API_KEY", group="llm", label="OpenAI API key", kind="secret",
                help="Also used by OpenAI embeddings."),
    SettingSpec(key="ANTHROPIC_API_KEY", group="llm", label="Anthropic API key", kind="secret"),
    SettingSpec(key="MEMORY_EMBED_PROVIDER", group="embed", label="Embedding provider", kind="choice",
                choices=EMBED_PROVIDERS,
                help="Changing provider or model makes stored vectors incompatible until re-embedded."),
    SettingSpec(key="MEMORY_EMBED_MODEL", group="embed", label="Embedding model", kind="text"),
    SettingSpec(key="MEMORY_EMBED_API_BASE", group="embed", label="Embedding API base URL", kind="url"),
    SettingSpec(key="MEMORY_EMBED_API_KEY", group="embed", label="Embedding API key", kind="secret"),
    SettingSpec(key="COHERE_API_KEY", group="embed", label="Cohere API key", kind="secret"),
    SettingSpec(key="DASHSCOPE_API_KEY", group="embed", label="DashScope API key", kind="secret",
                help="Region-bound Alibaba Cloud Model Studio key; match it with the API base URL."),
    SettingSpec(key="MEMORY_EMBED_DIMENSIONS", group="embed", label="Embedding dimensions", kind="number",
                help="text-embedding-v4 accepts 2048, 1536, 1024, 768, 512, 256, 128 or 64."),
)
RECALL_SPECS: tuple[SettingSpec, ...] = (
    SettingSpec(key="MEMORY_RECALL_MAX_RESULT_CHARS", group="recall", label="Characters per search result",
                kind="integer",
                help="Longer records are cut in search answers and marked truncated; memory_get returns them whole. "
                     "Default 6000."),
    SettingSpec(key="MEMORY_FLAG_INSTRUCTIONS", group="recall", label="Flag records that address the agent",
                kind="choice", choices=("true", "false"),
                help='Marks records like "ignore previous instructions" as untrusted_instructions. Default true.'),
)
# Personal installs only: the team server has its own reranker and storage layout.
LOCAL_SPECS: tuple[SettingSpec, ...] = (
    SettingSpec(key="MEMORY_CROSS_RERANK", group="recall", label="Cross-encoder re-ranking", kind="choice",
                choices=("auto", "on", "off"),
                help="Re-reads the top results with an 80 MB model for better order. Applies to new sessions."),
    SettingSpec(key="MEMORY_RAW_LOG_RETENTION_DAYS", group="storage", label="Keep raw call logs, days",
                kind="integer",
                help="raw/<session>.jsonl records every tool call. Older files are removed when a session starts. "
                     "Not set: kept forever."),
    SettingSpec(key="TAM_MODEL_CACHE", group="storage", label="Embedding model cache folder", kind="path",
                help="Default is the system temp folder, which macOS clears; a folder here survives. "
                     "Models download once more after a change."),
)
class ProviderSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target: Literal["llm", "embed"]
    id: str
    label: str
    fields: tuple[str, ...]
    local: bool = False


PROVIDER_KEYS = {"llm": "MEMORY_LLM_PROVIDER", "embed": "MEMORY_EMBED_PROVIDER"}
PROVIDER_DEFAULTS = {"llm": "ollama", "embed": "fastembed"}
COMMON_FIELDS = {"llm": ("MEMORY_LLM_ENABLED", "MEMORY_LLM_TIMEOUT_SEC", "MEMORY_LLM_API_KEY"), "embed": ()}
PROVIDERS: tuple[ProviderSpec, ...] = (
    ProviderSpec(target="llm", id="ollama", label="Ollama", fields=("OLLAMA_URL", "MEMORY_LLM_MODEL"), local=True),
    ProviderSpec(target="llm", id="openai", label="OpenAI",
                 fields=("OPENAI_API_KEY", "MEMORY_LLM_MODEL", "MEMORY_LLM_API_BASE")),
    ProviderSpec(target="llm", id="anthropic", label="Anthropic",
                 fields=("ANTHROPIC_API_KEY", "MEMORY_LLM_MODEL", "MEMORY_LLM_API_BASE")),
    ProviderSpec(target="llm", id="openai-compatible", label="OpenAI-compatible",
                 fields=("MEMORY_LLM_API_BASE", "MEMORY_LLM_MODEL", "MEMORY_LLM_API_KEY")),
    ProviderSpec(target="embed", id="fastembed", label="FastEmbed (local)", fields=("MEMORY_EMBED_MODEL",), local=True),
    ProviderSpec(target="embed", id="openai", label="OpenAI embeddings",
                 fields=("OPENAI_API_KEY", "MEMORY_EMBED_MODEL", "MEMORY_EMBED_API_BASE", "MEMORY_EMBED_API_KEY")),
    ProviderSpec(target="embed", id="cohere", label="Cohere",
                 fields=("COHERE_API_KEY", "MEMORY_EMBED_MODEL", "MEMORY_EMBED_API_KEY")),
    ProviderSpec(target="embed", id="dashscope", label="DashScope (text-embedding-v4)",
                 fields=("DASHSCOPE_API_KEY", "MEMORY_EMBED_MODEL", "MEMORY_EMBED_API_BASE", "MEMORY_EMBED_DIMENSIONS")),
)


TEAM_CATALOG: tuple[SettingSpec, ...] = PROVIDER_SPECS + RECALL_SPECS
LOCAL_CATALOG: tuple[SettingSpec, ...] = PROVIDER_SPECS + RECALL_SPECS + LOCAL_SPECS


def validate_value(spec: SettingSpec, value: str) -> str:
    value = value.strip()
    if not value or len(value) > MAX_VALUE_CHARS or any(ord(ch) < 32 for ch in value):
        raise SettingError(f"{spec.key}: value must be 1–{MAX_VALUE_CHARS} printable characters")
    if spec.kind == "choice" and value not in spec.choices:
        raise SettingError(f"{spec.key}: expected one of {', '.join(spec.choices)}")
    if spec.kind == "integer" and (not value.isdigit() or int(value) <= 0):
        raise SettingError(f"{spec.key}: expected a positive whole number")
    if spec.kind == "number":
        try:
            if float(value) <= 0:
                raise ValueError(value)
        except ValueError as exc:
            raise SettingError(f"{spec.key}: expected a positive number") from exc
    if spec.kind == "url":
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password \
                or parts.query or parts.fragment:
            raise SettingError(f"{spec.key}: expected an http(s) URL without credentials or query")
    if spec.kind == "path" and not Path(value).expanduser().is_absolute():
        raise SettingError(f"{spec.key}: expected an absolute folder path")
    return value


def mask(secret: str) -> str:
    if len(secret) < MIN_SECRET_CHARS_FOR_HINT:
        return "••••"
    return "••••" + secret[-MASK_VISIBLE_CHARS:]


def load_master_key(root: Path, env_name: str, environ: Mapping[str, str] | None = None, *,
                    create: bool = True) -> bytes:
    """The Fernet key: `env_name` from the environment, else <root>/master.key (created 0600 when ``create``).

    FileNotFoundError when the file is missing and ``create`` is False; PermissionError when group
    or others can read it.
    """
    env = os.environ if environ is None else environ
    if env.get(env_name):
        key = env[env_name].encode()
        try:
            Fernet(key)
        except ValueError as exc:
            raise ValueError(f"{env_name} must be a urlsafe base64 Fernet key") from exc
        return key
    path = root / "master.key"
    if not create:
        if path.is_file() and exposed_to_others(path):
            raise PermissionError(f"{path} must not be readable by group or others (chmod 600)")
        return path.read_bytes().strip()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if exposed_to_others(path):
            raise PermissionError(f"{path} must not be readable by group or others (chmod 600)") from None
        return path.read_bytes().strip()
    with os.fdopen(fd, "wb") as destination:
        destination.write(Fernet.generate_key())
    LOGGER.info(json.dumps({"event": "master_key_created", "path": str(path)}))
    return path.read_bytes().strip()

"""Adapter configuration, read once from the environment and validated eagerly."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_MAX_BODY_BYTES = 32 * 1024 * 1024
DEFAULT_MAX_INFLIGHT = 16
DEFAULT_RETRY_AFTER_SECONDS = 5
MAX_RETRY_AFTER_SECONDS = 60  # the platform ignores anything longer
DEFAULT_WORKERS = 4
DEFAULT_OPERATION_TIMEOUT_SECONDS = 28 * 60  # the platform gives a request 30 min
DEFAULT_WORKER_WAIT_SECONDS = 30.0
DEFAULT_RETENTION_DAYS = 14.0
MAX_RETENTION_DAYS = 30.0  # AML: delete within 30 days after the run
DEFAULT_PURGE_INTERVAL_SECONDS = 3600.0
DEFAULT_FRAGMENT_MAX_CHARS = 6000
MIN_FRAGMENT_MAX_CHARS = 200
DEFAULT_MAX_TOP_K = 1000
DEFAULT_EMBED_CONCURRENCY = 4
DEFAULT_METRICS_INTERVAL_SECONDS = 60.0
CONTENT_FORMATS = ("annotated", "raw")
_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off", "")
# Stores that belong to a person's everyday memory; the adapter never writes there.
PROTECTED_DIRS = (Path("~/.tam"), Path("~/.claude-memory"))


class ConfigError(ValueError):
    """Invalid or missing AML_* setting."""


@dataclass(frozen=True)
class AdapterConfig:
    data_dir: Path
    host: str
    port: int
    api_keys: tuple[str, ...]
    auth_disabled: bool
    max_body_bytes: int
    max_inflight: int
    retry_after_seconds: int
    workers: int
    operation_timeout_seconds: float
    worker_wait_seconds: float
    retention_days: float
    purge_interval_seconds: float
    fragment_max_chars: int
    content_format: str
    query_include_options: bool
    max_top_k: int
    embed_concurrency: int
    require_embed_model: str
    metrics_interval_seconds: float

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AdapterConfig:
        env = os.environ if env is None else env
        raw_dir = env.get("AML_DATA_DIR", "").strip()
        if not raw_dir:
            raise ConfigError("AML_DATA_DIR is required (a dedicated directory for AML run data)")
        data_dir = Path(raw_dir).expanduser().resolve()
        for protected in PROTECTED_DIRS:
            root = protected.expanduser().resolve()
            if data_dir == root or root in data_dir.parents:
                raise ConfigError(f"AML_DATA_DIR must not be inside {root}")
        keys = tuple(k.strip() for k in env.get("AML_API_KEYS", "").split(",") if k.strip())
        auth_disabled = _bool(env, "AML_AUTH_DISABLED", False)
        if not keys and not auth_disabled:
            raise ConfigError("AML_API_KEYS is required unless AML_AUTH_DISABLED=true")
        content_format = env.get("AML_CONTENT_FORMAT", "annotated").strip().lower()
        if content_format not in CONTENT_FORMATS:
            raise ConfigError(f"AML_CONTENT_FORMAT must be one of {CONTENT_FORMATS}")
        retention = _float(env, "AML_RETENTION_DAYS", DEFAULT_RETENTION_DAYS)
        if not 0 < retention <= MAX_RETENTION_DAYS:
            raise ConfigError(f"AML_RETENTION_DAYS must be in (0, {MAX_RETENTION_DAYS:g}]")
        retry_after = _int(env, "AML_RETRY_AFTER_SECONDS", DEFAULT_RETRY_AFTER_SECONDS)
        if not 1 <= retry_after <= MAX_RETRY_AFTER_SECONDS:
            raise ConfigError(f"AML_RETRY_AFTER_SECONDS must be in [1, {MAX_RETRY_AFTER_SECONDS}]")
        fragment_max = _int(env, "AML_FRAGMENT_MAX_CHARS", DEFAULT_FRAGMENT_MAX_CHARS)
        if fragment_max < MIN_FRAGMENT_MAX_CHARS:
            raise ConfigError(f"AML_FRAGMENT_MAX_CHARS must be >= {MIN_FRAGMENT_MAX_CHARS}")
        port = _int(env, "AML_PORT", DEFAULT_PORT)
        if not 0 < port < 65536:
            raise ConfigError("AML_PORT must be a TCP port")
        return cls(
            data_dir=data_dir,
            host=env.get("AML_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST,
            port=port,
            api_keys=keys,
            auth_disabled=auth_disabled,
            max_body_bytes=_positive_int(env, "AML_MAX_BODY_BYTES", DEFAULT_MAX_BODY_BYTES),
            max_inflight=_positive_int(env, "AML_MAX_INFLIGHT", DEFAULT_MAX_INFLIGHT),
            retry_after_seconds=retry_after,
            workers=_positive_int(env, "AML_WORKERS", DEFAULT_WORKERS),
            operation_timeout_seconds=_positive_float(
                env, "AML_OPERATION_TIMEOUT_SECONDS", DEFAULT_OPERATION_TIMEOUT_SECONDS),
            worker_wait_seconds=_positive_float(env, "AML_WORKER_WAIT_SECONDS", DEFAULT_WORKER_WAIT_SECONDS),
            retention_days=retention,
            purge_interval_seconds=_positive_float(
                env, "AML_PURGE_INTERVAL_SECONDS", DEFAULT_PURGE_INTERVAL_SECONDS),
            fragment_max_chars=fragment_max,
            content_format=content_format,
            query_include_options=_bool(env, "AML_QUERY_INCLUDE_OPTIONS", False),
            max_top_k=_positive_int(env, "AML_MAX_TOP_K", DEFAULT_MAX_TOP_K),
            embed_concurrency=_positive_int(env, "AML_EMBED_CONCURRENCY", DEFAULT_EMBED_CONCURRENCY),
            require_embed_model=env.get("AML_REQUIRE_EMBED_MODEL", "").strip(),
            metrics_interval_seconds=_positive_float(
                env, "AML_METRICS_INTERVAL_SECONDS", DEFAULT_METRICS_INTERVAL_SECONDS),
        )

    def public_settings(self) -> dict:
        """Settings that shape results, without secrets — used for the freeze hash."""
        data = asdict(self)
        for private in ("api_keys", "data_dir", "host", "port"):
            data.pop(private)
        return data

    def fingerprint(self) -> str:
        blob = json.dumps(self.public_settings(), sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(f"{name} must be a boolean, got {raw!r}")


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    value = _int(env, name, default)
    if value < 1:
        raise ConfigError(f"{name} must be positive")
    return value


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _positive_float(env: Mapping[str, str], name: str, default: float) -> float:
    value = _float(env, name, default)
    if value <= 0:
        raise ConfigError(f"{name} must be positive")
    return value

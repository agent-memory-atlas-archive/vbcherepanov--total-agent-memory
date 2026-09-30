"""A local OpenAI metering proxy with a hard dollar ceiling.

The benchmark harness talks to http://127.0.0.1:<port>/v1 with a random per-run session
token; only this proxy holds the real API key and adds it to upstream requests, so the
key never enters the harness process, its configs or its logs.

Budget guard (stop *before* the ceiling is crossed):

- Every request is priced up front at its worst case: input tokens are bounded by the
  request body size in bytes (a BPE token always covers at least one byte) plus a fixed
  per-request allowance, output tokens by the request's own max_output_tokens /
  max_completion_tokens / max_tokens (a request without one is refused). Reasoning tokens
  are billed as output and are bounded by the same field.
- A request is forwarded only if spent + in-flight reservations + its worst case stays
  within the ceiling. Otherwise the proxy trips: this and every later request gets HTTP
  402 (not retried by the OpenAI SDK) and the launcher stops the harness.
- After the response, the reservation is replaced by the cost computed from the API's
  own usage fields. The guard charges cached input at the full input price (an upper
  bound); the ledger also records the list-price cost with the cached-input discount.
- A 200 response without usage, or a transport failure after the request was sent,
  is charged at the full reservation (the upstream may have billed it).

Only POST /v1/responses and /v1/chat/completions are forwarded, for allow-listed models
with a known price, and never with stream=true.
"""

from __future__ import annotations

import hmac
import json
import os
import stat
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

OFFICIAL_UPSTREAM = "https://api.openai.com/v1"
DEFAULT_KEY_FILE = Path.home() / ".config" / "tam-bench" / "openai.env"
KEY_VARIABLE = "OPENAI_API_KEY"
FORWARDED_PATHS = {"/v1/responses", "/v1/chat/completions"}
MAX_OUTPUT_FIELDS = ("max_output_tokens", "max_completion_tokens", "max_tokens")
PER_REQUEST_INPUT_ALLOWANCE = 256
TOKENS_PER_MILLION = 1_000_000
MAX_BODY_BYTES = 64 * 1024 * 1024


class BudgetError(RuntimeError):
    """Configuration or key-loading error of the guard itself."""


# ---------------------------------------------------------------------------
# Prices and key
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ModelPrice:
    input: float
    cached_input: float
    output: float


def load_prices(path: Path, allowed_models: Iterable[str]) -> dict[str, ModelPrice]:
    table = json.loads(Path(path).read_text(encoding="utf-8"))
    models = table.get("models")
    if not isinstance(models, dict):
        raise BudgetError(f"{path} has no 'models' object")
    prices: dict[str, ModelPrice] = {}
    for model in allowed_models:
        entry = models.get(model)
        if not isinstance(entry, dict):
            raise BudgetError(f"no price for allowed model {model!r} in {path}")
        price = ModelPrice(float(entry["input"]), float(entry["cached_input"]), float(entry["output"]))
        if min(price.input, price.cached_input, price.output) < 0:
            raise BudgetError(f"negative price for {model!r}")
        prices[model] = price
    if not prices:
        raise BudgetError("at least one allowed model is required")
    return prices


def parse_env_file(text: str, variable: str = KEY_VARIABLE) -> str | None:
    """Read `VAR=value` (optionally `export VAR=value`, optionally quoted) from env-file text."""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, sep, value = line.partition("=")
        if not sep or name.strip() != variable:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        return value.strip() or None
    return None


def load_api_key(key_file: Path | None, environ: dict[str, str]) -> tuple[str, str]:
    """Return (key, source description). The key itself is never logged."""
    if key_file is not None and key_file.exists():
        mode = stat.S_IMODE(key_file.stat().st_mode)
        if mode & 0o077:
            raise BudgetError(f"{key_file} is readable by group/others (mode {oct(mode)}); run chmod 600")
        key = parse_env_file(key_file.read_text(encoding="utf-8"))
        if not key:
            raise BudgetError(f"{key_file} has no {KEY_VARIABLE}=... line")
        return key, f"file:{key_file}"
    key = environ.get(KEY_VARIABLE)
    if key:
        return key, f"env:{KEY_VARIABLE}"
    raise BudgetError(f"no API key: {key_file} is missing and {KEY_VARIABLE} is not set")


# ---------------------------------------------------------------------------
# Cost accounting
# ---------------------------------------------------------------------------

def max_output_tokens(body: dict[str, Any]) -> int | None:
    for name in MAX_OUTPUT_FIELDS:
        value = body.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return None


def worst_case_usd(raw_body_len: int, output_cap: int, price: ModelPrice) -> float:
    input_upper = raw_body_len + PER_REQUEST_INPUT_ALLOWANCE
    return (input_upper * price.input + output_cap * price.output) / TOKENS_PER_MILLION


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    reasoning_tokens: int


def parse_usage(payload: Any) -> Usage | None:
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return None

    def number(container: Any, key: str) -> int | None:
        value = container.get(key) if isinstance(container, dict) else None
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    if number(usage, "input_tokens") is not None and number(usage, "output_tokens") is not None:
        return Usage(number(usage, "input_tokens"),
                     number(usage.get("input_tokens_details"), "cached_tokens") or 0,
                     number(usage, "output_tokens"),
                     number(usage.get("output_tokens_details"), "reasoning_tokens") or 0)
    if number(usage, "prompt_tokens") is not None and number(usage, "completion_tokens") is not None:
        return Usage(number(usage, "prompt_tokens"),
                     number(usage.get("prompt_tokens_details"), "cached_tokens") or 0,
                     number(usage, "completion_tokens"),
                     number(usage.get("completion_tokens_details"), "reasoning_tokens") or 0)
    return None


def usage_cost(usage: Usage, price: ModelPrice) -> tuple[float, float]:
    """(guard cost with cached input at full price, list cost with the cached discount)."""
    cached = min(usage.cached_tokens, usage.input_tokens)
    guard = (usage.input_tokens * price.input + usage.output_tokens * price.output) / TOKENS_PER_MILLION
    listed = ((usage.input_tokens - cached) * price.input + cached * price.cached_input
              + usage.output_tokens * price.output) / TOKENS_PER_MILLION
    return guard, listed


@dataclass
class Ledger:
    ceiling_usd: float
    path: Path
    on_trip: Callable[[str], None] | None = None
    spent_usd: float = 0.0
    list_usd: float = 0.0
    reserved_usd: float = 0.0
    requests: int = 0
    refused: int = 0
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    tripped_reason: str | None = None
    per_model: dict[str, dict[str, float]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if self.ceiling_usd <= 0:
            raise BudgetError("ceiling must be > 0")
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def reserve(self, model: str, worst_usd: float) -> tuple[bool, str]:
        with self._lock:
            if self.tripped_reason is not None:
                self.refused += 1
                return False, self.tripped_reason
            if self.spent_usd + self.reserved_usd + worst_usd <= self.ceiling_usd:
                self.reserved_usd += worst_usd
                return True, ""
            self.refused += 1
            self.tripped_reason = (
                f"budget ceiling ${self.ceiling_usd:.2f}: spent ${self.spent_usd:.4f} + in-flight "
                f"${self.reserved_usd:.4f} + next {model} request worst case ${worst_usd:.4f} would exceed it")
            reason = self.tripped_reason
        if self.on_trip is not None:
            self.on_trip(reason)
        return False, reason

    def settle(self, *, model: str, endpoint: str, status: int, reserved_usd: float, usage: Usage | None,
               price: ModelPrice, latency_s: float, note: str = "") -> dict[str, Any]:
        if usage is not None:
            guard_usd, list_usd = usage_cost(usage, price)
        elif note in {"usage_missing", "transport_error_after_send"}:
            guard_usd, list_usd = reserved_usd, reserved_usd
        else:
            guard_usd, list_usd = 0.0, 0.0
        with self._lock:
            self.reserved_usd = max(0.0, self.reserved_usd - reserved_usd)
            self.spent_usd += guard_usd
            self.list_usd += list_usd
            self.requests += 1
            if usage is not None:
                self.input_tokens += usage.input_tokens
                self.cached_tokens += usage.cached_tokens
                self.output_tokens += usage.output_tokens
                self.reasoning_tokens += usage.reasoning_tokens
            model_totals = self.per_model.setdefault(model, {"requests": 0, "usd": 0.0, "list_usd": 0.0,
                                                             "input_tokens": 0, "output_tokens": 0})
            model_totals["requests"] += 1
            model_totals["usd"] += guard_usd
            model_totals["list_usd"] += list_usd
            if usage is not None:
                model_totals["input_tokens"] += usage.input_tokens
                model_totals["output_tokens"] += usage.output_tokens
            entry = {
                "ts": time.time(), "endpoint": endpoint, "model": model, "status": status,
                "latency_s": round(latency_s, 3), "reserved_usd": round(reserved_usd, 6),
                "usd": round(guard_usd, 6), "list_usd": round(list_usd, 6),
                "cum_usd": round(self.spent_usd, 6), "note": note,
                **({"input_tokens": usage.input_tokens, "cached_tokens": usage.cached_tokens,
                    "output_tokens": usage.output_tokens, "reasoning_tokens": usage.reasoning_tokens}
                   if usage is not None else {}),
            }
            if usage is not None and guard_usd > reserved_usd:
                entry["note"] = (note + " reservation_exceeded").strip()
            with self.path.open("a", encoding="utf-8") as sink:
                sink.write(json.dumps(entry) + "\n")
        return entry

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return {
                "ceiling_usd": self.ceiling_usd,
                "spent_usd_guard": round(self.spent_usd, 6),
                "spent_usd_list": round(self.list_usd, 6),
                "in_flight_usd": round(self.reserved_usd, 6),
                "requests": self.requests,
                "refused": self.refused,
                "input_tokens": self.input_tokens,
                "cached_tokens": self.cached_tokens,
                "output_tokens": self.output_tokens,
                "reasoning_tokens": self.reasoning_tokens,
                "per_model": {model: {k: (round(v, 6) if isinstance(v, float) else v) for k, v in totals.items()}
                              for model, totals in self.per_model.items()},
                "tripped": self.tripped_reason is not None,
                "tripped_reason": self.tripped_reason,
            }


# ---------------------------------------------------------------------------
# HTTP proxy
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProxyConfig:
    upstream: str
    api_key: str
    session_token: str
    prices: dict[str, ModelPrice]
    timeout_s: float = 900.0


def _make_handler(config: ProxyConfig, ledger: Ledger):
    upstream = config.upstream.rstrip("/")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:
            return

        def _reply(self, status: int, payload: bytes, content_type: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _error(self, status: int, message: str, code: str) -> None:
            self._reply(status, json.dumps({"error": {"message": message, "type": code, "code": code}}).encode())

        def do_GET(self) -> None:
            self._error(404, "only POST /v1/responses and /v1/chat/completions are proxied", "not_proxied")

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if 0 < length <= MAX_BODY_BYTES else b""
            supplied = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
            if not hmac.compare_digest(supplied.encode(), config.session_token.encode()):
                self._error(401, "wrong session token for the budget proxy", "proxy_auth")
                return
            if path not in FORWARDED_PATHS:
                self._error(404, f"{path} is not proxied", "not_proxied")
                return
            if not raw:
                self._error(400, f"request body missing or larger than {MAX_BODY_BYTES} bytes", "proxy_body")
                return
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                self._error(400, "request body is not JSON", "proxy_body")
                return
            model = body.get("model") if isinstance(body, dict) else None
            price = config.prices.get(model) if isinstance(model, str) else None
            if price is None:
                self._error(400, f"model {model!r} is not on the budget proxy's allow-list", "model_not_allowed")
                return
            if body.get("stream"):
                self._error(400, "streaming is not metered; send stream=false", "stream_not_allowed")
                return
            output_cap = max_output_tokens(body)
            if output_cap is None:
                self._error(400, "request has no max output token limit; the guard cannot bound it",
                            "unbounded_request")
                return
            worst = worst_case_usd(len(raw), output_cap, price)
            allowed, reason = ledger.reserve(model, worst)
            if not allowed:
                self._error(402, reason, "budget_exceeded")
                return
            request = urllib.request.Request(
                f"{upstream}{path.removeprefix('/v1')}", data=raw, method="POST",
                headers={"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"})
            started = time.monotonic()
            try:
                with urllib.request.urlopen(request, timeout=config.timeout_s) as response:
                    status, payload = response.status, response.read()
                    content_type = response.headers.get("Content-Type", "application/json")
            except urllib.error.HTTPError as exc:
                payload = exc.read()
                ledger.settle(model=model, endpoint=path, status=exc.code, reserved_usd=worst, usage=None,
                              price=price, latency_s=time.monotonic() - started, note="upstream_error")
                self._reply(exc.code, payload, exc.headers.get("Content-Type", "application/json"))
                return
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                ledger.settle(model=model, endpoint=path, status=502, reserved_usd=worst, usage=None,
                              price=price, latency_s=time.monotonic() - started, note="transport_error_after_send")
                self._error(502, f"upstream transport error: {type(exc).__name__}", "upstream_transport")
                return
            try:
                usage = parse_usage(json.loads(payload))
            except json.JSONDecodeError:
                usage = None
            ledger.settle(model=model, endpoint=path, status=status, reserved_usd=worst, usage=usage, price=price,
                          latency_s=time.monotonic() - started, note="" if usage is not None else "usage_missing")
            self._reply(status, payload, content_type)

    return Handler


class BudgetProxy:
    """Runs the proxy on 127.0.0.1 in a daemon thread."""

    def __init__(self, config: ProxyConfig, ledger: Ledger, port: int = 0):
        self.ledger = ledger
        self._server = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(config, ledger))
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="budget-proxy", daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/v1"

    def start(self) -> BudgetProxy:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=10)


def is_official_upstream(url: str) -> bool:
    return url.rstrip("/") == OFFICIAL_UPSTREAM


def check_key_routing(upstream: str, key_file: Path | None) -> None:
    """The real key (default key file or env) may only ever travel to api.openai.com."""
    if is_official_upstream(upstream):
        return
    if key_file is None or key_file.expanduser().resolve() == DEFAULT_KEY_FILE.resolve():
        raise BudgetError("a non-OpenAI upstream needs an explicit --key-file other than the real key file")
    if not key_file.exists():
        raise BudgetError(f"{key_file} does not exist (a non-OpenAI upstream never falls back to the env key)")


def scrub_environment(environ: dict[str, str]) -> dict[str, str]:
    markers = ("API_KEY", "APIKEY", "TOKEN", "SECRET", "PASSWORD")
    return {name: value for name, value in environ.items() if not any(m in name.upper() for m in markers)}


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path

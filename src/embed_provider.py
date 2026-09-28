"""Pluggable embedding provider abstraction.

Scaffolding only. The existing FastEmbed flow inside src/server.py stays
put — this module just exposes it behind a stable Protocol and adds
OpenAI/Cohere cloud backends. Wiring into server.py comes later.

Providers:
  - FastEmbedProvider   — local (fastembed lib), no HTTP.
  - OpenAIEmbedProvider — POST {base}/embeddings; supports any
    OpenAI-compatible embed endpoint (OpenRouter, LiteLLM, LM Studio).
  - CohereEmbedProvider — POST {base}/embed (v2 API).
  - DashScopeEmbedProvider — Alibaba Cloud Model Studio text-embedding-v4
    through its OpenAI-compatible endpoint (dimensions, 10-text batches).
"""

from __future__ import annotations

import datetime
import email.utils
import json
import random
import sys
import time
import urllib.error
import urllib.request
from typing import Protocol, Sequence, runtime_checkable

import config

LOG = lambda msg: sys.stderr.write(f"[embed-provider] {msg}\n")


# ──────────────────────────────────────────────
# Protocol
# ──────────────────────────────────────────────


@runtime_checkable
class EmbeddingProvider(Protocol):
    name: str

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one embedding per input text."""
        ...

    def dim(self) -> int:
        """Vector dimensionality. May return 0 if unknown until first call."""
        ...

    def available(self) -> bool:
        ...


# ──────────────────────────────────────────────
# Known model → dimension table
# ──────────────────────────────────────────────
#
# Used to report `dim()` without forcing an actual request. Conservative —
# callers should treat 0 as "unknown, run one embed first".

_OPENAI_DIM = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
}

# Alibaba Cloud Model Studio text embeddings: allowed `dimensions` values
# (first entry = native default) and the per-request text limit.
TEXT_EMBEDDING_V4 = "text-embedding-v4"
_DASHSCOPE_DIMENSIONS = {
    TEXT_EMBEDDING_V4: (1024, 2048, 1536, 768, 512, 256, 128, 64),
    "text-embedding-v3": (1024, 768, 512, 256, 128, 64),
}
DASHSCOPE_MAX_BATCH = 10

_COHERE_DIM = {
    "embed-english-v3.0": 1024,
    "embed-multilingual-v3.0": 1024,
    "embed-english-light-v3.0": 384,
    "embed-multilingual-light-v3.0": 384,
}

_FASTEMBED_DIM = {
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2": 384,
    "BAAI/bge-small-en-v1.5": 384,
    "BAAI/bge-base-en-v1.5": 768,
    "BAAI/bge-large-en-v1.5": 1024,
}


# ──────────────────────────────────────────────
# HTTP helper (shared shape with llm_provider)
# ──────────────────────────────────────────────


def _ssl_context():
    """Return an SSL context that works on Python.org macOS installs.

    Those installs ship without system CAs, so urllib's default verify
    always fails on TLS endpoints. Prefer certifi when available; fall
    back to the platform default (e.g. Linux distros where CAs exist).
    """
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


RETRYABLE_HTTP_STATUS = frozenset((408, 425, 429, 500, 502, 503, 504))
DEFAULT_MAX_BACKOFF_SEC = 30.0


def _retry_after_seconds(error: urllib.error.HTTPError) -> float | None:
    """Parse a Retry-After header (delta-seconds or HTTP-date); None if absent/invalid."""
    raw = error.headers.get("Retry-After") if error.headers is not None else None
    if not raw:
        return None
    raw = raw.strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.UTC)
    return max(0.0, (when - datetime.datetime.now(datetime.UTC)).total_seconds())


def _backoff_seconds(attempt: int, retry_after: float | None, max_backoff: float) -> float:
    """Server-requested delay when given, else jittered exponential backoff; both capped."""
    if retry_after is not None:
        return min(retry_after, max_backoff)
    base = min(float(2 ** attempt), max_backoff)
    return random.uniform(base / 2, base)


def _http_post_json(
    url: str,
    body: dict,
    headers: dict[str, str],
    timeout: float,
    retries: int = 4,
    *,
    max_backoff: float = DEFAULT_MAX_BACKOFF_SEC,
) -> dict:
    """POST JSON; retry 408/425/429/5xx and network errors with capped backoff.

    Honours Retry-After on retryable responses. Other 4xx (auth, quota,
    malformed input) raise immediately — retrying cannot fix them.
    """
    data = json.dumps(body).encode("utf-8")
    hdrs = {"Content-Type": "application/json", **headers}
    last_exc: Exception | None = None
    ctx = _ssl_context()
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                raw = resp.read()
            return json.loads(raw)
        except urllib.error.HTTPError as e:
            if e.code in RETRYABLE_HTTP_STATUS and attempt < retries:
                wait = _backoff_seconds(attempt, _retry_after_seconds(e), max_backoff)
                LOG(f"HTTP {e.code} on {url} — retry in {wait:.1f}s (attempt {attempt + 1})")
                time.sleep(wait)
                last_exc = e
                continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < retries:
                wait = _backoff_seconds(attempt, None, max_backoff)
                LOG(f"Network error on {url}: {e} — retry in {wait:.1f}s (attempt {attempt + 1})")
                time.sleep(wait)
                last_exc = e
                continue
            raise
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("unreachable")


# ──────────────────────────────────────────────
# FastEmbed (local)
# ──────────────────────────────────────────────


class FastEmbedProvider:
    """Thin wrapper around the existing FastEmbed init in server.Store.

    Kept intentionally small: lazy-loads the model, converts generators to
    plain lists-of-lists so the output shape matches the HTTP providers.
    """

    name = "fastembed"

    def __init__(self, model: str | None = None) -> None:
        self._model_name = model or config.get_embed_model("fastembed")
        self._model: object | None = None  # TextEmbedding instance or False
        self._dim_cache: int = _FASTEMBED_DIM.get(self._model_name, 0)

    @property
    def model_name(self) -> str:
        return self._model_name

    def _ensure_model(self) -> object | None:
        if self._model is False:
            return None
        if self._model is not None:
            return self._model
        try:
            from fastembed import TextEmbedding  # type: ignore[import-not-found]
        except ImportError:
            self._model = False
            return None
        try:
            from memory_core.fastembed_loader import load_model

            self._model = load_model(TextEmbedding, self._model_name, threads=config.get_embed_threads())
        except Exception as exc:  # noqa: BLE001
            # Loudly: the caller falls back to sentence-transformers, which
            # pulls torch and costs ~400 MB RSS. That used to happen with only
            # this one line to explain it, so a purged model cache looked like
            # "the memory server randomly eats 1.5 GB". Name the cache, because
            # a half-purged one (macOS clears the system tmp dir) is the usual
            # cause and TAM_MODEL_CACHE is the fix.
            import os as _os  # noqa: PLC0415

            cache = _os.environ.get("FASTEMBED_CACHE_PATH") or "the system temp dir"
            LOG(f"FastEmbed init failed: {exc}")
            LOG(
                f"  model cache: {cache}"
            )
            LOG(
                "  falling back to sentence-transformers, which is NOT in the "
                'base install any more (pip install "total-agent-memory[rerank]"). '
                "If the cache was purged, set TAM_MODEL_CACHE to a durable path "
                "and restart — that is the fix, not installing torch."
            )
            self._model = False
            return None
        return self._model

    def available(self) -> bool:
        return self._ensure_model() is not None

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        model = self._ensure_model()
        if model is None:
            raise RuntimeError("FastEmbedProvider: model unavailable")
        # fastembed yields generator of numpy arrays
        out: list[list[float]] = []
        for vec in model.embed(list(texts)):  # type: ignore[attr-defined]
            if hasattr(vec, "tolist"):
                out.append(vec.tolist())
            else:
                out.append([float(x) for x in vec])
        if out and not self._dim_cache:
            self._dim_cache = len(out[0])
        return out

    def dim(self) -> int:
        return self._dim_cache


# ──────────────────────────────────────────────
# OpenAI (and OpenAI-compatible)
# ──────────────────────────────────────────────


def _l2_normalise_vec(vec: list[float]) -> list[float]:
    """Return a copy of `vec` rescaled to unit L2 length.

    Pure-Python fallback (no numpy dep): cosine similarity == dot product
    once both query and stored vectors are L2-normalised, which lets the
    binary-quantization path treat sign bits as Hamming proxies for cosine.
    """
    s = 0.0
    for x in vec:
        s += float(x) * float(x)
    if s <= 0.0:
        return [float(x) for x in vec]
    inv = 1.0 / (s ** 0.5)
    return [float(x) * inv for x in vec]


class OpenAIEmbedProvider:
    """OpenAI embeddings API.

    Body schema: `{"input": [texts...], "model": model}`. Response:
    `{"data": [{"embedding": [...]}, ...]}`. Supports `text-embedding-3-small`
    (1536) and `text-embedding-3-large` (3072). `api_base` override lets this
    target LiteLLM / OpenRouter / self-hosted proxies.

    Vectors are L2-normalised before being returned (see `normalize=False` to
    opt out). The `embeddings` table stores both float32 and 1-bit quantised
    blobs; cosine similarity reduces to a dot product (or a Hamming distance
    on the binary blob) only when both query and stored vectors live on the
    unit sphere.

    Outputs are batched at `batch_size` (default 64) to stay under OpenAI's
    per-request token cap on long documents.
    """

    name = "openai"

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        model: str | None = None,
        *,
        batch_size: int = 64,
        normalize: bool = False,
        dimensions: int | None = None,
        max_retries: int = 4,
        timeout: float = 60.0,
        max_backoff: float = DEFAULT_MAX_BACKOFF_SEC,
    ) -> None:
        self.api_key = api_key
        self.api_base = (api_base or config.get_embed_api_base("openai")).rstrip("/")
        self._model = model or config.get_embed_model("openai")
        self._batch_size = max(1, int(batch_size))
        self._normalize = bool(normalize)
        if dimensions is not None and int(dimensions) < 1:
            raise ValueError(f"dimensions must be positive, got {dimensions}")
        self._dimensions = int(dimensions) if dimensions is not None else None
        if int(max_retries) < 0:
            raise ValueError(f"max_retries must be >= 0, got {max_retries}")
        self._max_retries = int(max_retries)
        self._timeout = float(timeout)
        self._max_backoff = float(max_backoff)

    @property
    def model(self) -> str:
        return self._model

    @property
    def batch_size(self) -> int:
        return self._batch_size

    @property
    def dimensions(self) -> int | None:
        return self._dimensions

    def available(self) -> bool:
        return bool(self.api_key) and bool(self.api_base)

    def dim(self) -> int:
        if self._dimensions is not None:
            return self._dimensions
        return _OPENAI_DIM.get(self._model, 0)

    def _request_body(self, batch: list[str]) -> dict:
        body: dict = {"input": batch, "model": self._model}
        if self._dimensions is not None:
            body["dimensions"] = self._dimensions
        return body

    def _embed_batch(self, batch: list[str], *, timeout: float) -> list[list[float]]:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        resp = _http_post_json(
            f"{self.api_base}/embeddings",
            body=self._request_body(batch),
            headers=headers,
            timeout=timeout,
            retries=self._max_retries,
            max_backoff=self._max_backoff,
        )
        try:
            items = resp["data"]
        except (KeyError, TypeError) as exc:
            raise RuntimeError(f"{type(self).__name__}: malformed response: {exc}") from exc
        if not isinstance(items, list) or len(items) != len(batch):
            got = len(items) if isinstance(items, list) else type(items).__name__
            raise RuntimeError(
                f"{type(self).__name__}: expected {len(batch)} embeddings, got {got}"
            )
        # Preserve input order via `index` when provided.
        ordered: list[list[float] | None] = [None] * len(items)
        for position, entry in enumerate(items):
            try:
                idx = int(entry.get("index", position))
                vec = [float(x) for x in entry["embedding"]]
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"{type(self).__name__}: malformed entry: {exc}"
                ) from exc
            if not 0 <= idx < len(items) or ordered[idx] is not None:
                raise RuntimeError(f"{type(self).__name__}: invalid embedding index {idx}")
            if self._dimensions is not None and len(vec) != self._dimensions:
                raise RuntimeError(
                    f"{type(self).__name__}: requested {self._dimensions} dimensions, "
                    f"got {len(vec)}"
                )
            ordered[idx] = _l2_normalise_vec(vec) if self._normalize else vec
        return [vec for vec in ordered if vec is not None]

    def embed(self, texts: Sequence[str], *, timeout: float | None = None) -> list[list[float]]:
        if not self.api_key:
            raise RuntimeError(f"{type(self).__name__}: missing api_key")
        if not texts:
            return []
        all_texts = list(texts)
        per_request = self._timeout if timeout is None else float(timeout)
        out: list[list[float]] = []
        for i in range(0, len(all_texts), self._batch_size):
            chunk = all_texts[i : i + self._batch_size]
            out.extend(self._embed_batch(chunk, timeout=per_request))
        return out


class DashScopeEmbedProvider(OpenAIEmbedProvider):
    """Alibaba Cloud Model Studio embeddings (text-embedding-v4 by default).

    Uses the OpenAI-compatible `POST {base}/embeddings` endpoint. Differences
    from OpenAI that matter here: at most 10 texts per request, `dimensions`
    restricted to the model's published sizes, empty strings rejected by the
    API. The base URL is region-bound (Singapore `dashscope-intl` by default).
    """

    name = "dashscope"

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        model: str | None = None,
        *,
        batch_size: int = DASHSCOPE_MAX_BATCH,
        normalize: bool = True,
        dimensions: int | None = None,
        max_retries: int = 6,
        timeout: float = 60.0,
        max_backoff: float = DEFAULT_MAX_BACKOFF_SEC,
    ) -> None:
        resolved_model = model or config.get_embed_model("dashscope")
        allowed = _DASHSCOPE_DIMENSIONS.get(resolved_model)
        if dimensions is None and allowed:
            dimensions = allowed[0]
        if allowed and dimensions not in allowed:
            raise ValueError(
                f"{resolved_model} supports dimensions {sorted(allowed)}, got {dimensions}"
            )
        if int(batch_size) > DASHSCOPE_MAX_BATCH:
            raise ValueError(
                f"DashScope accepts at most {DASHSCOPE_MAX_BATCH} texts per request, "
                f"got batch_size={batch_size}"
            )
        super().__init__(
            api_key=api_key,
            api_base=api_base or config.get_embed_api_base("dashscope"),
            model=resolved_model,
            batch_size=batch_size,
            normalize=normalize,
            dimensions=dimensions,
            max_retries=max_retries,
            timeout=timeout,
            max_backoff=max_backoff,
        )

    def _request_body(self, batch: list[str]) -> dict:
        body = super()._request_body(batch)
        body["encoding_format"] = "float"
        return body

    def embed(self, texts: Sequence[str], *, timeout: float | None = None) -> list[list[float]]:
        for position, text in enumerate(texts):
            if not isinstance(text, str) or not text.strip():
                raise ValueError(f"DashScopeEmbedProvider: text #{position} is empty")
        return super().embed(texts, timeout=timeout)


# ──────────────────────────────────────────────
# Cohere
# ──────────────────────────────────────────────


class CohereEmbedProvider:
    """Cohere v2 embeddings.

    Body: `{"texts": [...], "model": model, "input_type": "search_document"}`.
    Response: `{"embeddings": {"float": [[...], ...]}}` (v2) or
    `{"embeddings": [[...], ...]}` (legacy). We accept both shapes.
    """

    name = "cohere"

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        model: str | None = None,
        input_type: str = "search_document",
    ) -> None:
        self.api_key = api_key
        self.api_base = (api_base or config.get_embed_api_base("cohere")).rstrip("/")
        self._model = model or config.get_embed_model("cohere")
        self.input_type = input_type

    @property
    def model(self) -> str:
        return self._model

    def available(self) -> bool:
        return bool(self.api_key) and bool(self.api_base)

    def dim(self) -> int:
        return _COHERE_DIM.get(self._model, 0)

    def embed(self, texts: Sequence[str], *, timeout: float = 30.0) -> list[list[float]]:
        if not self.api_key:
            raise RuntimeError("CohereEmbedProvider: missing api_key")
        if not texts:
            return []
        body = {
            "texts": list(texts),
            "model": self._model,
            "input_type": self.input_type,
            "embedding_types": ["float"],
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        resp = _http_post_json(
            f"{self.api_base}/embed",
            body=body,
            headers=headers,
            timeout=timeout,
        )
        raw = resp.get("embeddings")
        try:
            if isinstance(raw, dict):
                # v2 shape: {"float": [[...]]}
                vectors = raw.get("float") or raw.get("embeddings") or []
            elif isinstance(raw, list):
                vectors = raw
            else:
                vectors = []
            return [[float(x) for x in v] for v in vectors]
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"CohereEmbedProvider: malformed response: {exc}") from exc


# ──────────────────────────────────────────────
# Factory
# ──────────────────────────────────────────────


def make_embed_provider(name: str, **kwargs) -> EmbeddingProvider:
    """Build an embedding provider by name.

    kwargs (optional): api_key, api_base, model, batch_size, normalize,
    dimensions; dashscope also takes max_retries, timeout, max_backoff.
    Missing values fall back to config-driven defaults.

    `name="auto"` — read `MEMORY_EMBED_PROVIDER` (default fastembed). When
    that resolves to `openai`, the model defaults to `MEMORY_EMBED_MODEL`
    (or `text-embedding-3-small`); pass `model=` explicitly to override.
    """
    key = (name or "").strip().lower()
    if key == "auto":
        key = config.get_embed_provider()

    if key == "fastembed":
        return FastEmbedProvider(model=kwargs.get("model"))
    if key == "openai":
        # Production OpenAI path always L2-normalises so cosine == dot
        # against existing FastEmbed/ST vectors, which are unit-norm too.
        # The class default is `False` purely to preserve byte-for-byte
        # back-compat in legacy tests that constructed the class directly.
        opts: dict = {
            "batch_size": int(kwargs["batch_size"]) if "batch_size" in kwargs else 64,
            "normalize": bool(kwargs["normalize"]) if "normalize" in kwargs else True,
        }
        return OpenAIEmbedProvider(
            api_key=kwargs.get("api_key") or config.get_embed_api_key("openai"),
            api_base=kwargs.get("api_base") or config.get_embed_api_base("openai"),
            model=kwargs.get("model") or config.get_embed_model("openai"),
            dimensions=kwargs.get("dimensions") or config.get_embed_dimensions("openai"),
            **opts,
        )
    if key == "cohere":
        return CohereEmbedProvider(
            api_key=kwargs.get("api_key") or config.get_embed_api_key("cohere"),
            api_base=kwargs.get("api_base") or config.get_embed_api_base("cohere"),
            model=kwargs.get("model"),
        )
    if key == "dashscope":
        return DashScopeEmbedProvider(
            api_key=kwargs.get("api_key") or config.get_embed_api_key("dashscope"),
            api_base=kwargs.get("api_base") or config.get_embed_api_base("dashscope"),
            model=kwargs.get("model") or config.get_embed_model("dashscope"),
            batch_size=int(kwargs.get("batch_size") or config.get_embed_batch_size("dashscope")),
            normalize=bool(kwargs.get("normalize", True)),
            dimensions=kwargs.get("dimensions") or config.get_embed_dimensions("dashscope"),
            max_retries=int(kwargs.get("max_retries", config.get_embed_max_retries())),
            timeout=float(kwargs.get("timeout") or config.get_embed_timeout_sec()),
            max_backoff=float(kwargs.get("max_backoff") or config.get_embed_max_backoff_sec()),
        )
    raise ValueError(
        f"unknown embedding provider {name!r}; expected fastembed|openai|cohere|dashscope|auto"
    )


# ──────────────────────────────────────────────
# Env-driven dispatch
# ──────────────────────────────────────────────


def provider_from_env() -> EmbeddingProvider:
    """Build an embedding provider from MEMORY_EMBED_* env vars.

    Reads `MEMORY_EMBED_PROVIDER` (fastembed|openai|cohere|dashscope) and
    `MEMORY_EMBED_MODEL`; secrets/base URLs come from
    `config.get_embed_api_key()` / `get_embed_api_base()` so the same
    plumbing as `choose_embed.get_provider()` is used. Intended for
    re-embed scripts and any caller that wants to honour the operator's
    `MEMORY_EMBED_PROVIDER=openai` flip without bouncing through the
    `V9_EMBED_BACKEND` table.
    """
    return make_embed_provider("auto")

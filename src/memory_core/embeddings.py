"""v11.0 Phase 3 — Single embedding entry-point for the hot path.

`EmbeddingProvider` is the only API the rest of memory_core/ should use to
turn text into vectors. It enforces three contracts that v11 needs:

1. FastEmbed-first. The `embed_provider.FastEmbedProvider` is preferred;
   `choose_embed.get_provider()` is the fallback for the v9 backend selector.
2. No silent Ollama fallback. If FastEmbed is unavailable AND
   `MEMORY_ALLOW_OLLAMA_IN_HOT_PATH != true`, methods raise `RuntimeError`
   instead of probing Ollama. The fast hot-path tripwire test relies on this.
3. Per-space model resolution. `embed_texts(..., space="code")` consults
   `memory_core.embedding_spaces.model_for_space("code")` so each chunk gets
   the right model (or falls back to the TEXT model if a per-space env var
   is empty — see §J of the audit).

This module imports neither `llm_provider` nor anything in `ai_layer/`.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path

_SRC = str(Path(__file__).resolve().parent.parent)
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import config as _cfg
from memory_core.embedding_spaces import (
    DEFAULT_SPACE,
    is_space_supported,
    model_for_space,
)

_HOT_PATH_OLLAMA_ENV = "MEMORY_ALLOW_OLLAMA_IN_HOT_PATH"
QUERY_CACHE_SIZE = 128
MAX_CACHED_QUERY_CHARS = 4096


def _allow_ollama_in_hot_path() -> bool:
    raw = (os.environ.get(_HOT_PATH_OLLAMA_ENV, "false") or "false").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _build_fastembed(model: str):
    """Build a FastEmbedProvider; return None on import/init failure."""
    try:
        from embed_provider import FastEmbedProvider  # noqa: WPS433
    except Exception:  # noqa: BLE001 — embed_provider absent ⇒ no fastembed
        return None
    try:
        provider = FastEmbedProvider(model=model)
    except Exception:  # noqa: BLE001
        return None
    if not provider.available():
        return None
    return provider


def _build_choose_embed_fallback():
    """Return a `choose_embed.get_provider()` instance, or None on failure."""
    try:
        import choose_embed  # noqa: WPS433
    except Exception:  # noqa: BLE001
        return None
    try:
        provider = choose_embed.get_provider()
    except Exception:  # noqa: BLE001
        return None
    if not provider.available():
        return None
    return provider


class EmbeddingProvider:
    """Hot-path-safe embedding entry-point.

    One instance is enough for the whole process — providers cache their
    models internally. Construct lazily on first call to keep import cost
    near zero.
    """

    def __init__(self) -> None:
        self._providers: dict[tuple[str, str], object] = {}
        self._unavailable: set[tuple[str, str]] = set()
        self._cached_query = lru_cache(maxsize=QUERY_CACHE_SIZE)(self._encode_query)

    # ─── public ─────────────────────────────────────────────────────

    def embed_texts(
        self,
        texts: Sequence[str],
        *,
        space: str = DEFAULT_SPACE,
    ) -> list[list[float]]:
        if not texts:
            return []
        provider = self._provider_for(space)
        return provider.embed(list(texts))  # type: ignore[union-attr]

    def embed_query(
        self,
        query: str,
        *,
        space: str = DEFAULT_SPACE,
    ) -> list[float]:
        encoder = self._cached_query if len(query) <= MAX_CACHED_QUERY_CHARS else self._encode_query
        return list(encoder(query, space, self.active_model(space)))

    def _encode_query(self, query: str, space: str, model: str) -> tuple[float, ...]:
        if model != self.active_model(space):
            raise RuntimeError('Embedding model changed during query encoding')
        out = self.embed_texts([query], space=space)
        return tuple(out[0]) if out else ()

    def active_model(self, space: str = DEFAULT_SPACE) -> str:
        space_norm = space if is_space_supported(space) else DEFAULT_SPACE
        return model_for_space(space_norm)

    def dim(self) -> int:
        try:
            provider = self._provider_for(DEFAULT_SPACE)
        except RuntimeError:
            return 0
        try:
            return int(provider.dim())  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            return 0

    def health(self) -> dict:
        ok_fast = _build_fastembed(_cfg.get_text_embed_model()) is not None
        return {
            "fastembed_available": ok_fast,
            "allow_ollama_in_hot_path": _allow_ollama_in_hot_path(),
            "default_space": DEFAULT_SPACE,
            "active_text_model": model_for_space(DEFAULT_SPACE),
            "dim": self.dim(),
        }

    # ─── internal ───────────────────────────────────────────────────

    def _provider_for(self, space: str):
        space_norm = space if is_space_supported(space) else DEFAULT_SPACE
        model = model_for_space(space_norm)
        key = (space_norm, model)
        cached = self._providers.get(key)
        if cached is not None:
            return cached
        if key in self._unavailable:
            raise RuntimeError(f"EmbeddingProvider: {model!r} for space {space_norm!r} could not be loaded")

        # Preferred: FastEmbed (local, no HTTP, fits the hot path).
        provider = _build_fastembed(model)
        if provider is None and space_norm != DEFAULT_SPACE:
            # The fallback below serves the text backend's model, not `model`:
            # its vectors would be recorded under `model` with the wrong
            # dimension. Fail instead, so the caller stores the row in the
            # text space; remember the failure so later calls stay offline.
            self._unavailable.add(key)
            raise RuntimeError(f"EmbeddingProvider: {model!r} for space {space_norm!r} could not be loaded")
        if provider is None:
            # Secondary: choose_embed.get_provider() — handles bge-m3 / ST /
            # OpenAI per V9_EMBED_BACKEND. Still LLM-free for local backends.
            provider = _build_choose_embed_fallback()

        if provider is None:
            if not _allow_ollama_in_hot_path():
                raise RuntimeError(
                    "EmbeddingProvider: FastEmbed unavailable and "
                    f"{_HOT_PATH_OLLAMA_ENV}=false. "
                    "v11 hot path forbids silent Ollama fallback — install "
                    "fastembed or set the env var explicitly."
                )
            # Allow-list: the caller has explicitly opted into Ollama in the
            # hot path. Build the choose_embed fallback even if it failed
            # health-checks earlier; the caller wants the probe.
            provider = _build_choose_embed_fallback()
            if provider is None:
                raise RuntimeError(
                    "EmbeddingProvider: no embedding backend available "
                    "(fastembed missing AND choose_embed fallback unusable)."
                )

        self._providers[key] = provider
        return provider


__all__ = ["EmbeddingProvider"]

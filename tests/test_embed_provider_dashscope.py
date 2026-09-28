"""text-embedding-v4 (Alibaba Cloud Model Studio) provider — zero-network.

Every HTTP call goes through a fake `urllib.request.urlopen`, so the request
shape, batching, retry and dimension gates are asserted without a key.
"""

from __future__ import annotations

import email.message
import io
import json
import math
import urllib.error

import pytest

import embed_provider

INTL_BASE = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"


class _FakeResp:
    def __init__(self, payload: dict) -> None:
        self._payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def read(self):
        return self._payload


def _vectors(n: int, dim: int, *, reverse: bool = False) -> dict:
    items = [{"index": i, "embedding": [float(i + 1)] * dim, "object": "embedding"} for i in range(n)]
    if reverse:
        items.reverse()
    return {"data": items, "model": "text-embedding-v4", "usage": {"prompt_tokens": n, "total_tokens": n}}


def _http_error(code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(INTL_BASE + "/embeddings", code, "error", headers, io.BytesIO(b"{}"))


class _Transport:
    """Scripted urlopen: each entry is an exception to raise or a payload factory."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []

    def __call__(self, req, timeout=None, *, context=None, **_kw):
        body = json.loads(req.data.decode("utf-8"))
        self.calls.append({"url": req.full_url, "headers": dict(req.headers), "body": body,
                           "timeout": timeout})
        step = self.script.pop(0) if self.script else (lambda b: _vectors(len(b["input"]), b.get("dimensions", 4)))
        if isinstance(step, Exception):
            raise step
        return _FakeResp(step(body))


@pytest.fixture
def sleeps(monkeypatch):
    recorded: list[float] = []
    monkeypatch.setattr(embed_provider.time, "sleep", recorded.append)
    return recorded


@pytest.fixture
def dashscope_env(monkeypatch):
    for name in ("MEMORY_EMBED_MODEL", "MEMORY_EMBED_API_BASE", "MEMORY_EMBED_API_KEY",
                 "MEMORY_EMBED_DIMENSIONS", "MEMORY_EMBED_BATCH_SIZE", "MEMORY_EMBED_MAX_RETRIES",
                 "MEMORY_EMBED_TIMEOUT_SEC", "MEMORY_EMBED_MAX_BACKOFF_SEC"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MEMORY_EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "ds-test-key")


def test_factory_defaults_to_text_embedding_v4_intl(dashscope_env):
    provider = embed_provider.provider_from_env()
    assert isinstance(provider, embed_provider.DashScopeEmbedProvider)
    assert provider.name == "dashscope"
    assert provider.model == "text-embedding-v4"
    assert provider.api_base == INTL_BASE
    assert provider.dim() == 1024
    assert provider.batch_size == 10
    assert provider.available()


def test_env_overrides_base_dimensions_and_batch(dashscope_env, monkeypatch):
    monkeypatch.setenv("MEMORY_EMBED_API_BASE", "https://dashscope.aliyuncs.com/compatible-mode/v1/")
    monkeypatch.setenv("MEMORY_EMBED_DIMENSIONS", "768")
    monkeypatch.setenv("MEMORY_EMBED_BATCH_SIZE", "5")
    provider = embed_provider.provider_from_env()
    assert provider.api_base == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert provider.dim() == 768
    assert provider.batch_size == 5


@pytest.mark.parametrize("value", ["abc", "0", "-5"])
def test_invalid_dimensions_env_fails_loudly(dashscope_env, monkeypatch, value):
    monkeypatch.setenv("MEMORY_EMBED_DIMENSIONS", value)
    with pytest.raises(ValueError, match="MEMORY_EMBED_DIMENSIONS"):
        embed_provider.provider_from_env()


def test_unsupported_dimensions_rejected():
    with pytest.raises(ValueError, match="supports dimensions"):
        embed_provider.DashScopeEmbedProvider(api_key="k", dimensions=1000)


def test_batch_above_api_limit_rejected():
    with pytest.raises(ValueError, match="at most 10"):
        embed_provider.DashScopeEmbedProvider(api_key="k", batch_size=11)


def test_request_shape(monkeypatch, sleeps):
    transport = _Transport([])
    monkeypatch.setattr(embed_provider.urllib.request, "urlopen", transport)
    provider = embed_provider.DashScopeEmbedProvider(api_key="ds-key", dimensions=256, timeout=12.5)
    out = provider.embed(["hello", "world"])
    assert len(out) == 2 and all(len(v) == 256 for v in out)
    call = transport.calls[0]
    assert call["url"] == INTL_BASE + "/embeddings"
    assert call["headers"]["Authorization"] == "Bearer ds-key"
    assert call["body"] == {"input": ["hello", "world"], "model": "text-embedding-v4",
                            "dimensions": 256, "encoding_format": "float"}
    assert call["timeout"] == 12.5
    assert sleeps == []


def test_batches_of_ten_and_order_by_index(monkeypatch, sleeps):
    transport = _Transport([lambda b: _vectors(len(b["input"]), 64, reverse=True)] * 3)
    monkeypatch.setattr(embed_provider.urllib.request, "urlopen", transport)
    provider = embed_provider.DashScopeEmbedProvider(api_key="k", dimensions=64, normalize=False)
    out = provider.embed([f"t{i}" for i in range(23)])
    assert [len(c["body"]["input"]) for c in transport.calls] == [10, 10, 3]
    assert len(out) == 23
    # Reversed response is re-ordered by `index`: first of each batch has value 1.0.
    assert out[0][0] == 1.0 and out[9][0] == 10.0 and out[10][0] == 1.0 and out[22][0] == 3.0


def test_vectors_are_l2_normalised_by_default(monkeypatch, sleeps):
    transport = _Transport([lambda b: {"data": [{"index": 0, "embedding": [3.0, 4.0] + [0.0] * 62}]}])
    monkeypatch.setattr(embed_provider.urllib.request, "urlopen", transport)
    vec = embed_provider.DashScopeEmbedProvider(api_key="k", dimensions=64).embed(["x"])[0]
    assert math.isclose(math.sqrt(sum(x * x for x in vec)), 1.0)


def test_dimension_mismatch_in_response_raises(monkeypatch, sleeps):
    transport = _Transport([lambda b: _vectors(1, 512)])
    monkeypatch.setattr(embed_provider.urllib.request, "urlopen", transport)
    provider = embed_provider.DashScopeEmbedProvider(api_key="k", dimensions=1024)
    with pytest.raises(RuntimeError, match="requested 1024 dimensions, got 512"):
        provider.embed(["x"])


def test_count_mismatch_in_response_raises(monkeypatch, sleeps):
    transport = _Transport([lambda b: _vectors(1, 64)])
    monkeypatch.setattr(embed_provider.urllib.request, "urlopen", transport)
    provider = embed_provider.DashScopeEmbedProvider(api_key="k", dimensions=64)
    with pytest.raises(RuntimeError, match="expected 2 embeddings, got 1"):
        provider.embed(["a", "b"])


def test_empty_text_rejected_before_any_request(monkeypatch):
    transport = _Transport([])
    monkeypatch.setattr(embed_provider.urllib.request, "urlopen", transport)
    provider = embed_provider.DashScopeEmbedProvider(api_key="k")
    with pytest.raises(ValueError, match="text #1 is empty"):
        provider.embed(["ok", "   "])
    assert transport.calls == []


def test_missing_key_raises():
    provider = embed_provider.DashScopeEmbedProvider(api_key=None)
    assert provider.available() is False
    with pytest.raises(RuntimeError, match="missing api_key"):
        provider.embed(["x"])


def test_429_honours_retry_after_then_succeeds(monkeypatch, sleeps):
    transport = _Transport([_http_error(429, "7"), _http_error(503)])
    monkeypatch.setattr(embed_provider.urllib.request, "urlopen", transport)
    provider = embed_provider.DashScopeEmbedProvider(api_key="k", dimensions=64, max_backoff=30)
    assert len(provider.embed(["x"])) == 1
    assert len(transport.calls) == 3
    assert sleeps[0] == 7.0
    # Second retry has no Retry-After: jittered exponential backoff for attempt 1 is in [1, 2].
    assert 1.0 <= sleeps[1] <= 2.0


def test_retry_after_is_capped(monkeypatch, sleeps):
    transport = _Transport([_http_error(429, "3600")])
    monkeypatch.setattr(embed_provider.urllib.request, "urlopen", transport)
    embed_provider.DashScopeEmbedProvider(api_key="k", dimensions=64, max_backoff=5).embed(["x"])
    assert sleeps == [5.0]


def test_retries_exhausted_raise_last_error(monkeypatch, sleeps):
    transport = _Transport([_http_error(502)] * 3)
    monkeypatch.setattr(embed_provider.urllib.request, "urlopen", transport)
    provider = embed_provider.DashScopeEmbedProvider(api_key="k", dimensions=64, max_retries=2)
    with pytest.raises(urllib.error.HTTPError) as info:
        provider.embed(["x"])
    assert info.value.code == 502
    assert len(transport.calls) == 3 and len(sleeps) == 2


def test_client_error_is_not_retried(monkeypatch, sleeps):
    transport = _Transport([_http_error(400)])
    monkeypatch.setattr(embed_provider.urllib.request, "urlopen", transport)
    with pytest.raises(urllib.error.HTTPError):
        embed_provider.DashScopeEmbedProvider(api_key="k", dimensions=64).embed(["x"])
    assert len(transport.calls) == 1 and sleeps == []


def test_network_error_is_retried(monkeypatch, sleeps):
    transport = _Transport([urllib.error.URLError("connection reset")])
    monkeypatch.setattr(embed_provider.urllib.request, "urlopen", transport)
    out = embed_provider.DashScopeEmbedProvider(api_key="k", dimensions=64).embed(["x"])
    assert len(out) == 1 and len(sleeps) == 1


def test_store_uses_dashscope_and_refuses_dimension_change(dashscope_env, monkeypatch, tmp_path, sleeps):
    import server

    transport = _Transport([])
    monkeypatch.setattr(embed_provider.urllib.request, "urlopen", transport)
    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    monkeypatch.setenv("MEMORY_EMBED_DIMENSIONS", "256")
    store = server.Store()
    assert store._embed_mode == "dashscope"
    rid, *_ = store.save_knowledge("s", "Alice moved to Berlin", "fact", project="p",
                                   skip_dedup=True, skip_quality=True, source_format="conversation")
    row = store.db.execute("SELECT embed_dim, embed_model FROM embeddings WHERE knowledge_id=?", (rid,)).fetchone()
    assert tuple(row) == (256, "text-embedding-v4")
    assert transport.calls[-1]["body"]["dimensions"] == 256
    store.db.close()

    monkeypatch.setenv("MEMORY_EMBED_DIMENSIONS", "512")
    with pytest.raises(RuntimeError, match="Embedding dimension mismatch: stored=256, provider=512"):
        server.Store()


def test_store_precomputed_embedding_skips_provider(dashscope_env, monkeypatch, tmp_path):
    import server

    transport = _Transport([])
    monkeypatch.setattr(embed_provider.urllib.request, "urlopen", transport)
    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    monkeypatch.setenv("MEMORY_EMBED_DIMENSIONS", "64")
    store = server.Store()
    vector = [0.125] * 64
    rid, *_ = store.save_knowledge("s", "def handler(): return 1", "fact", project="p",
                                   skip_dedup=True, skip_quality=True, source_format="conversation",
                                   embedding=vector)
    assert transport.calls == []
    row = store.db.execute("SELECT embed_dim, embedding_space FROM embeddings WHERE knowledge_id=?",
                           (rid,)).fetchone()
    assert tuple(row) == (64, "text")
    store.db.close()


def test_dashscope_never_falls_back_to_another_model(dashscope_env, monkeypatch, tmp_path, sleeps):
    import server

    monkeypatch.setattr(embed_provider.urllib.request, "urlopen", _Transport([_http_error(401)]))
    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    monkeypatch.setenv("MEMORY_EMBED_DIMENSIONS", "64")
    store = server.Store()
    monkeypatch.setattr(type(store), "embedder", property(lambda _self: pytest.fail("fallback model used")))
    assert store.embed(["x"]) is None
    store.db.close()

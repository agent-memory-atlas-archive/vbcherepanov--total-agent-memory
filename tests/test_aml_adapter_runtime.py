"""AML worker runtime in-process: atomic Add, embedding identity gate, text-embedding-v4 path."""

from __future__ import annotations

import json

import pytest

import embed_provider
from aml_adapter.contracts import AddRequest, SearchRequest
from aml_adapter.errors import Conflict, Unavailable
from aml_adapter.runtime import FORCED_TAM_ENV, Runtime, RuntimeSettings

SETTINGS = RuntimeSettings(fragment_max_chars=6000, content_format="annotated", query_include_options=False,
                           max_top_k=1000, embed_concurrency=3, require_embed_model="", id_prefix="abc")


@pytest.fixture
def user_store(monkeypatch, tmp_path):
    import server

    for name, value in FORCED_TAM_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    return tmp_path


def _add(request_id="r1", texts=("Alice moved to Berlin.",), session="s1") -> AddRequest:
    return AddRequest.model_validate({"request_id": request_id, "user_id": "u", "session_id": session,
                                      "messages": [{"role": "user", "content": t} for t in texts]})


def _search(query, top_k=10) -> SearchRequest:
    return SearchRequest.model_validate({"query": query, "user_id": "u", "top_k": top_k})


def _counts(runtime) -> tuple[int, int, int]:
    db = runtime.db
    return (db.execute("SELECT COUNT(*) FROM aml_requests").fetchone()[0],
            db.execute("SELECT COUNT(*) FROM aml_fragments").fetchone()[0],
            db.execute("SELECT COUNT(*) FROM knowledge WHERE status='active'").fetchone()[0])


def test_failed_add_leaves_nothing_and_retry_succeeds(user_store, monkeypatch):
    monkeypatch.setenv("MEMORY_EMBED_PROVIDER", "fastembed")
    runtime = Runtime(SETTINGS)
    original = runtime.store.save_knowledge
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("disk hiccup")
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime.store, "save_knowledge", flaky)
    request = _add(texts=("first fact about owls", "second fact about owls", "third fact about owls"))
    with pytest.raises(RuntimeError, match="disk hiccup"):
        runtime.add(request)
    assert _counts(runtime) == (0, 0, 0)
    assert runtime.search(_search("owls")) == []

    result = runtime.add(request)
    assert result == {"fragments": 3, "replayed": False}
    assert _counts(runtime) == (1, 3, 3)
    assert runtime.add(request) == {"fragments": 3, "replayed": True}
    assert _counts(runtime) == (1, 3, 3)
    with pytest.raises(Conflict):
        runtime.add(_add(texts=("something else",)))
    runtime.db.close()


def test_embedding_identity_gate_refuses_a_different_model(user_store, monkeypatch):
    monkeypatch.setenv("MEMORY_EMBED_PROVIDER", "fastembed")
    runtime = Runtime(SETTINGS)
    runtime.db.execute("UPDATE aml_meta SET value='dashscope:text-embedding-v4:1024' WHERE key='embedding_identity'")
    runtime.db.commit()
    runtime.db.close()
    with pytest.raises(RuntimeError, match="refusing to mix"):
        Runtime(SETTINGS)


def test_required_model_is_enforced(user_store, monkeypatch):
    monkeypatch.setenv("MEMORY_EMBED_PROVIDER", "fastembed")
    strict = RuntimeSettings(**{**json.loads(SETTINGS.to_json()), "require_embed_model": "text-embedding-v4"})
    with pytest.raises(RuntimeError, match="AML_REQUIRE_EMBED_MODEL"):
        Runtime(strict)


class _FakeResp:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def read(self):
        return self.payload


def _fake_dashscope(calls: list, fail: bool = False):
    def urlopen(req, timeout=None, *, context=None, **_kw):
        body = json.loads(req.data)
        calls.append(body)
        if fail:
            raise embed_provider.urllib.error.URLError("unreachable")
        # Deterministic toy vectors: one hot on the text length bucket, rest small.
        data = []
        for index, text in enumerate(body["input"]):
            vector = [0.01] * body["dimensions"]
            vector[len(text) % body["dimensions"]] = 1.0
            data.append({"index": index, "embedding": vector})
        return _FakeResp({"data": data})
    return urlopen


@pytest.fixture
def dashscope(user_store, monkeypatch):
    monkeypatch.setenv("MEMORY_EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "ds-test")
    monkeypatch.setenv("MEMORY_EMBED_DIMENSIONS", "64")
    monkeypatch.setenv("MEMORY_EMBED_MAX_RETRIES", "0")
    monkeypatch.delenv("MEMORY_EMBED_MODEL", raising=False)
    monkeypatch.delenv("MEMORY_EMBED_API_BASE", raising=False)
    calls: list = []
    monkeypatch.setattr(embed_provider.urllib.request, "urlopen", _fake_dashscope(calls))
    return calls


def test_text_embedding_v4_batches_ten_and_embeds_once(dashscope, monkeypatch):
    strict = RuntimeSettings(**{**json.loads(SETTINGS.to_json()), "require_embed_model": "text-embedding-v4"})
    runtime = Runtime(strict)
    assert runtime.dimension == 64
    dashscope.clear()
    texts = [f"note {i}: the meeting room {i} has a projector" for i in range(23)]
    assert runtime.add(_add(texts=texts))["fragments"] == 23
    assert sorted(len(call["input"]) for call in dashscope) == [3, 10, 10]
    assert all(call["model"] == "text-embedding-v4" and call["dimensions"] == 64 for call in dashscope)
    rows = runtime.db.execute("SELECT DISTINCT embed_model, embed_dim FROM embeddings").fetchall()
    assert [tuple(row) for row in rows] == [("text-embedding-v4", 64)]
    dashscope.clear()
    hits = runtime.search(_search("meeting room projector", top_k=5))
    assert 0 < len(hits) <= 5 and all(hit["id"].startswith("abc-") for hit in hits)
    assert len(dashscope) == 1 and dashscope[0]["input"] == ["meeting room projector"]
    runtime.db.close()


def test_embedding_outage_is_retryable_and_writes_nothing(dashscope, monkeypatch):
    runtime = Runtime(SETTINGS)
    monkeypatch.setattr(embed_provider.urllib.request, "urlopen", _fake_dashscope([], fail=True))
    with pytest.raises(Unavailable, match="embedding API failed"):
        runtime.add(_add())
    assert _counts(runtime) == (0, 0, 0)
    runtime.db.close()


def test_search_fails_loudly_when_query_embedding_is_down(dashscope, monkeypatch):
    runtime = Runtime(SETTINGS)
    runtime.add(_add())
    monkeypatch.setattr(embed_provider.urllib.request, "urlopen", _fake_dashscope([], fail=True))
    with pytest.raises(Unavailable, match="semantic retrieval"):
        runtime.search(_search("where does Alice live"))
    runtime.db.close()

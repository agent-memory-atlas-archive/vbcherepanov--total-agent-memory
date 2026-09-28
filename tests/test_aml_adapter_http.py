"""AML adapter over HTTP with real per-user worker processes (local fastembed, no network).

One app instance is shared by the module so worker processes are spawned
once per user. Each test uses its own user_id, which is itself what the
isolation guarantees make safe.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time

import httpx
import pytest
from starlette.testclient import TestClient

from aml_adapter.app import create_app
from aml_adapter.cli import build_service
from aml_adapter.config import AdapterConfig

KEY = "test-key-1"
AUTH = {"Authorization": f"Bearer {KEY}"}
LOCAL_EMBED_ENV = {"MEMORY_EMBED_PROVIDER": "fastembed", "MEMORY_CROSS_RERANK": "off"}


@pytest.fixture(scope="module")
def local_embeddings():
    saved = {name: os.environ.get(name) for name in (*LOCAL_EMBED_ENV, "MEMORY_EMBED_MODEL")}
    os.environ.update(LOCAL_EMBED_ENV)
    os.environ.pop("MEMORY_EMBED_MODEL", None)
    yield
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def _config(data_dir, **extra) -> AdapterConfig:
    return AdapterConfig.from_env({"AML_DATA_DIR": str(data_dir), "AML_API_KEYS": f"{KEY},second-key",
                                   "AML_WORKERS": "3", "AML_MAX_BODY_BYTES": "200000",
                                   "AML_OPERATION_TIMEOUT_SECONDS": "300", **extra})


@pytest.fixture(scope="module")
def app_parts(tmp_path_factory, local_embeddings):
    config = _config(tmp_path_factory.mktemp("aml-http"))
    service = build_service(config)
    app = create_app(config, service, "test")
    with TestClient(app) as client:
        yield client, service, app


@pytest.fixture
def client(app_parts):
    return app_parts[0]


def add(client, user, request_id, messages, session="s1", headers=AUTH):
    return client.post("/add", headers=headers, json={
        "request_id": request_id, "user_id": user, "session_id": session, "messages": messages})


def search(client, user, query, top_k=10, **extra):
    return client.post("/search", headers=AUTH, json={"query": query, "user_id": user, "top_k": top_k, **extra})


def msg(content, role="user", timestamp=None):
    body = {"role": role, "content": content}
    if timestamp is not None:
        body["timestamp"] = timestamp
    return body


def test_health_needs_no_auth(client):
    response = client.get("/health")
    assert response.status_code == 200 and response.json()["status"] == "ok"


def test_only_three_endpoints_are_exposed(client):
    assert client.get("/metrics").status_code == 404
    assert client.get("/").status_code == 404
    assert client.get("/add", headers=AUTH).status_code == 405


def test_add_then_immediate_search_with_echo_and_created_at(client):
    response = add(client, "user-basic", "req-1", [
        msg("Caroline adopted a golden retriever named Max.", timestamp=1683547200000),
        msg("That is lovely, dogs are great company.", role="assistant", timestamp=1683547260500),
    ], session="session-7")
    assert response.status_code == 200
    assert response.json() == {"success": True, "request_id": "req-1", "user_id": "user-basic",
                               "session_id": "session-7"}
    data = search(client, "user-basic", "What is the name of Caroline's dog?").json()["data"]
    assert data, "Add must be searchable immediately"
    top = data[0]
    assert "golden retriever named Max" in top["content"]
    assert top["content"] == "[2023-05-08T12:00:00Z] user: Caroline adopted a golden retriever named Max."
    assert top["created_at"] == "2023-05-08T12:00:00Z"
    assert set(top) <= {"id", "content", "score", "created_at"}
    by_content = {item["content"]: item for item in data}
    assert by_content["[2023-05-08T12:01:00.500Z] assistant: That is lovely, dogs are great company."][
        "created_at"] == "2023-05-08T12:01:00.500Z"


def test_scores_are_sorted_and_top_k_respected_with_stable_ids(client):
    facts = [f"Fact number {i}: the office plant on desk {i} is a cactus." for i in range(8)]
    assert add(client, "user-rank", "rank-1", [msg(text) for text in facts]).status_code == 200
    first = search(client, "user-rank", "cactus on desk 3", top_k=5).json()["data"]
    assert 0 < len(first) <= 5
    scores = [item["score"] for item in first]
    assert scores == sorted(scores, reverse=True)
    assert len({item["id"] for item in first}) == len(first)
    second = search(client, "user-rank", "cactus on desk 3", top_k=5).json()["data"]
    assert [item["id"] for item in second] == [item["id"] for item in first]
    assert len(search(client, "user-rank", "cactus", top_k=1).json()["data"]) == 1


def test_idempotent_retry_stores_once(client):
    messages = [msg("Melanie ran a charity race for mental health on Saturday.")]
    assert add(client, "user-retry", "retry-1", messages).status_code == 200
    again = add(client, "user-retry", "retry-1", messages)
    assert again.status_code == 200 and again.json()["success"] is True
    data = search(client, "user-retry", "charity race", top_k=50).json()["data"]
    assert sum("charity race" in item["content"] for item in data) == 1


def test_conflicting_payload_for_same_request_id_is_409(client):
    assert add(client, "user-conflict", "c-1", [msg("original text")]).status_code == 200
    response = add(client, "user-conflict", "c-1", [msg("different text")])
    assert response.status_code == 409 and response.json()["code"] == "conflict"
    data = search(client, "user-conflict", "different text", top_k=50).json()["data"]
    assert all("different text" not in item["content"] for item in data)


def test_same_request_id_under_another_user_is_independent(client):
    assert add(client, "user-iso-a", "shared-id", [msg("Alpha keeps bees in Lisbon.")]).status_code == 200
    assert add(client, "user-iso-b", "shared-id", [msg("Beta grows tomatoes in Oslo.")]).status_code == 200


def test_user_isolation_has_zero_leakage(client):
    secret = "The vault combination for project Nightingale is 4471."
    assert add(client, "user-iso-owner", "iso-1", [msg(secret)]).status_code == 200
    assert add(client, "user-iso-other", "iso-2", [msg("I like hiking in the Alps.")]).status_code == 200
    for query in ("vault combination Nightingale", secret):
        leaked = search(client, "user-iso-other", query, top_k=100).json()["data"]
        assert all("Nightingale" not in item["content"] for item in leaked)
    owner = search(client, "user-iso-owner", "vault combination", top_k=100).json()["data"]
    assert any("Nightingale" in item["content"] for item in owner)
    other_ids = {item["id"] for item in search(client, "user-iso-other", "hiking", top_k=100).json()["data"]}
    assert other_ids.isdisjoint({item["id"] for item in owner})


def test_unknown_user_gets_empty_list(client):
    response = search(client, "never-written", "anything")
    assert response.status_code == 200 and response.json() == {"data": []}


def test_options_are_accepted(client):
    assert add(client, "user-mc", "mc-1", [msg("The train to Porto leaves at 9am.")]).status_code == 200
    response = search(client, "user-mc", "When does the train leave?", options=["A. 9am", "B. 5pm"])
    assert response.status_code == 200 and response.json()["data"]


def test_content_parts_query_and_messages(client):
    parts = [{"type": "text", "text": "Rover the robot"}, {"type": "text", "text": "cleans the lab."}]
    assert add(client, "user-parts", "p-1", [msg(parts)]).status_code == 200
    data = search(client, "user-parts", [{"type": "text", "text": "robot lab"}]).json()["data"]
    assert data and "Rover the robot\ncleans the lab." in data[0]["content"]


@pytest.mark.parametrize("header", [
    {"Authorization": f"Bearer {KEY}"},
    {"Authorization": f"Token {KEY}"},
    {"X-Api-Key": "second-key"},
    {"Token": KEY},
])
def test_auth_styles_accepted(client, header):
    assert add(client, "user-auth", "auth-" + next(iter(header.values()))[-6:] + next(iter(header)),
               [msg("auth check")], headers=header).status_code == 200


@pytest.mark.parametrize("header", [{}, {"Authorization": "Bearer wrong"}, {"X-Api-Key": "wrong"}])
def test_auth_rejected(client, header):
    response = add(client, "user-auth", "auth-bad", [msg("x")], headers=header)
    assert response.status_code == 401
    assert client.post("/search", headers=header, json={"query": "x", "user_id": "u", "top_k": 1}).status_code == 401


@pytest.mark.parametrize("body,status", [
    ({"request_id": "x", "user_id": "u", "session_id": "s", "messages": []}, 422),
    ({"request_id": "x", "session_id": "s", "messages": [{"role": "user", "content": "a"}]}, 422),
    ({"request_id": "x", "user_id": "u", "session_id": "s", "messages": [{"role": "user", "content": " "}]}, 422),
    ({"request_id": "x", "user_id": "u", "session_id": "s", "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}]}, 422),
])
def test_add_contract_errors(client, body, status):
    response = client.post("/add", headers=AUTH, json=body)
    assert response.status_code == status and response.json()["code"] == "invalid_request"


def test_search_contract_errors(client):
    assert search(client, "u", "q", top_k=0).status_code == 422
    assert client.post("/search", headers=AUTH, json={"query": "q", "user_id": "u"}).status_code == 422
    assert client.post("/search", headers=AUTH, content=b"{not json").status_code == 400


def test_body_limit(client):
    response = add(client, "user-big", "big-1", [msg("x" * 300_000)])
    assert response.status_code == 413


def test_inflight_limit_returns_429_with_retry_after(app_parts):
    client, _service, app = app_parts
    app.inflight = app.config.max_inflight
    try:
        response = search(client, "u", "q")
    finally:
        app.inflight = 0
    assert response.status_code == 429
    assert response.headers["Retry-After"] == str(app.config.retry_after_seconds)
    assert client.get("/health").status_code == 200


def test_retention_purge_deletes_data_and_journals(app_parts):
    client, service, _app = app_parts
    assert add(client, "user-purge", "purge-1", [msg("Temporary fact about kiwis.")]).status_code == 200
    ns = service.registry.lookup("user-purge")
    user_dir = service.registry.users_dir / ns
    assert user_dir.exists()
    deleted = service.purge_expired(now=time.time() + 60 * 86400)
    entry = next(e for e in deleted if e["user_ns"] == ns)
    assert entry["fragments"] == 1 and entry["requests"] == 1 and entry["bytes"] > 0
    assert not user_dir.exists()
    journal = [json.loads(line) for line in service.registry.journal_path.read_text().splitlines()]
    assert any(line["user_ns"] == ns for line in journal)
    assert "kiwis" not in service.registry.journal_path.read_text()
    assert search(client, "user-purge", "kiwis").json() == {"data": []}
    # The user can write again after a purge; nothing old comes back.
    assert add(client, "user-purge", "purge-2", [msg("New fact about apples.")]).status_code == 200
    data = search(client, "user-purge", "fact", top_k=50).json()["data"]
    assert data and all("kiwis" not in item["content"] for item in data)


def test_metrics_are_recorded(app_parts):
    _client, service, _app = app_parts
    text = service.metrics.render()
    assert 'aml_requests_total{operation="add",status="ok"}' in text
    assert 'aml_request_duration_seconds_count{operation="search"}' in text


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_end_to_end_real_server(tmp_path, local_embeddings):
    """Real uvicorn listener + real worker processes + local fastembed, over TCP."""
    import uvicorn

    port = _free_port()
    config = _config(tmp_path / "e2e", AML_PORT=str(port))
    service = build_service(config)
    server = uvicorn.Server(uvicorn.Config(create_app(config, service, "e2e"), host="127.0.0.1", port=port,
                                           log_config=None, access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 30
        while not server.started:
            assert time.monotonic() < deadline, "server did not start"
            time.sleep(0.05)
        with httpx.Client(base_url=base, timeout=300) as http:
            assert http.get("/health").status_code == 200
            messages = [msg("Jon opened a dance studio in March 2023.", timestamp=1679000000000),
                        msg("Gina lost her job at Door Dash.", timestamp=1679000060000)]
            body = {"request_id": "e2e-1", "user_id": "e2e-user", "session_id": "e2e-s", "messages": messages}
            first = http.post("/add", headers={"X-Api-Key": KEY}, json=body)
            retry = http.post("/add", headers={"X-Api-Key": KEY}, json=body)
            assert first.status_code == retry.status_code == 200
            assert first.json() == retry.json() == {"success": True, "request_id": "e2e-1",
                                                    "user_id": "e2e-user", "session_id": "e2e-s"}
            found = http.post("/search", headers={"Authorization": f"Token {KEY}"},
                              json={"query": "What did Jon open?", "user_id": "e2e-user", "top_k": 100})
            data = found.json()["data"]
            assert found.status_code == 200 and "dance studio" in data[0]["content"]
            assert len(data) == 2 and data[0]["score"] >= data[1]["score"]
            assert http.post("/search", headers={"Authorization": f"Bearer {KEY}"},
                             json={"query": "dance", "user_id": "someone-else", "top_k": 5}).json() == {"data": []}

        def concurrent_add(index: int) -> tuple[int, str]:
            user = f"parallel-{index % 4}"
            body = {"request_id": f"p-{index}", "user_id": user, "session_id": "p",
                    "messages": [msg(f"Parallel note {index} for {user}.")]}
            with httpx.Client(base_url=base, timeout=300) as own:
                return own.post("/add", headers={"X-Api-Key": KEY}, json=body).status_code, user

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=8) as executor:
            outcomes = list(executor.map(concurrent_add, range(12)))
        # 4 users over 3 workers: requests either succeed or are told to retry.
        assert {status for status, _ in outcomes} <= {200, 429}
        with httpx.Client(base_url=base, timeout=300) as http:
            for status, user in outcomes:
                if status == 429:
                    continue
                data = http.post("/search", headers={"X-Api-Key": KEY},
                                 json={"query": "Parallel note", "user_id": user, "top_k": 100}).json()["data"]
                assert data and all(user in item["content"] for item in data)
        assert (config.data_dir / "metrics.prom").exists()
    finally:
        server.should_exit = True
        thread.join(60)
    assert not thread.is_alive()
    assert service.pool.active() == 0

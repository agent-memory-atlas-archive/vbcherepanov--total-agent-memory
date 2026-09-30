"""Tests for the benchmark budget guard (docs/benchmarks/tam_bench_common).

The guard stands between a benchmark harness and the paid OpenAI API: a bug here either
leaks the key or spends past the owner's ceiling, so the accounting, the hard stop and the
key routing are tested against a local stub upstream. Nothing here touches the network
beyond 127.0.0.1.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "docs" / "benchmarks"
sys.path.insert(0, str(BENCH))

from tam_bench_common import budget_proxy as bp
from tam_bench_common.stub_openai import make_server

PRICE = bp.ModelPrice(input=1.75, cached_input=0.175, output=14.0)
DUMMY_KEY = "dummy-upstream-key"


def _post(url: str, body: dict, token: str) -> tuple[int, dict]:
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {token}",
                                              "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


@pytest.fixture
def stub_upstream(tmp_path):
    server = make_server(0, DUMMY_KEY, tmp_path / "stub.jsonl")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1", tmp_path / "stub.jsonl"
    server.shutdown()
    server.server_close()


def _proxy(upstream: str, tmp_path: Path, ceiling: float):
    ledger = bp.Ledger(ceiling_usd=ceiling, path=tmp_path / "ledger.jsonl")
    config = bp.ProxyConfig(upstream=upstream, api_key=DUMMY_KEY, session_token="session-token",
                            prices={"gpt-5.2": PRICE, "gpt-5-mini": bp.ModelPrice(0.25, 0.025, 2.0)})
    return bp.BudgetProxy(config, ledger).start(), ledger


# ── key file parsing ─────────────────────────────────────────────────


@pytest.mark.parametrize("text", [
    "OPENAI_API_KEY=sk-abc\n",
    "export OPENAI_API_KEY=sk-abc\n",
    'export OPENAI_API_KEY="sk-abc"\n',
    "# comment\n\nOTHER=1\nOPENAI_API_KEY='sk-abc'\n",
    "  OPENAI_API_KEY = sk-abc  \n",
])
def test_parse_env_file_accepts_export_and_quotes(text):
    assert bp.parse_env_file(text) == "sk-abc"


def test_parse_env_file_without_key_returns_none():
    assert bp.parse_env_file("OTHER=1\nOPENAI_API_KEY=\n") is None


def test_load_api_key_refuses_group_readable_file(tmp_path):
    key_file = tmp_path / "openai.env"
    key_file.write_text("OPENAI_API_KEY=sk-abc\n")
    os.chmod(key_file, 0o644)
    with pytest.raises(bp.BudgetError, match="chmod 600"):
        bp.load_api_key(key_file, {})
    os.chmod(key_file, 0o600)
    assert bp.load_api_key(key_file, {}) == ("sk-abc", f"file:{key_file}")


def test_load_api_key_falls_back_to_env_only_when_file_missing(tmp_path):
    assert bp.load_api_key(tmp_path / "missing.env", {"OPENAI_API_KEY": "sk-env"})[0] == "sk-env"
    with pytest.raises(bp.BudgetError, match="no API key"):
        bp.load_api_key(tmp_path / "missing.env", {})


def test_real_key_never_routes_to_a_non_openai_upstream(tmp_path):
    bp.check_key_routing(bp.OFFICIAL_UPSTREAM, None)
    with pytest.raises(bp.BudgetError, match="explicit --key-file"):
        bp.check_key_routing("http://127.0.0.1:9/v1", None)
    with pytest.raises(bp.BudgetError, match="explicit --key-file"):
        bp.check_key_routing("http://127.0.0.1:9/v1", bp.DEFAULT_KEY_FILE)
    with pytest.raises(bp.BudgetError, match="does not exist"):
        bp.check_key_routing("http://127.0.0.1:9/v1", tmp_path / "dummy.env")


def test_scrub_environment_drops_secret_like_names():
    env = bp.scrub_environment({"OPENAI_API_KEY": "x", "HF_TOKEN": "y", "MY_SECRET": "z", "PATH": "/bin"})
    assert env == {"PATH": "/bin"}


# ── pricing ──────────────────────────────────────────────────────────


def test_parse_usage_reads_responses_and_chat_shapes():
    responses = {"usage": {"input_tokens": 1000, "input_tokens_details": {"cached_tokens": 200},
                           "output_tokens": 50, "output_tokens_details": {"reasoning_tokens": 30}}}
    chat = {"usage": {"prompt_tokens": 1000, "completion_tokens": 50,
                      "prompt_tokens_details": {"cached_tokens": 200},
                      "completion_tokens_details": {"reasoning_tokens": 30}}}
    assert bp.parse_usage(responses) == bp.parse_usage(chat) == bp.Usage(1000, 200, 50, 30)
    assert bp.parse_usage({"usage": {"total_tokens": 3}}) is None
    assert bp.parse_usage({}) is None


def test_usage_cost_guard_is_upper_bound_of_list_price():
    guard, listed = bp.usage_cost(bp.Usage(1_000_000, 400_000, 100_000, 0), PRICE)
    assert guard == pytest.approx(1.75 + 1.4)
    assert listed == pytest.approx(0.6 * 1.75 + 0.4 * 0.175 + 1.4)
    assert listed < guard


def test_worst_case_bounds_input_by_body_bytes_and_output_by_cap():
    assert bp.worst_case_usd(1000, 2048, PRICE) == pytest.approx(
        ((1000 + bp.PER_REQUEST_INPUT_ALLOWANCE) * 1.75 + 2048 * 14.0) / 1e6)


def test_load_prices_requires_every_allowed_model(tmp_path):
    table = tmp_path / "prices.json"
    table.write_text(json.dumps({"models": {"gpt-5.2": {"input": 1.75, "cached_input": 0.175, "output": 14}}}))
    assert bp.load_prices(table, ["gpt-5.2"])["gpt-5.2"] == PRICE
    with pytest.raises(bp.BudgetError, match="no price"):
        bp.load_prices(table, ["gpt-5.2", "gpt-9"])


def test_shipped_price_table_has_the_pilot_models():
    prices = bp.load_prices(BENCH / "tam_bench_common" / "prices.json", ["gpt-5-mini", "gpt-5.2"])
    assert prices["gpt-5-mini"] == bp.ModelPrice(0.25, 0.025, 2.0)
    assert prices["gpt-5.2"] == PRICE


# ── ledger ───────────────────────────────────────────────────────────


def test_ledger_trips_before_the_ceiling_and_stays_tripped(tmp_path):
    trips = []
    ledger = bp.Ledger(ceiling_usd=1.0, path=tmp_path / "l.jsonl", on_trip=trips.append)
    assert ledger.reserve("m", 0.6) == (True, "")
    allowed, reason = ledger.reserve("m", 0.5)
    assert not allowed and "would exceed" in reason and len(trips) == 1
    ledger.settle(model="m", endpoint="/v1/responses", status=200, reserved_usd=0.6,
                  usage=bp.Usage(10, 0, 10, 0), price=PRICE, latency_s=0.1)
    assert ledger.reserve("m", 0.01)[0] is False
    summary = ledger.summary()
    assert summary["tripped"] and summary["refused"] == 2 and summary["in_flight_usd"] == 0
    assert summary["spent_usd_guard"] <= 1.0


def test_ledger_charges_reservation_when_usage_is_missing(tmp_path):
    ledger = bp.Ledger(ceiling_usd=1.0, path=tmp_path / "l.jsonl")
    ledger.reserve("m", 0.25)
    ledger.settle(model="m", endpoint="/v1/responses", status=200, reserved_usd=0.25, usage=None,
                  price=PRICE, latency_s=0.1, note="usage_missing")
    ledger.reserve("m", 0.25)
    ledger.settle(model="m", endpoint="/v1/responses", status=500, reserved_usd=0.25, usage=None,
                  price=PRICE, latency_s=0.1, note="upstream_error")
    assert ledger.summary()["spent_usd_guard"] == pytest.approx(0.25)


# ── proxy end to end against the stub ───────────────────────────────


def test_proxy_forwards_with_real_key_and_meters_usage(stub_upstream, tmp_path):
    upstream, stub_log = stub_upstream
    proxy, ledger = _proxy(upstream, tmp_path, ceiling=5.0)
    try:
        status, payload = _post(f"{proxy.base_url}/responses",
                                {"model": "gpt-5-mini", "input": "x" * 400, "max_output_tokens": 100},
                                "session-token")
        assert status == 200 and payload["usage"]["input_tokens"] == 100
        status, payload = _post(f"{proxy.base_url}/chat/completions",
                                {"model": "gpt-5.2", "messages": [{"role": "user", "content": "y" * 40}],
                                 "max_completion_tokens": 100}, "session-token")
        assert status == 200 and "choices" in payload
    finally:
        proxy.stop()
    assert [json.loads(line)["key_ok"] for line in stub_log.read_text().splitlines()] == [True, True]
    entries = [json.loads(line) for line in (tmp_path / "ledger.jsonl").read_text().splitlines()]
    assert [entry["model"] for entry in entries] == ["gpt-5-mini", "gpt-5.2"]
    assert all(entry["usd"] > 0 and entry["output_tokens"] > 0 for entry in entries)
    assert ledger.summary()["spent_usd_guard"] == pytest.approx(sum(entry["usd"] for entry in entries))
    assert DUMMY_KEY not in (tmp_path / "ledger.jsonl").read_text()


@pytest.mark.parametrize("body,token,status,code", [
    ({"model": "gpt-5-mini", "input": "x", "max_output_tokens": 5}, "wrong", 401, "proxy_auth"),
    ({"model": "gpt-4o", "input": "x", "max_output_tokens": 5}, "session-token", 400, "model_not_allowed"),
    ({"model": "gpt-5-mini", "input": "x"}, "session-token", 400, "unbounded_request"),
    ({"model": "gpt-5-mini", "input": "x", "max_output_tokens": 5, "stream": True}, "session-token", 400,
     "stream_not_allowed"),
])
def test_proxy_refuses_what_it_cannot_meter(stub_upstream, tmp_path, body, token, status, code):
    upstream, stub_log = stub_upstream
    proxy, _ = _proxy(upstream, tmp_path, ceiling=5.0)
    try:
        got_status, payload = _post(f"{proxy.base_url}/responses", body, token)
    finally:
        proxy.stop()
    assert (got_status, payload["error"]["code"]) == (status, code)
    assert not stub_log.exists()


def test_proxy_returns_402_and_never_forwards_past_the_ceiling(stub_upstream, tmp_path):
    upstream, stub_log = stub_upstream
    # a gpt-5.2 request with a 1000-token cap is reserved at ~$0.0146 and the stub bills
    # ~$0.001, so the fourth request's worst case no longer fits under $0.017
    proxy, ledger = _proxy(upstream, tmp_path, ceiling=0.017)
    body = {"model": "gpt-5.2", "input": "q", "max_output_tokens": 1000}
    try:
        statuses = [_post(f"{proxy.base_url}/responses", body, "session-token")[0] for _ in range(5)]
    finally:
        proxy.stop()
    assert statuses[0] == 200 and 402 in statuses
    assert statuses[statuses.index(402):] == [402] * (5 - statuses.index(402))
    assert len(stub_log.read_text().splitlines()) == statuses.index(402)
    assert ledger.summary()["spent_usd_guard"] <= 0.017


def test_openai_sdk_through_proxy_does_not_retry_a_budget_stop(stub_upstream, tmp_path):
    openai = pytest.importorskip("openai")
    upstream, stub_log = stub_upstream
    proxy, _ = _proxy(upstream, tmp_path, ceiling=0.0001)
    try:
        client = openai.OpenAI(base_url=proxy.base_url, api_key="session-token", max_retries=5)
        with pytest.raises(openai.APIStatusError) as excinfo:
            client.responses.create(model="gpt-5.2", input="hello", max_output_tokens=500)
    finally:
        proxy.stop()
    assert excinfo.value.status_code == 402
    assert not stub_log.exists()


# ── launcher ─────────────────────────────────────────────────────────


def _run_guarded(tmp_path: Path, upstream: str, ceiling: str, child_code: str) -> subprocess.CompletedProcess:
    key_file = tmp_path / "dummy.env"
    key_file.write_text(f'export OPENAI_API_KEY="{DUMMY_KEY}"\n')
    os.chmod(key_file, 0o600)
    env = {**os.environ, "SOME_API_KEY": "must-not-reach-child"}
    return subprocess.run(
        [sys.executable, str(BENCH / "tam_bench_common" / "run_guarded.py"), "--ceiling-usd", ceiling,
         "--allow-model", "gpt-5.2", "--out-dir", str(tmp_path / "run"), "--upstream", upstream,
         "--key-file", str(key_file), "--grace-s", "5", "--", sys.executable, "-c", child_code, "{PROXY_URL}"],
        capture_output=True, text=True, timeout=120, env=env, check=False)


CHILD = """
import json, os, sys, time, urllib.request, urllib.error
assert "SOME_API_KEY" not in os.environ and os.environ["OPENAI_API_KEY"].startswith("tam-bench-")
assert sys.argv[1] == os.environ["OPENAI_BASE_URL"]
body = json.dumps({"model": "gpt-5.2", "input": "q", "max_output_tokens": 1000}).encode()
for _ in range(COUNT):
    req = urllib.request.Request(sys.argv[1] + "/responses", data=body, method="POST",
        headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"], "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except urllib.error.HTTPError as exc:
        print("status", exc.code, flush=True)
time.sleep(SLEEP)
"""


def test_run_guarded_completes_and_writes_summary(stub_upstream, tmp_path):
    upstream, _ = stub_upstream
    done = _run_guarded(tmp_path, upstream, "1", CHILD.replace("COUNT", "2").replace("SLEEP", "0"))
    assert done.returncode == 0, done.stderr
    summary = json.loads((tmp_path / "run" / "budget" / "summary.json").read_text())
    assert summary["requests"] == 2 and not summary["budget_stop"] and summary["spent_usd_guard"] > 0
    assert DUMMY_KEY not in done.stderr + done.stdout


def test_run_guarded_stops_the_command_on_budget_trip(stub_upstream, tmp_path):
    upstream, _ = stub_upstream
    done = _run_guarded(tmp_path, upstream, "0.017", CHILD.replace("COUNT", "5").replace("SLEEP", "60"))
    assert done.returncode == 3, done.stderr
    assert "status 402" in done.stdout
    summary = json.loads((tmp_path / "run" / "budget" / "summary.json").read_text())
    assert summary["budget_stop"] and summary["spent_usd_guard"] <= 0.017
    assert summary["wall_seconds"] < 60


def test_run_guarded_refuses_default_key_with_foreign_upstream(tmp_path):
    done = subprocess.run(
        [sys.executable, str(BENCH / "tam_bench_common" / "run_guarded.py"), "--ceiling-usd", "1",
         "--allow-model", "gpt-5.2", "--out-dir", str(tmp_path / "run"), "--upstream", "http://127.0.0.1:9/v1",
         "--", sys.executable, "-c", "pass"], capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 2 and "explicit --key-file" in done.stderr

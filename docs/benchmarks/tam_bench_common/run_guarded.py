#!/usr/bin/env python3
"""Run a benchmark command behind the OpenAI budget proxy.

    python run_guarded.py --ceiling-usd 25 --allow-model gpt-5-mini --allow-model gpt-5.2 \
        --out-dir RUN_DIR -- python src/run.py ...

What it does:
1. Loads the API key from --key-file (default ~/.config/tam-bench/openai.env, mode 0600,
   one `OPENAI_API_KEY=...` line, optional `export` and quotes) or, if that file does not
   exist, from OPENAI_API_KEY. The key is never printed or written anywhere.
2. Starts the metering proxy on 127.0.0.1 (random port) with the dollar ceiling.
3. Runs the command in its own process group with a scrubbed environment: every
   *API_KEY* / *TOKEN* / *SECRET* / *PASSWORD* variable removed, then OPENAI_API_KEY set
   to a random session token and OPENAI_BASE_URL to the proxy. The literal `{PROXY_URL}`
   in the command's arguments is replaced by the proxy URL.
4. If the proxy trips, the command's process group gets SIGTERM (SIGKILL after a grace
   period) and this script exits with code 3.
5. Writes RUN_DIR/budget/ledger.jsonl (one line per API call: tokens, $, latency; no
   prompt text) and RUN_DIR/budget/summary.json (totals, wall time, exit status).
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tam_bench_common.budget_proxy import (
    DEFAULT_KEY_FILE,
    OFFICIAL_UPSTREAM,
    BudgetError,
    BudgetProxy,
    Ledger,
    ProxyConfig,
    check_key_routing,
    ensure_private_dir,
    load_api_key,
    load_prices,
    scrub_environment,
)

EXIT_BUDGET_STOP = 3
POLL_INTERVAL_S = 0.5
DEFAULT_PRICES = Path(__file__).resolve().parent / "prices.json"


def log(event: str, **fields) -> None:
    print(json.dumps({"ts": datetime.now(UTC).isoformat(), "component": "budget-guard",
                      "event": event, **fields}), file=sys.stderr, flush=True)


def stop_group(process: subprocess.Popen, grace_s: float) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        process.wait()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ceiling-usd", type=float, required=True)
    parser.add_argument("--allow-model", action="append", required=True, dest="allowed_models")
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--upstream", default=OFFICIAL_UPSTREAM)
    parser.add_argument("--key-file", type=Path, default=None,
                        help=f"env file with OPENAI_API_KEY=... (default {DEFAULT_KEY_FILE})")
    parser.add_argument("--prices", type=Path, default=DEFAULT_PRICES)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--upstream-timeout-s", type=float, default=900.0)
    parser.add_argument("--grace-s", type=float, default=20.0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        parser.error("missing command after --")
    return args


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    key_file = args.key_file.expanduser() if args.key_file is not None else None
    try:
        check_key_routing(args.upstream, key_file)
        prices = load_prices(args.prices, args.allowed_models)
        api_key, key_source = load_api_key(key_file if key_file is not None else DEFAULT_KEY_FILE,
                                           dict(os.environ))
    except BudgetError as exc:
        log("config_error", error=str(exc))
        return 2

    budget_dir = ensure_private_dir(args.out_dir / "budget")
    ledger_path = budget_dir / "ledger.jsonl"
    ledger_path.write_text("", encoding="utf-8")
    ledger = Ledger(ceiling_usd=args.ceiling_usd, path=ledger_path)
    session_token = "tam-bench-" + secrets.token_urlsafe(24)
    proxy = BudgetProxy(ProxyConfig(upstream=args.upstream, api_key=api_key, session_token=session_token,
                                    prices=prices, timeout_s=args.upstream_timeout_s), ledger, port=args.port).start()
    del api_key

    command = [part.replace("{PROXY_URL}", proxy.base_url) for part in args.command]
    env = scrub_environment(dict(os.environ))
    env.update({"OPENAI_API_KEY": session_token, "OPENAI_BASE_URL": proxy.base_url,
                "TAM_BENCH_PROXY_URL": proxy.base_url})
    log("start", upstream=args.upstream, key_source=key_source, ceiling_usd=args.ceiling_usd,
        allowed_models=sorted(prices), proxy=proxy.base_url, prices_file=str(args.prices), command=command)

    started_wall = datetime.now(UTC).isoformat()
    started = time.monotonic()
    process = subprocess.Popen(command, env=env, start_new_session=True)
    received_signal = {"value": None}

    def forward(signum, _frame):
        received_signal["value"] = signum
        stop_group(process, args.grace_s)

    previous = {sig: signal.signal(sig, forward) for sig in (signal.SIGINT, signal.SIGTERM)}
    budget_stop = False
    try:
        while process.poll() is None:
            if ledger.summary()["tripped"]:
                budget_stop = True
                log("budget_stop", reason=ledger.tripped_reason)
                stop_group(process, args.grace_s)
                break
            time.sleep(POLL_INTERVAL_S)
        returncode = process.wait()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        proxy.stop()
    if not budget_stop and ledger.summary()["tripped"]:
        budget_stop = True

    wall_s = time.monotonic() - started
    summary = {
        "started_utc": started_wall,
        "finished_utc": datetime.now(UTC).isoformat(),
        "wall_seconds": round(wall_s, 1),
        "command": command,
        "upstream": args.upstream,
        "command_returncode": returncode,
        "budget_stop": budget_stop,
        "signal": received_signal["value"],
        "prices_file": str(args.prices),
        **ledger.summary(),
    }
    (budget_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    log("finish", wall_seconds=summary["wall_seconds"], spent_usd_guard=summary["spent_usd_guard"],
        spent_usd_list=summary["spent_usd_list"], requests=summary["requests"], budget_stop=budget_stop,
        command_returncode=returncode)
    if budget_stop:
        return EXIT_BUDGET_STOP
    return returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

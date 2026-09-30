#!/usr/bin/env python3
"""Local OpenAI-compatible stub for zero-cost dry runs. It is not a model.

Serves POST /v1/responses and /v1/chat/completions on 127.0.0.1 and answers with
deterministic text plus a non-zero `usage` block (input ~ prompt chars / 4, output ~
reply chars / 4 plus a fixed number of simulated reasoning tokens), so the budget proxy's
accounting and hard stop are exercised exactly as with the real API.

Replies:
- AMA-Bench answer prompts: `Answer[1]: <first 300 chars of the retrieved steps>`.
- AMA-Bench judge prompts: `yes` if token-F1(predicted, reference) >= 0.5, else `no`.
- LongMemEval-V2 judge prompts: `{"label": 0, "reason": "stub"}`.
- anything else (e.g. a LongMemEval-V2 reader prompt): `stub \\boxed{UNKNOWN}`.
Scores produced through this stub are meaningless by construction.

With --expect-key-file, requests whose bearer token differs from the key in that env
file get 401: a dry run proves that the proxy injects the key and the harness never
sends its own.
"""

from __future__ import annotations

import argparse
import hmac
import json
import re
import sys
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tam_bench_common.budget_proxy import parse_env_file

AMA_JUDGE = re.compile(r"Reference Answer: (.*?)\n\nPredicted Answer: (.*?)\n\nIs the predicted answer correct\?", re.DOTALL)
AMA_STEPS = re.compile(r"## Retrieved trajectory steps \(in trajectory order\)\n(.*?)(?:\n\n## Questions|\Z)", re.DOTALL)
LME_JUDGE_MARKER = '"label": 0 or 1'
SIMULATED_REASONING_TOKENS = 64
CHARS_PER_TOKEN = 4


def token_f1(predicted: str, reference: str) -> float:
    pred = re.findall(r"\w+", predicted.lower())
    ref = re.findall(r"\w+", reference.lower())
    if not pred or not ref:
        return float(pred == ref)
    overlap = sum((Counter(pred) & Counter(ref)).values())
    if overlap == 0:
        return 0.0
    precision, recall = overlap / len(pred), overlap / len(ref)
    return 2 * precision * recall / (precision + recall)


def reply_for(prompt: str) -> str:
    judge = AMA_JUDGE.search(prompt)
    if judge:
        return "yes" if token_f1(judge.group(2), judge.group(1)) >= 0.5 else "no"
    if LME_JUDGE_MARKER in prompt:
        return json.dumps({"label": 0, "reason": "stub"})
    steps = AMA_STEPS.search(prompt)
    if steps:
        return "Answer[1]: " + " ".join(steps.group(1).split())[:300]
    return "stub \\boxed{UNKNOWN}"


def prompt_text(path: str, body: dict[str, Any]) -> str:
    if path.endswith("/responses"):
        source = body.get("input")
        if isinstance(source, str):
            return source
        return json.dumps(source)
    parts = []
    for message in body.get("messages", []):
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.extend(item.get("text", "") for item in content if isinstance(item, dict))
    return "\n".join(parts)


def build_response(path: str, body: dict[str, Any], text: str, prompt: str) -> dict[str, Any]:
    input_tokens = max(1, len(prompt) // CHARS_PER_TOKEN)
    output_tokens = max(1, len(text) // CHARS_PER_TOKEN) + SIMULATED_REASONING_TOKENS
    model = body.get("model", "stub")
    if path.endswith("/responses"):
        return {
            "id": "resp_stub", "object": "response", "created_at": int(time.time()), "status": "completed",
            "model": model,
            "output": [{"type": "message", "id": "msg_stub", "status": "completed", "role": "assistant",
                        "content": [{"type": "output_text", "text": text, "annotations": []}]}],
            "usage": {"input_tokens": input_tokens, "input_tokens_details": {"cached_tokens": 0},
                      "output_tokens": output_tokens,
                      "output_tokens_details": {"reasoning_tokens": SIMULATED_REASONING_TOKENS},
                      "total_tokens": input_tokens + output_tokens},
        }
    return {
        "id": "chatcmpl_stub", "object": "chat.completion", "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": input_tokens, "completion_tokens": output_tokens,
                  "total_tokens": input_tokens + output_tokens,
                  "prompt_tokens_details": {"cached_tokens": 0},
                  "completion_tokens_details": {"reasoning_tokens": SIMULATED_REASONING_TOKENS}},
    }


def make_server(port: int, expected_key: str | None, log_path: Path | None) -> ThreadingHTTPServer:
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:
            return

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            path = self.path.split("?", 1)[0]
            supplied = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
            key_ok = expected_key is None or hmac.compare_digest(supplied.encode(), expected_key.encode())
            if log_path is not None:
                with lock, log_path.open("a", encoding="utf-8") as sink:
                    sink.write(json.dumps({"ts": time.time(), "path": path, "model": body.get("model"),
                                           "key_ok": key_ok}) + "\n")
            if not key_ok:
                self._send(401, {"error": {"message": "stub: unexpected bearer token", "type": "auth"}})
                return
            if path not in {"/v1/responses", "/v1/chat/completions"}:
                self._send(404, {"error": {"message": f"stub: {path} not served", "type": "not_found"}})
                return
            prompt = prompt_text(path, body)
            self._send(200, build_response(path, body, reply_for(prompt), prompt))

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--expect-key-file", type=Path, default=None)
    parser.add_argument("--log", type=Path, default=None, help="JSONL of received requests (no prompt text)")
    args = parser.parse_args()
    expected = parse_env_file(args.expect_key_file.read_text(encoding="utf-8")) if args.expect_key_file else None
    if args.expect_key_file and not expected:
        parser.error(f"{args.expect_key_file} has no OPENAI_API_KEY line")
    server = make_server(args.port, expected, args.log)
    print(json.dumps({"event": "stub_listening", "url": f"http://127.0.0.1:{args.port}/v1"}), flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())

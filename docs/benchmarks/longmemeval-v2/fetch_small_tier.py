#!/usr/bin/env python3
"""Download the text-only part of LongMemEval-V2 that the small tier needs.

The published trajectories.jsonl is 1.2 GB and covers the medium tier too. This
script streams it once from a pinned dataset revision, verifies the sha256 of the
whole stream against the dataset's checksums.sha256, and keeps only the
trajectories referenced by haystacks/lme_v2_small.json. Trajectory screenshot
archives (5.9 GB) are not downloaded: the TAM adapter indexes text only. Question
screenshots (29 small PNGs) are downloaded because the harness sends them to the
reader for the questions that have one.

Output layout (a valid --data-root for the harness's data helpers):

    <data-root>/questions.jsonl
    <data-root>/haystacks/lme_v2_small.json
    <data-root>/haystacks/lme_v2_medium.json
    <data-root>/question_screenshots/*.png
    <data-root>/trajectories.jsonl          # small-tier trajectories only
    <data-root>/FETCH_MANIFEST.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

DATASET_REPO = "xiaowu0162/longmemeval-v2"
# Dataset revision the adapter was built against (HF dataset sha, 2026-05-17).
DEFAULT_REVISION = "f152293e235517d504809563c833d7190b8c713b"
CHUNK_BYTES = 1 << 20
SMALL_FILES = (
    "questions.jsonl",
    "haystacks/lme_v2_small.json",
    "haystacks/lme_v2_medium.json",
    "SCHEMA.md",
    "LICENSE",
)
USER_AGENT = "tam-bench-fetch/1.0"


def resolve_url(revision: str, path: str) -> str:
    return f"https://huggingface.co/datasets/{DATASET_REPO}/resolve/{revision}/{path}"


def open_url(url: str, timeout_s: float):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(request, timeout=timeout_s)


def fetch_bytes(url: str, timeout_s: float) -> bytes:
    with open_url(url, timeout_s) as response:
        return response.read()


def parse_checksums(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            out[parts[1].strip().lstrip("*")] = parts[0].lower()
    return out


def write_verified(path: Path, payload: bytes, expected_sha: str | None) -> None:
    actual = hashlib.sha256(payload).hexdigest()
    if expected_sha is not None and actual != expected_sha:
        raise RuntimeError(f"sha256 mismatch for {path.name}: expected {expected_sha}, got {actual}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_bytes(payload)
    os.replace(tmp, path)


def small_tier_trajectory_ids(haystack_path: Path) -> set[str]:
    mapping = json.loads(haystack_path.read_text(encoding="utf-8"))
    if not isinstance(mapping, dict):
        raise TypeError(f"{haystack_path} is not a JSON object")
    return {trajectory_id for ids in mapping.values() for trajectory_id in ids}


def stream_filter_trajectories(url: str, keep: set[str], out_path: Path, expected_sha: str,
                               timeout_s: float) -> dict[str, int]:
    digest = hashlib.sha256()
    tmp = out_path.with_suffix(".jsonl.part")
    kept = 0
    seen = 0
    total_bytes = 0
    pending = b""
    started = time.monotonic()
    with open_url(url, timeout_s) as response, tmp.open("wb") as sink:
        while True:
            chunk = response.read(CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            total_bytes += len(chunk)
            pending += chunk
            lines = pending.split(b"\n")
            pending = lines.pop()
            for line in lines:
                if not line.strip():
                    continue
                seen += 1
                record_id = json.loads(line).get("id")
                if record_id in keep:
                    sink.write(line + b"\n")
                    kept += 1
            if total_bytes % (100 * CHUNK_BYTES) < CHUNK_BYTES:
                rate = total_bytes / max(time.monotonic() - started, 1e-6) / CHUNK_BYTES
                print(json.dumps({"event": "progress", "mb": total_bytes // CHUNK_BYTES,
                                  "mb_per_s": round(rate, 1), "kept": kept}), flush=True)
        if pending.strip():
            seen += 1
            if json.loads(pending).get("id") in keep:
                sink.write(pending.rstrip(b"\n") + b"\n")
                kept += 1
    actual = digest.hexdigest()
    if actual != expected_sha:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"sha256 mismatch for trajectories.jsonl: expected {expected_sha}, got {actual}")
    if kept != len(keep):
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"small tier references {len(keep)} trajectories, found {kept}")
    os.replace(tmp, out_path)
    return {"bytes_streamed": total_bytes, "trajectories_seen": seen, "trajectories_kept": kept}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-root", required=True, help="output directory (created if missing)")
    parser.add_argument("--revision", default=DEFAULT_REVISION)
    parser.add_argument("--timeout-s", type=float, default=120.0)
    args = parser.parse_args()

    data_root = Path(args.data_root).expanduser().resolve()
    data_root.mkdir(parents=True, exist_ok=True)
    checksums = parse_checksums(fetch_bytes(resolve_url(args.revision, "checksums.sha256"), args.timeout_s)
                                .decode("utf-8"))
    for relative in SMALL_FILES:
        target = data_root / relative
        expected = checksums.get(relative)
        if target.exists() and expected and hashlib.sha256(target.read_bytes()).hexdigest() == expected:
            continue
        write_verified(target, fetch_bytes(resolve_url(args.revision, relative), args.timeout_s), expected)
    screenshots = sorted(path for path in checksums if path.startswith("question_screenshots/"))
    for relative in screenshots:
        target = data_root / relative
        if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == checksums[relative]:
            continue
        write_verified(target, fetch_bytes(resolve_url(args.revision, relative), args.timeout_s),
                       checksums[relative])

    keep = small_tier_trajectory_ids(data_root / "haystacks" / "lme_v2_small.json")
    trajectories_path = data_root / "trajectories.jsonl"
    manifest_path = data_root / "FETCH_MANIFEST.json"
    if trajectories_path.exists() and manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("revision") == args.revision and manifest.get("trajectories_kept") == len(keep):
            print(json.dumps({"event": "already_present", **manifest}))
            return 0
    stats = stream_filter_trajectories(resolve_url(args.revision, "trajectories.jsonl"), keep, trajectories_path,
                                       checksums["trajectories.jsonl"], args.timeout_s)
    manifest = {
        "dataset": DATASET_REPO,
        "revision": args.revision,
        "tier": "small",
        "trajectories_sha256_full_file": checksums["trajectories.jsonl"],
        "question_screenshots": len(screenshots),
        "trajectory_screenshots": "not downloaded (text-only indexing)",
        **stats,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"event": "done", **manifest}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

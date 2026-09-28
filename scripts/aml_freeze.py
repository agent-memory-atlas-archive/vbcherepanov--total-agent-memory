#!/usr/bin/env python3
"""Write FROZEN.md for an AML Full run: commit, configuration hash, environment.

Run it on the evaluation host with the exact environment `tam-aml serve`
uses. It refuses a dirty working tree (a Full run must map to one public
commit) unless --allow-dirty is given for a smoke run. Secrets are never
written: variables whose name contains KEY, TOKEN, SECRET or PASSWORD are
listed by name only.

    python scripts/aml_freeze.py --run-label tam-textual-full-1 \
        --out docs/benchmarks/aml-cycle2/FROZEN.md
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from aml_adapter.cli import tam_worker_env
from aml_adapter.config import AdapterConfig, ConfigError
from aml_adapter.runtime import FORCED_TAM_ENV

SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD")
ENV_PREFIXES = ("AML_", "MEMORY_", "V9_", "USE_", "FASTEMBED_", "TAM_")
PACKAGES = ("fastembed", "onnxruntime", "numpy", "starlette", "uvicorn", "pydantic", "httpx", "mcp")


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()


def package_versions() -> dict[str, str]:
    versions = {}
    for name in PACKAGES:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "not installed"
    return versions


def environment() -> tuple[dict[str, str], list[str]]:
    visible, hidden = {}, []
    for name in sorted(os.environ):
        if not name.startswith(ENV_PREFIXES):
            continue
        if any(marker in name for marker in SECRET_MARKERS):
            hidden.append(name)
        else:
            visible[name] = os.environ[name]
    return visible, hidden


def embedding_settings() -> dict[str, object]:
    import config as tam_config

    provider = tam_config.get_embed_provider()
    return {
        "provider": provider,
        "model": tam_config.get_embed_model(provider),
        "api_base": tam_config.get_embed_api_base(provider),
        "dimensions": tam_config.get_embed_dimensions(provider),
        "batch_size": tam_config.get_embed_batch_size(provider),
        "max_retries": tam_config.get_embed_max_retries(),
        "api_key_present": bool(tam_config.get_embed_api_key(provider)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-label", required=True, help="the Run Label submitted to AML")
    parser.add_argument("--out", type=Path, default=ROOT / "docs/benchmarks/aml-cycle2/FROZEN.md")
    parser.add_argument("--allow-dirty", action="store_true", help="smoke runs only")
    args = parser.parse_args()
    try:
        adapter = AdapterConfig.from_env()
    except ConfigError as exc:
        parser.error(str(exc))
    commit = git("rev-parse", "HEAD")
    dirty = git("status", "--porcelain", "--untracked-files=no")
    if dirty and not args.allow_dirty:
        print("working tree has uncommitted changes; commit them or pass --allow-dirty for a smoke run",
              file=sys.stderr)
        return 1
    from version import VERSION

    embedding = embedding_settings()
    worker_env = {**FORCED_TAM_ENV, **tam_worker_env()}
    frozen = {"adapter": adapter.public_settings(), "embedding": embedding, "worker_env": worker_env}
    config_hash = hashlib.sha256(json.dumps(frozen, sort_keys=True, default=str).encode()).hexdigest()
    visible_env, hidden_env = environment()
    now = datetime.now(UTC).isoformat(timespec="seconds")
    lines = [
        "# AML cycle 2 — frozen configuration",
        "",
        f"- Run label: `{args.run_label}`",
        f"- Frozen at: {now}",
        f"- Commit: `{commit}`" + (" (DIRTY — smoke only)" if dirty else ""),
        f"- Package version: {VERSION}",
        f"- Configuration SHA-256: `{config_hash}`",
        f"- Python: {platform.python_version()} ({platform.python_implementation()})",
        f"- Platform: {platform.platform()}",
        "",
        "The hash covers the three JSON blocks below. Re-run this script on the evaluation host before",
        "submitting; any change to them changes the hash.",
        "",
        "## Adapter settings",
        "",
        "```json",
        json.dumps(adapter.public_settings(), indent=2, sort_keys=True, default=str),
        "```",
        "",
        "## Embedding",
        "",
        "```json",
        json.dumps(embedding, indent=2, sort_keys=True),
        "```",
        "",
        "## TAM settings forced in every worker",
        "",
        "```json",
        json.dumps(worker_env, indent=2, sort_keys=True),
        "```",
        "",
        "## Packages",
        "",
        *[f"- {name} {version}" for name, version in package_versions().items()],
        "",
        "## Environment (non-secret)",
        "",
        "```",
        *[f"{name}={value}" for name, value in visible_env.items()],
        "```",
        "",
        "Secret variables present (values withheld): " + (", ".join(hidden_env) if hidden_env else "none"),
        "",
    ]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"out": str(args.out), "commit": commit, "config_sha256": config_hash, "dirty": bool(dirty)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

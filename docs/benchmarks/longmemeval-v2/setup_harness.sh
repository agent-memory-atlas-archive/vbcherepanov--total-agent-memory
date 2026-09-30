#!/usr/bin/env bash
# Prepare a LongMemEval-V2 checkout and the small-tier text data for the TAM backend.
#
#   setup_harness.sh HARNESS_DIR DATA_ROOT HARNESS_PYTHON
#
# - clones github.com/xiaowu0162/LongMemEval-V2 at the pinned commit into HARNESS_DIR
#   (or verifies an existing checkout is at that commit). The harness is not patched:
#   run_tam.py registers the `tam` backend at runtime.
# - installs the harness requirements into the environment of HARNESS_PYTHON
#   (a Python >= 3.11 virtualenv created by the caller);
# - downloads the small-tier text data into DATA_ROOT with fetch_small_tier.py
#   (sha256-verified, no trajectory screenshots).
set -euo pipefail

LME_REPO_URL="https://github.com/xiaowu0162/LongMemEval-V2"
LME_COMMIT="2cc8c540bdb87fe6761629b585e727e1c4704520"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS_DIR="${1:?usage: setup_harness.sh HARNESS_DIR DATA_ROOT HARNESS_PYTHON}"
DATA_ROOT="${2:?usage: setup_harness.sh HARNESS_DIR DATA_ROOT HARNESS_PYTHON}"
HARNESS_PYTHON="${3:?usage: setup_harness.sh HARNESS_DIR DATA_ROOT HARNESS_PYTHON}"

if [ -d "$HARNESS_DIR/.git" ]; then
  head_commit="$(git -C "$HARNESS_DIR" rev-parse HEAD)"
  if [ "$head_commit" != "$LME_COMMIT" ]; then
    echo "error: $HARNESS_DIR is at $head_commit, expected $LME_COMMIT" >&2
    exit 1
  fi
else
  git clone --quiet "$LME_REPO_URL" "$HARNESS_DIR"
  git -C "$HARNESS_DIR" -c advice.detachedHead=false checkout --quiet "$LME_COMMIT"
fi

if command -v uv >/dev/null 2>&1; then
  uv pip install --python "$HARNESS_PYTHON" -q -r "$HERE/requirements-harness.txt"
else
  "$HARNESS_PYTHON" -m pip install -q -r "$HERE/requirements-harness.txt"
fi

"$HARNESS_PYTHON" "$HERE/fetch_small_tier.py" --data-root "$DATA_ROOT"
echo "{\"harness\": \"$HARNESS_DIR\", \"commit\": \"$LME_COMMIT\", \"data_root\": \"$DATA_ROOT\"}"

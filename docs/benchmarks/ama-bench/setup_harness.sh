#!/usr/bin/env bash
# Prepare an AMA-Bench checkout for the TAM method.
#
#   setup_harness.sh HARNESS_DIR [DATASET_TEST_DIR]
#
# - clones github.com/AMA-Bench/AMA-Bench at the pinned commit into HARNESS_DIR (or
#   verifies an existing checkout is at that commit);
# - symlinks this directory's tam_memory.py to HARNESS_DIR/src/method/tam_memory.py and
#   registers it in src/method_register.py as method `tam` (idempotent);
# - links DATASET_TEST_DIR (a directory holding open_end_qa_set.jsonl) as
#   HARNESS_DIR/dataset/test, or downloads the pinned dataset revision from Hugging Face.
# Every file is verified by sha256.
set -euo pipefail

AMA_REPO_URL="https://github.com/AMA-Bench/AMA-Bench"
AMA_COMMIT="ddfd319e0be33424288c13806f1eafc63e625b59"
AMA_DATASET_REPO="AMA-bench/AMA-bench"
AMA_DATASET_REVISION="a5777378066f53229a94557a7b192435cd027909"
AMA_TEST_SHA256="45c36052e1520d87ad9de4114f71c9df42d4aac9cf158c0c353e800b653d65ff"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS_DIR="${1:?usage: setup_harness.sh HARNESS_DIR [DATASET_TEST_DIR]}"
DATASET_TEST_DIR="${2:-}"

sha256_of() { shasum -a 256 "$1" | awk '{print $1}'; }

if [ -d "$HARNESS_DIR/.git" ]; then
  head_commit="$(git -C "$HARNESS_DIR" rev-parse HEAD)"
  if [ "$head_commit" != "$AMA_COMMIT" ]; then
    echo "error: $HARNESS_DIR is at $head_commit, expected $AMA_COMMIT" >&2
    exit 1
  fi
else
  git clone --quiet "$AMA_REPO_URL" "$HARNESS_DIR"
  git -C "$HARNESS_DIR" -c advice.detachedHead=false checkout --quiet "$AMA_COMMIT"
fi

target="$HARNESS_DIR/src/method/tam_memory.py"
if [ -e "$target" ] && [ ! -L "$target" ]; then
  echo "error: $target exists and is not a symlink; remove it first" >&2
  exit 1
fi
ln -sfn "$HERE/tam_memory.py" "$target"

python3 - "$HARNESS_DIR/src/method_register.py" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
entry = '    "tam": ("src.method.tam_memory", "TAMMethod"),\n'
anchor = '    "claude_code": ("src.method.agent_method", "ClaudeCodeAgentMethod"),\n'
if entry not in text:
    if anchor not in text:
        raise SystemExit(f"cannot find the method registry anchor in {path}")
    path.write_text(text.replace(anchor, anchor + entry), encoding="utf-8")
PY

test_file="$HARNESS_DIR/dataset/test/open_end_qa_set.jsonl"
if [ -n "$DATASET_TEST_DIR" ]; then
  mkdir -p "$HARNESS_DIR/dataset"
  if [ ! -e "$HARNESS_DIR/dataset/test" ]; then
    ln -s "$(cd "$DATASET_TEST_DIR" && pwd)" "$HARNESS_DIR/dataset/test"
  fi
elif [ ! -f "$test_file" ]; then
  python3 - "$HARNESS_DIR/dataset" "$AMA_DATASET_REPO" "$AMA_DATASET_REVISION" <<'PY'
import sys
from huggingface_hub import snapshot_download

snapshot_download(repo_id=sys.argv[2], repo_type="dataset", revision=sys.argv[3], local_dir=sys.argv[1],
                  allow_patterns=["test/open_end_qa_set.jsonl"])
PY
fi

actual="$(sha256_of "$test_file")"
if [ "$actual" != "$AMA_TEST_SHA256" ]; then
  echo "error: $test_file sha256 $actual, expected $AMA_TEST_SHA256" >&2
  exit 1
fi
echo "{\"harness\": \"$HARNESS_DIR\", \"commit\": \"$AMA_COMMIT\", \"method\": \"tam\", \"test_sha256\": \"$actual\"}"

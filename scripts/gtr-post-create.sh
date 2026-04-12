#!/usr/bin/env bash
set -euo pipefail

# gtr postCreate hook for ai-orchestra repository
# 環境変数: REPO_ROOT, WORKTREE_PATH, BRANCH (git-worktree-runner が渡す)

if [[ -z "${REPO_ROOT:-}" || -z "${WORKTREE_PATH:-}" ]]; then
  echo "[gtr-post-create] ERROR: REPO_ROOT or WORKTREE_PATH not set" >&2
  exit 1
fi

TARGET_SETTINGS="$WORKTREE_PATH/.claude/settings.local.json"
SOURCE_ORCHESTRA_JSON="$REPO_ROOT/.claude/orchestra.json"
ORCHESTRA_MANAGER="$REPO_ROOT/scripts/orchestra-manager.py"

if [[ -f "$TARGET_SETTINGS" ]]; then
  echo "[gtr-post-create] AI Orchestra already initialized, skipping"
  exit 0
fi

if [[ ! -f "$SOURCE_ORCHESTRA_JSON" ]]; then
  echo "[gtr-post-create] WARN: $SOURCE_ORCHESTRA_JSON not found, skipping"
  exit 0
fi

if [[ ! -f "$ORCHESTRA_MANAGER" ]]; then
  echo "[gtr-post-create] ERROR: $ORCHESTRA_MANAGER not found" >&2
  exit 1
fi

declare -a PACKAGES=()
while IFS= read -r line; do
  [[ -z "$line" ]] && continue
  PACKAGES+=("$line")
done < <(
  python3 - "$SOURCE_ORCHESTRA_JSON" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding="utf-8"))

for pkg in data.get("installed_packages") or []:
    if pkg:
        print(pkg)
PY
)

if [[ "${#PACKAGES[@]}" -eq 0 ]]; then
  echo "[gtr-post-create] WARN: installed_packages not found, skipping"
  exit 0
fi

echo "[gtr-post-create] Setting up AI Orchestra..."
AI_ORCHESTRA_DIR="$REPO_ROOT" \
  python3 "$ORCHESTRA_MANAGER" install "${PACKAGES[@]}" --project "$WORKTREE_PATH"
echo "[gtr-post-create] Done${BRANCH:+ (branch: $BRANCH)}"

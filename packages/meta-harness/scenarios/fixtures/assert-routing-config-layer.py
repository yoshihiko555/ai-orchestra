#!/usr/bin/env python3
"""Assert that the candidate's own routing-config patch is effective after layering."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

# `packages/meta-harness/lib/meta_harness_common.py` の CONFIG_PATCH_ALLOWLIST_CEILING /
# `packages/meta-harness/lib/promoter.py` の ROUTING_CONFIG_PATCH_FILE と同じ値。scenario
# fixture はサンドボックス化された scenario 実行環境から独立して動くため、
# packages/meta-harness/lib を import せずローカル定数として複製する。
ROUTING_CONFIG_PATCH_FILE = "agent-routing/cli-tools.yaml"
APPLIED_CONFIG_PATCH_RELATIVE = Path(".claude/meta-harness/applied-config-patch.json")


def _leaves(
    value: dict[str, Any], prefix: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], Any]]:
    for key, child in value.items():
        path = (*prefix, str(key))
        if isinstance(child, dict):
            yield from _leaves(child, path)
        else:
            yield path, child


def _get(value: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = value
    for segment in path:
        assert isinstance(current, dict) and segment in current, (
            f"missing merged key: {'.'.join(path)}"
        )
        current = current[segment]
    return current


def _is_allowlisted(path: tuple[str, ...]) -> bool:
    return path in (("codex", "model"), ("antigravity", "model")) or (
        len(path) == 3 and path[0] == "agents" and path[2] == "tool"
    )


def _patched_leaves(project_root: Path) -> list[tuple[tuple[str, ...], Any]] | None:
    """候補が実際に適用した config patch のキー一覧を読む。

    `applied-config-patch.json`（`evaluator.py` の `_apply_config_patch` が worktree 内に
    書き出す）が無ければ None を返し、呼び出し側で従来どおりの全リーフ走査にフォール
    バックする。
    """
    patch_path = project_root / APPLIED_CONFIG_PATCH_RELATIVE
    if not patch_path.is_file() or patch_path.is_symlink():
        return None
    items = json.loads(patch_path.read_text(encoding="utf-8"))
    assert isinstance(items, list), "applied-config-patch.json must contain a JSON array"
    leaves: list[tuple[tuple[str, ...], Any]] = []
    for item in items:
        if str(item.get("file")) != ROUTING_CONFIG_PATCH_FILE:
            continue
        key_path = str(item["key_path"])
        leaves.append((tuple(key_path.split(".")), item["value"]))
    return leaves


def main() -> None:
    project_root = Path(os.environ.get("AI_ORCHESTRA_DIR") or Path.cwd()).resolve()
    local_path = project_root / ".claude/config/agent-routing/cli-tools.local.yaml"
    assert local_path.is_file() and not local_path.is_symlink(), (
        f"missing local config: {local_path}"
    )
    local = yaml.safe_load(local_path.read_text(encoding="utf-8"))
    assert isinstance(local, dict), "local config must be a mapping"

    # 候補の config patch が worktree 内で読める場合は、そのキーだけを allowlist / 反映
    # 確認の対象に絞る。materialize 済みファイルの全リーフを対象にすると、プロジェクト
    # 固有の無関係な既存 local override が混在するだけでこの oracle が失敗してしまう
    # （候補の内容とは無関係な false failure、PR #252 R2-4 レビュー指摘）。artifact が
    # 無い場合は、従来どおり local override ファイルの全リーフを対象にする
    # （後方互換フォールバック）。
    patched_leaves = _patched_leaves(project_root)
    leaves = patched_leaves if patched_leaves is not None else list(_leaves(local))
    assert leaves, "local config must contain at least one patched value"
    assert all(_is_allowlisted(path) for path, _value in leaves), (
        "local config contains a non-allowlisted key"
    )

    sys.path.insert(0, str(project_root / "packages/core/hooks"))
    from hook_common import load_cli_tools_config

    merged = load_cli_tools_config(str(project_root))
    for path, expected in leaves:
        actual = _get(merged, path)
        assert actual == expected, (
            f"layering mismatch for {'.'.join(path)}: {actual!r} != {expected!r}"
        )


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
SessionStart hook: ai-orchestra パッケージの agents/config/hooks を自動同期する。

処理フロー:
1. .claude/orchestra.json を読み込み → インストール済みパッケージ一覧を取得
2. 各パッケージの manifest.json を読み込み → agents/config をコピー
3. 差分があるファイルのみ .claude/{agents,config}/ にコピー（mtime 比較）
4. config/*.local.yaml はプロジェクト固有設定のため同期・削除の対象外
5. 前回 synced_files にあって今回ないファイルを削除（ソース側で削除されたファイルの反映）
6. synced_files リストと last_sync タイムスタンプを更新
7. manifest.json の hooks と settings.local.json を比較し、不足/余剰 hook を同期

Note: skills/rules は facet build に完全委譲（packages からは同期しない）

パフォーマンス: 変更なしの場合 ~70ms（Python 起動 + mtime 比較のみ）
"""

import datetime
import json
import os
import sys
from pathlib import Path

# scripts/ ディレクトリをモジュール検索パスに追加（lib/ を解決するため）
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from lib.agent_model_patch import patch_all_agents  # noqa: E402
from lib.gitignore_sync import sync_gitignore as _sync_gitignore  # noqa: E402
from lib.scaffold import ensure_claude_scaffold, sync_claudeignore  # noqa: E402
from lib.sync_engine import (  # noqa: E402
    build_facets,
    collect_facet_managed_paths,
    remove_stale_files,
    sync_hooks,
    sync_packages,
)


def read_hook_input() -> dict:
    """stdin から JSON を読み取って dict を返す。"""
    try:
        return json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        return {}


def get_project_dir(data: dict) -> str:
    """hook 入力からプロジェクトディレクトリを取得"""
    cwd = data.get("cwd") or ""
    if cwd:
        return cwd
    return os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())


def main() -> None:
    data = read_hook_input()
    project_dir = Path(get_project_dir(data))

    # orchestra.json を読み込み
    orch_path = project_dir / ".claude" / "orchestra.json"
    if not orch_path.exists():
        return

    try:
        with open(orch_path, encoding="utf-8") as f:
            orch = json.load(f)
    except (json.JSONDecodeError, OSError):
        return

    installed_packages = orch.get("installed_packages", [])
    orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")

    if not orchestra_dir:
        return

    orchestra_path = Path(orchestra_dir).resolve()
    if not orchestra_path.is_dir():
        return

    scaffolded_count = ensure_claude_scaffold(project_dir, orchestra_path)
    if not installed_packages:
        if scaffolded_count > 0:
            print(f"[orchestra] {scaffolded_count} scaffolded")
        return

    claude_dir = project_dir / ".claude"

    # facet composition で管理される skill/rule パスを収集（sync スキップ対象）
    facet_managed = collect_facet_managed_paths(orchestra_path, project_dir)

    # パッケージ単位の同期
    synced_count, synced_files = sync_packages(
        claude_dir, orchestra_path, installed_packages, facet_managed
    )

    # ファセットビルド
    facet_built_count = build_facets(orchestra_path, project_dir, installed_packages)

    # 前回同期されたが今回は対象外のファイルを削除（facet 管理パスは除外）
    prev_synced = orch.get("synced_files", [])
    removed_count = remove_stale_files(claude_dir, prev_synced, synced_files, facet_managed)

    # サブエージェント model パッチ
    patched_count = patch_all_agents(project_dir)

    # orchestra.json を更新
    prev_set = set(prev_synced)
    needs_save = (
        synced_count > 0
        or removed_count > 0
        or patched_count > 0
        or synced_files != prev_set
        or "synced_files" not in orch
    )
    if needs_save:
        orch["last_sync"] = datetime.datetime.now(datetime.UTC).isoformat()
        orch["synced_files"] = sorted(synced_files)
        try:
            with open(orch_path, "w", encoding="utf-8") as f:
                json.dump(orch, f, indent=2, ensure_ascii=False)
                f.write("\n")
        except OSError:
            pass

    # hooks 同期
    hooks_changed = sync_hooks(project_dir, orchestra_path, installed_packages)

    # .claudeignore 同期
    claudeignore_updated = sync_claudeignore(project_dir, orchestra_path)

    # .gitignore 同期
    gitignore_updated = _sync_gitignore(project_dir)

    # SessionStart hook の stdout は Claude コンテキストに注入される
    if (
        synced_count > 0
        or removed_count > 0
        or hooks_changed > 0
        or claudeignore_updated
        or gitignore_updated
        or scaffolded_count > 0
        or patched_count > 0
        or facet_built_count > 0
    ):
        parts = []
        if scaffolded_count > 0:
            parts.append(f"{scaffolded_count} scaffolded")
        if synced_count > 0:
            parts.append(f"{synced_count} synced")
        if removed_count > 0:
            parts.append(f"{removed_count} removed")
        if hooks_changed > 0:
            parts.append(f"{hooks_changed} hooks synced")
        if claudeignore_updated:
            parts.append(".claudeignore updated")
        if patched_count > 0:
            parts.append(f"{patched_count} agent models patched")
        if facet_built_count > 0:
            parts.append(f"{facet_built_count} facets built")
        print(f"[orchestra] {', '.join(parts)}")


if __name__ == "__main__":
    main()

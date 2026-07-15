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
import tomllib
from pathlib import Path

# scripts/ ディレクトリをモジュール検索パスに追加（lib/ を解決するため）
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from lib.agent_model_patch import patch_all_agents_paths  # noqa: E402
from lib.gitignore_sync import sync_gitignore as _sync_gitignore  # noqa: E402
from lib.scaffold import ensure_claude_scaffold  # noqa: E402
from lib.sync_engine import (  # noqa: E402
    apply_codex_harness_config,
    build_facets,
    collect_facet_managed_paths,
    collect_managed_agent_stems,
    refresh_patched_agent_hashes,
    remove_stale_files,
    sync_codex_files,
    sync_hooks,
    sync_packages,
)
from lib.toml_merge import TomlMergeError  # noqa: E402

# リネームされたパッケージの読み替え表（旧名 → 新名）
# 横展開先の orchestra.json に旧名が残っていても自動移行する
RENAMED_PACKAGES = {
    "gemini-suggestions": "antigravity-suggestions",
}


def migrate_installed_packages(packages: list[str]) -> tuple[list[str], bool]:
    """installed_packages の旧パッケージ名を新名に読み替える。

    Args:
        packages: orchestra.json の installed_packages。

    Returns:
        (移行後リスト, 変更有無)。重複は除去し元の順序を保つ。
    """
    migrated: list[str] = []
    changed = False
    for name in packages:
        new_name = RENAMED_PACKAGES.get(name, name)
        if new_name != name:
            changed = True
        if new_name not in migrated:
            migrated.append(new_name)
        else:
            changed = True
    return migrated, changed


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

    installed_packages, packages_migrated = migrate_installed_packages(
        orch.get("installed_packages", [])
    )
    if packages_migrated:
        orch["installed_packages"] = installed_packages
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

    # パッケージ単位の同期（agents は orch の file_hashes 台帳でユーザー編集を保護）
    synced_count, synced_files = sync_packages(
        claude_dir, orchestra_path, installed_packages, facet_managed, orch
    )

    # codex_files（.codex/ 配下配布物）の同期（ハッシュ保護付き、強制上書きなし）
    codex_synced_count = sync_codex_files(project_dir, orchestra_path, installed_packages, orch)
    synced_count += codex_synced_count

    # codex-harness の config.toml マージ（default_permissions / [permissions.*] 等）
    # マージ失敗（不正 TOML 生成 / 読み書き失敗）は SessionStart 同期全体を
    # 巻き添えにせず、警告を出してスキップする（fail-soft）。
    try:
        config_updated = apply_codex_harness_config(project_dir, orchestra_path, installed_packages)
    except (TomlMergeError, tomllib.TOMLDecodeError, OSError) as e:
        print(
            f"[warn] .codex/config.toml マージに失敗したためスキップしました: {e}", file=sys.stderr
        )
        config_updated = False
    if config_updated:
        print("[orchestra] .codex/config.toml updated with codex-harness settings")

    # ファセットビルド
    facet_built_count = build_facets(orchestra_path, project_dir, installed_packages)

    # 前回同期されたが今回は対象外のファイルを削除（facet 管理パスは除外）
    prev_synced = orch.get("synced_files", [])
    removed_count = remove_stale_files(claude_dir, prev_synced, synced_files, facet_managed)

    # サブエージェント model パッチ（インストール済みパッケージ宣言分のみ）
    managed_agent_stems = collect_managed_agent_stems(orchestra_path, installed_packages)
    patched_paths = patch_all_agents_paths(project_dir, managed_agent_stems)
    patched_count = len(patched_paths)
    # パッチ後の内容で file_hashes 台帳を更新し直す（PR #244: is_user_modified の誤判定防止）
    if patched_paths:
        refresh_patched_agent_hashes(orch, claude_dir, patched_paths)

    # orchestra.json を更新
    prev_set = set(prev_synced)
    needs_save = (
        synced_count > 0
        or removed_count > 0
        or patched_count > 0
        or packages_migrated
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

    # .gitignore 同期
    gitignore_updated = _sync_gitignore(project_dir)

    # SessionStart hook の stdout は Claude コンテキストに注入される
    if (
        synced_count > 0
        or removed_count > 0
        or hooks_changed > 0
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
        if patched_count > 0:
            parts.append(f"{patched_count} agent models patched")
        if facet_built_count > 0:
            parts.append(f"{facet_built_count} facets built")
        print(f"[orchestra] {', '.join(parts)}")


if __name__ == "__main__":
    main()

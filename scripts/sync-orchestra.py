#!/usr/bin/env python3
"""
SessionStart hook: ai-orchestra パッケージの skills/agents/rules を自動同期する。

処理フロー:
1. .claude/orchestra.json を読み込み → インストール済みパッケージ一覧を取得
2. 各パッケージの manifest.json を読み込み → skills/agents/rules をコピー
3. sync_top_level=true の場合、$AI_ORCHESTRA_DIR 直下の agents/skills/rules もコピー
4. 差分があるファイルのみ .claude/{skills,agents,rules}/ にコピー（mtime 比較）
5. last_sync タイムスタンプを更新

パフォーマンス: 変更なしの場合 ~70ms（Python 起動 + mtime 比較のみ）
"""

import datetime
import json
import os
import shutil
import sys
from pathlib import Path


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


def needs_sync(src: Path, dst: Path) -> bool:
    """ソースがデスティネーションより新しいか、デスティネーションが存在しないか判定"""
    if not dst.exists():
        return True
    return src.stat().st_mtime > dst.stat().st_mtime


def sync_top_level(
    orchestra_path: Path, claude_dir: Path, existing_files: set[str]
) -> int:
    """$AI_ORCHESTRA_DIR 直下の agents/skills/rules を .claude/ に差分コピー。

    プロジェクト固有ファイル（同名）が既に存在する場合はスキップする。
    パッケージ同期で既にコピーされたファイルもスキップする。
    """
    synced = 0

    for category in ("agents", "skills", "rules"):
        src_dir = orchestra_path / category
        if not src_dir.is_dir():
            continue

        for src_file in src_dir.rglob("*"):
            if not src_file.is_file():
                continue

            rel_path = src_file.relative_to(src_dir)
            dst = claude_dir / category / rel_path

            # パッケージ同期で既にコピーされたファイルはスキップ
            dst_key = f"{category}/{rel_path}"
            if dst_key in existing_files:
                continue

            if not needs_sync(src_file, dst):
                continue

            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst)
            synced += 1

    return synced


def main() -> None:
    data = read_hook_input()
    project_dir = Path(get_project_dir(data))

    # orchestra.json を読み込み
    orch_path = project_dir / ".claude" / "orchestra.json"
    if not orch_path.exists():
        return

    try:
        with open(orch_path, "r", encoding="utf-8") as f:
            orch = json.load(f)
    except (json.JSONDecodeError, OSError):
        return

    installed_packages = orch.get("installed_packages", [])
    orchestra_dir = orch.get("orchestra_dir", "")

    if not orchestra_dir or not installed_packages:
        return

    orchestra_path = Path(orchestra_dir)
    if not orchestra_path.is_dir():
        return

    claude_dir = project_dir / ".claude"
    synced_count = 0
    synced_files: set[str] = set()

    # パッケージ単位の同期
    for pkg_name in installed_packages:
        manifest_path = orchestra_path / "packages" / pkg_name / "manifest.json"
        if not manifest_path.exists():
            continue

        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        pkg_dir = orchestra_path / "packages" / pkg_name

        for category in ("skills", "agents", "rules"):
            file_list = manifest.get(category, [])
            for rel_path in file_list:
                src = pkg_dir / category / rel_path
                dst = claude_dir / category / rel_path

                if not src.exists():
                    continue

                synced_files.add(f"{category}/{rel_path}")

                if not needs_sync(src, dst):
                    continue

                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                synced_count += 1

    # トップレベル同期（sync_top_level フラグが有効な場合）
    if orch.get("sync_top_level", False):
        synced_count += sync_top_level(orchestra_path, claude_dir, synced_files)

    # last_sync を更新（同期があった場合のみ）
    if synced_count > 0:
        orch["last_sync"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        try:
            with open(orch_path, "w", encoding="utf-8") as f:
                json.dump(orch, f, indent=2, ensure_ascii=False)
                f.write("\n")
        except OSError:
            pass

    # SessionStart hook の stdout は Claude コンテキストに注入される
    if synced_count > 0:
        print(f"[orchestra] {synced_count} files synced")


if __name__ == "__main__":
    main()

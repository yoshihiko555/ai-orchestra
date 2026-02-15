#!/usr/bin/env python3
"""
SessionStart hook: ai-orchestra パッケージの skills/agents/rules/config を自動同期する。

処理フロー:
1. .claude/orchestra.json を読み込み → インストール済みパッケージ一覧を取得
2. 各パッケージの manifest.json を読み込み → skills/agents/rules/config をコピー
3. 差分があるファイルのみ .claude/{skills,agents,rules,config}/ にコピー（mtime 比較）
4. config/*.local.yaml はプロジェクト固有設定のため同期・削除の対象外
5. 前回 synced_files にあって今回ないファイルを削除（ソース側で削除されたファイルの反映）
6. synced_files リストと last_sync タイムスタンプを更新

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


def is_local_override(category: str, rel_path: Path) -> bool:
    """プロジェクト固有の上書きファイル（*.local.yaml / *.local.json）かどうか判定"""
    name = rel_path.name
    return category == "config" and (
        name.endswith(".local.yaml") or name.endswith(".local.json")
    )


def remove_stale_files(
    claude_dir: Path, prev_synced: list[str], current_synced: set[str]
) -> int:
    """前回同期したが今回は対象外になったファイルを削除する。

    削除後に空になったディレクトリも再帰的に削除する。
    """
    removed = 0
    for file_key in prev_synced:
        if file_key in current_synced:
            continue
        # config/*.local.yaml はプロジェクト固有設定のため削除しない
        parts = file_key.split("/", 1)
        if len(parts) == 2 and is_local_override(parts[0], Path(parts[1])):
            continue
        target = claude_dir / file_key
        if target.is_file():
            target.unlink()
            removed += 1
            # 空ディレクトリを再帰的に削除
            parent = target.parent
            while parent != claude_dir and parent.is_dir():
                try:
                    parent.rmdir()  # 空でなければ OSError
                    parent = parent.parent
                except OSError:
                    break
    return removed


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

        for category in ("skills", "agents", "rules", "config"):
            file_list = manifest.get(category, [])
            for rel_path in file_list:
                # rel_path はカテゴリプレフィックスを含む (例: "config/flags.json")
                src = pkg_dir / rel_path
                if not src.exists():
                    continue

                if src.is_dir():
                    # ディレクトリの場合: 中身を再帰的に展開して個別コピー
                    for src_file in src.rglob("*"):
                        if not src_file.is_file():
                            continue
                        file_rel = str(src_file.relative_to(pkg_dir))
                        synced_files.add(file_rel)
                        dst = claude_dir / file_rel
                        if not needs_sync(src_file, dst):
                            continue
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src_file, dst)
                        synced_count += 1
                else:
                    if category == "config":
                        # config はパッケージ名サブディレクトリに配置
                        filename = Path(rel_path).name
                        dst = claude_dir / "config" / pkg_name / filename
                        dst_key = f"config/{pkg_name}/{filename}"
                    else:
                        dst = claude_dir / rel_path
                        dst_key = rel_path

                    synced_files.add(dst_key)

                    if not needs_sync(src, dst):
                        continue

                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    synced_count += 1

    # 前回同期されたが今回は対象外のファイルを削除
    # synced_files キーが未設定（初回）の場合は削除しない（プロジェクト固有ファイルの誤削除を防止）
    prev_synced = orch.get("synced_files", [])
    removed_count = remove_stale_files(claude_dir, prev_synced, synced_files)

    # orchestra.json を更新（同期・削除があった場合、synced_files が変わった場合、または初回記録時）
    prev_set = set(prev_synced)
    needs_save = (
        synced_count > 0
        or removed_count > 0
        or synced_files != prev_set
        or "synced_files" not in orch
    )
    if needs_save:
        orch["last_sync"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        orch["synced_files"] = sorted(synced_files)
        try:
            with open(orch_path, "w", encoding="utf-8") as f:
                json.dump(orch, f, indent=2, ensure_ascii=False)
                f.write("\n")
        except OSError:
            pass

    # SessionStart hook の stdout は Claude コンテキストに注入される
    if synced_count > 0 or removed_count > 0:
        parts = []
        if synced_count > 0:
            parts.append(f"{synced_count} synced")
        if removed_count > 0:
            parts.append(f"{removed_count} removed")
        print(f"[orchestra] {', '.join(parts)}")


if __name__ == "__main__":
    main()

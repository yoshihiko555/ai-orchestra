#!/usr/bin/env python3
"""
SessionStart hook: ai-orchestra パッケージの skills/agents/rules/config/hooks を自動同期する。

処理フロー:
1. .claude/orchestra.json を読み込み → インストール済みパッケージ一覧を取得
2. 各パッケージの manifest.json を読み込み → skills/agents/rules/config をコピー
3. 差分があるファイルのみ .claude/{skills,agents,rules,config}/ にコピー（mtime 比較）
4. config/*.local.yaml はプロジェクト固有設定のため同期・削除の対象外
5. 前回 synced_files にあって今回ないファイルを削除（ソース側で削除されたファイルの反映）
6. synced_files リストと last_sync タイムスタンプを更新
7. manifest.json の hooks と settings.local.json を比較し、不足/余剰 hook を同期

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
    return category == "config" and (name.endswith(".local.yaml") or name.endswith(".local.json"))


def remove_stale_files(claude_dir: Path, prev_synced: list[str], current_synced: set[str]) -> int:
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


def _get_hook_command(pkg_name: str, filename: str) -> str:
    """フックコマンド文字列を生成（orchestra-manager.py と同じ形式）"""
    return f'python3 "$AI_ORCHESTRA_DIR/packages/{pkg_name}/hooks/{filename}"'


def _parse_hook_entry(value: object) -> tuple[str, str | None]:
    """manifest.json の hooks 値から (file, matcher) を取得"""
    if isinstance(value, str):
        return value, None
    if isinstance(value, dict):
        return value["file"], value.get("matcher")
    return "", None


def _find_hook_in_settings(
    settings_hooks: dict, event: str, command: str, matcher: str | None
) -> bool:
    """settings.local.json に指定 hook が登録済みか判定"""
    for entry in settings_hooks.get(event, []):
        if matcher:
            if entry.get("matcher") != matcher:
                continue
        else:
            if "matcher" in entry:
                continue
        for hook in entry.get("hooks", []):
            if hook.get("command") == command:
                return True
    return False


def _add_hook_to_settings(
    settings_hooks: dict,
    event: str,
    command: str,
    matcher: str | None,
    timeout: int = 5,
) -> None:
    """settings.local.json に hook を追加"""
    if event not in settings_hooks:
        settings_hooks[event] = []

    hook_obj = {"type": "command", "command": command, "timeout": timeout}

    target_entry = None
    for entry in settings_hooks[event]:
        if matcher:
            if entry.get("matcher") == matcher:
                target_entry = entry
                break
        else:
            if "matcher" not in entry:
                target_entry = entry
                break

    if target_entry is None:
        target_entry = {"hooks": []}
        if matcher:
            target_entry["matcher"] = matcher
        settings_hooks[event].append(target_entry)

    for hook in target_entry["hooks"]:
        if hook.get("command") == command:
            return

    target_entry["hooks"].append(hook_obj)


def _remove_hook_from_settings(
    settings_hooks: dict,
    event: str,
    command: str,
    matcher: str | None,
) -> None:
    """settings.local.json から hook を削除"""
    if event not in settings_hooks:
        return

    for entry in settings_hooks[event]:
        if matcher:
            if entry.get("matcher") != matcher:
                continue
        else:
            if "matcher" in entry:
                continue
        entry["hooks"] = [h for h in entry.get("hooks", []) if h.get("command") != command]

    # hooks が空になったエントリを除去
    settings_hooks[event] = [e for e in settings_hooks[event] if e.get("hooks")]


def _is_orchestra_hook(command: str) -> bool:
    """コマンドが $AI_ORCHESTRA_DIR/packages/*/hooks/* パターンか判定"""
    return command.startswith('python3 "$AI_ORCHESTRA_DIR/packages/') and "/hooks/" in command


def _parse_pkg_from_command(command: str) -> str | None:
    """hook コマンドからパッケージ名を抽出"""
    # python3 "$AI_ORCHESTRA_DIR/packages/{pkg}/hooks/{file}"
    prefix = 'python3 "$AI_ORCHESTRA_DIR/packages/'
    if not command.startswith(prefix):
        return None
    rest = command[len(prefix) :]
    slash_idx = rest.find("/")
    if slash_idx < 0:
        return None
    return rest[:slash_idx]


def sync_hooks(
    project_dir: Path,
    orchestra_path: Path,
    installed_packages: list[str],
) -> int:
    """manifest.json の hooks と settings.local.json を比較し差分を同期する。

    Returns:
        変更があった hook 数（追加 + 削除）
    """
    settings_path = project_dir / ".claude" / "settings.local.json"
    if not settings_path.exists():
        return 0

    try:
        with open(settings_path, encoding="utf-8") as f:
            settings = json.load(f)
    except (json.JSONDecodeError, OSError):
        return 0

    settings_hooks = settings.get("hooks", {})

    # sync-orchestra 自身の SessionStart hook コマンド（同期対象外）
    sync_hook_command = 'python3 "$AI_ORCHESTRA_DIR/scripts/sync-orchestra.py"'

    # 1. manifest から期待される hook 一覧を構築
    # key: (event, command, matcher)
    expected_hooks: set[tuple[str, str, str | None]] = set()
    installed_set = set(installed_packages)

    for pkg_name in installed_packages:
        manifest_path = orchestra_path / "packages" / pkg_name / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            with open(manifest_path, encoding="utf-8") as f:
                manifest = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        for event, entries in manifest.get("hooks", {}).items():
            for raw_entry in entries:
                filename, matcher = _parse_hook_entry(raw_entry)
                if not filename:
                    continue
                command = _get_hook_command(pkg_name, filename)
                expected_hooks.add((event, command, matcher))

    # 2. 不足 hook を追加
    added = 0
    for event, command, matcher in expected_hooks:
        if not _find_hook_in_settings(settings_hooks, event, command, matcher):
            _add_hook_to_settings(settings_hooks, event, command, matcher)
            added += 1

    # 3. 余剰 hook を削除（orchestra パッケージ由来の hook のみ対象）
    removed = 0
    for event, entries in list(settings_hooks.items()):
        for entry in list(entries):
            matcher = entry.get("matcher")
            for hook in list(entry.get("hooks", [])):
                command = hook.get("command", "")
                if command == sync_hook_command:
                    continue
                if not _is_orchestra_hook(command):
                    continue
                pkg_name = _parse_pkg_from_command(command)
                if pkg_name is not None and pkg_name not in installed_set:
                    # アンインストール済みパッケージの hook → 削除
                    _remove_hook_from_settings(settings_hooks, event, command, matcher)
                    removed += 1
                    continue
                if (event, command, matcher) not in expected_hooks:
                    _remove_hook_from_settings(settings_hooks, event, command, matcher)
                    removed += 1

    # 4. 変更があった場合のみ書き戻す
    changes = added + removed
    if changes > 0:
        settings["hooks"] = settings_hooks
        try:
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=2, ensure_ascii=False)
                f.write("\n")
        except OSError:
            pass

    return changes


CLAUDEIGNORE_HEADER = """\
# =============================================================================
# .claudeignore (auto-generated by AI Orchestra)
# DO NOT EDIT — managed by sync-orchestra.py
# Project-specific patterns: create .claudeignore.local
# =============================================================================
"""

LOCAL_SECTION_HEADER = """\
# =============================================================================
# Project-specific patterns (from .claudeignore.local)
# =============================================================================
"""


def _strip_header(content: str) -> str:
    """先頭の連続コメント行 + 空行ブロック（= ヘッダー）を除去してボディを返す。"""
    lines = content.splitlines(keepends=True)
    idx = 0
    # 先頭のコメント行・空行をスキップ
    while idx < len(lines):
        stripped = lines[idx].strip()
        if stripped == "" or stripped.startswith("#"):
            idx += 1
        else:
            break
    return "".join(lines[idx:])


def sync_claudeignore(project_dir: Path, orchestra_path: Path) -> bool:
    """ベーステンプレートと .claudeignore.local をマージして .claudeignore を生成する。

    Returns:
        True if .claudeignore was updated, False otherwise.
    """
    template_path = orchestra_path / "templates" / "project" / ".claudeignore"
    if not template_path.exists():
        return False

    try:
        base_content = template_path.read_text(encoding="utf-8")
    except OSError:
        return False

    base_body = _strip_header(base_content)

    # .claudeignore.local のマージ
    local_path = project_dir / ".claudeignore.local"
    local_section = ""
    if local_path.is_file():
        try:
            local_content = local_path.read_text(encoding="utf-8")
            local_body = _strip_header(local_content)
            if local_body.strip():
                local_section = "\n" + LOCAL_SECTION_HEADER + "\n" + local_body
        except OSError:
            pass

    merged = CLAUDEIGNORE_HEADER + "\n" + base_body + local_section

    # 末尾改行を正規化
    if not merged.endswith("\n"):
        merged += "\n"

    # 内容ベース差分チェック
    dst_path = project_dir / ".claudeignore"
    if dst_path.is_file():
        try:
            existing = dst_path.read_text(encoding="utf-8")
            if existing == merged:
                return False
        except OSError:
            pass

    try:
        dst_path.write_text(merged, encoding="utf-8")
    except OSError:
        return False

    return True


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
            with open(manifest_path, encoding="utf-8") as f:
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
        orch["last_sync"] = datetime.datetime.now(datetime.UTC).isoformat()
        orch["synced_files"] = sorted(synced_files)
        try:
            with open(orch_path, "w", encoding="utf-8") as f:
                json.dump(orch, f, indent=2, ensure_ascii=False)
                f.write("\n")
        except OSError:
            pass

    # hooks 同期（manifest.json の hooks と settings.local.json の差分を反映）
    hooks_changed = sync_hooks(project_dir, orchestra_path, installed_packages)

    # .claudeignore 同期（ベーステンプレート + .claudeignore.local をマージ）
    claudeignore_updated = sync_claudeignore(project_dir, orchestra_path)

    # SessionStart hook の stdout は Claude コンテキストに注入される
    if synced_count > 0 or removed_count > 0 or hooks_changed > 0 or claudeignore_updated:
        parts = []
        if synced_count > 0:
            parts.append(f"{synced_count} synced")
        if removed_count > 0:
            parts.append(f"{removed_count} removed")
        if hooks_changed > 0:
            parts.append(f"{hooks_changed} hooks synced")
        if claudeignore_updated:
            parts.append(".claudeignore updated")
        print(f"[orchestra] {', '.join(parts)}")


if __name__ == "__main__":
    main()

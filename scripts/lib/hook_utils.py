"""Hook 操作の共通ユーティリティ。

orchestra-manager.py (HooksMixin) と sync-orchestra.py の両方から使用する。
"""

from __future__ import annotations

from typing import Any

HOOK_COMMAND_TEMPLATE = 'python3 "$AI_ORCHESTRA_DIR/packages/{pkg_name}/hooks/{filename}"'


def get_hook_command(pkg_name: str, filename: str) -> str:
    """フックコマンド文字列を生成する。"""
    return HOOK_COMMAND_TEMPLATE.format(pkg_name=pkg_name, filename=filename)


def find_hook_in_settings(
    settings_hooks: dict[str, Any],
    event: str,
    command: str,
    matcher: str | None = None,
) -> bool:
    """settings.local.json に指定 hook が登録済みか判定する。"""
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


def add_hook_to_settings(
    settings_hooks: dict[str, Any],
    event: str,
    command: str,
    matcher: str | None = None,
    timeout: int = 5,
) -> None:
    """settings.local.json の hooks dict に hook を追加する。"""
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


def remove_hook_from_settings(
    settings_hooks: dict[str, Any],
    event: str,
    command: str,
    matcher: str | None = None,
) -> None:
    """settings.local.json の hooks dict から hook を削除する。"""
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


def is_orchestra_hook(command: str) -> bool:
    """コマンドが $AI_ORCHESTRA_DIR/packages/*/hooks/* パターンか判定する。"""
    return command.startswith('python3 "$AI_ORCHESTRA_DIR/packages/') and "/hooks/" in command


def parse_pkg_from_command(command: str) -> str | None:
    """hook コマンドからパッケージ名を抽出する。"""
    prefix = 'python3 "$AI_ORCHESTRA_DIR/packages/'
    if not command.startswith(prefix):
        return None
    rest = command[len(prefix) :]
    slash_idx = rest.find("/")
    if slash_idx < 0:
        return None
    return rest[:slash_idx]


def parse_hook_entry(value: object) -> tuple[str, str | None]:
    """manifest.json の hooks 値から (file, matcher) を取得する。"""
    if isinstance(value, str):
        return value, None
    if isinstance(value, dict):
        return value["file"], value.get("matcher")
    return "", None

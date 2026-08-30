"""Hook 操作の共通ユーティリティ。

orchestra-manager.py (HooksMixin) と sync-orchestra.py の両方から使用する。
"""

from __future__ import annotations

from typing import Any

# hook コマンドが使う Python インタプリタの参照方法（Issue #343）。
# リテラル python3 だと、hook を起動するシェルの PATH 解決次第で想定外のインタプリタ
# （バージョンマネージャ未適用のログインシェルのシステム python 等）に落ち、依存モジュール
# 不足で全 hook が黙って失敗する。環境変数で明示指定できる形にし、`orchex init` が
# ~/.claude/settings.json の env.AI_ORCHESTRA_PYTHON へ sys.executable を書き込む。
# 未設定の環境では従来どおり PATH の python3 にフォールバックする（後方互換）。
HOOK_PYTHON_ENV_VAR = "AI_ORCHESTRA_PYTHON"
HOOK_INTERPRETER = f'"${{{HOOK_PYTHON_ENV_VAR}:-python3}}"'

# 旧形式（PATH の python3 直参照）。既存 settings.local.json の移行判定にのみ使う。
LEGACY_HOOK_INTERPRETER = "python3"

# 認識するインタプリタ表記。新形式を先に置き、前方一致で判定する。
_HOOK_INTERPRETERS = (HOOK_INTERPRETER, LEGACY_HOOK_INTERPRETER)

_PACKAGE_HOOK_PREFIX = '"$AI_ORCHESTRA_DIR/packages/'

# str.format() 用テンプレート。インタプリタ側の `${...}` は書式指定と衝突するため
# `{{` / `}}` へエスケープしたうえで連結する。
_ESCAPED_HOOK_INTERPRETER = HOOK_INTERPRETER.replace("{", "{{").replace("}", "}}")
HOOK_COMMAND_TEMPLATE = (
    _ESCAPED_HOOK_INTERPRETER + ' "$AI_ORCHESTRA_DIR/packages/{pkg_name}/hooks/{filename}"'
)

# sync-orchestra.py（SessionStart hook）のスクリプト引数とコマンド全体。
SYNC_HOOK_SCRIPT_ARG = '"$AI_ORCHESTRA_DIR/scripts/sync-orchestra.py"'
SYNC_HOOK_COMMAND = f"{HOOK_INTERPRETER} {SYNC_HOOK_SCRIPT_ARG}"

# manifest.json の hooks エントリで timeout 未指定/不正値の場合に使うデフォルト（秒）。
# Claude Code の settings.local.json hook エントリの既定値と揃える。
DEFAULT_HOOK_TIMEOUT = 5


def _coerce_timeout(value: object) -> int:
    """timeout 値を正の int に正規化する。不正値・未指定は既定値にフォールバックする。"""
    # bool は int のサブクラスのため isinstance(value, int) だけでは True/False を通してしまう
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return DEFAULT_HOOK_TIMEOUT
    return value


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
    timeout: int = DEFAULT_HOOK_TIMEOUT,
) -> bool:
    """settings.local.json の hooks dict に hook を追加/更新する。

    command が既に登録済みの場合、timeout が manifest 側と異なれば更新する
    （内部サブプロセスの許容時間を超える既定値 5 秒に固定されたままにならないようにするため）。

    Returns:
        変更（新規追加または timeout 更新）があれば True。
    """
    if event not in settings_hooks:
        settings_hooks[event] = []

    target_entry: dict[str, Any] | None = None
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
            if hook.get("timeout", DEFAULT_HOOK_TIMEOUT) == timeout:
                return False
            hook["timeout"] = timeout
            return True

    target_entry["hooks"].append({"type": "command", "command": command, "timeout": timeout})
    return True


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


def strip_hook_interpreter(command: str) -> str | None:
    """hook コマンドからインタプリタ部分を取り除いた残りを返す。

    新形式（HOOK_INTERPRETER）と旧形式（リテラル python3）の両方を受理する。
    どちらの表記でもない場合は None を返す（ai-orchestra が登録した hook ではない）。
    """
    for interpreter in _HOOK_INTERPRETERS:
        prefix = f"{interpreter} "
        if command.startswith(prefix):
            return command[len(prefix) :]
    return None


def is_sync_hook_command(command: str) -> bool:
    """sync-orchestra hook のコマンドか判定する（新旧インタプリタ表記の両方を受理）。"""
    return strip_hook_interpreter(command) == SYNC_HOOK_SCRIPT_ARG


def is_orchestra_hook(command: str) -> bool:
    """コマンドが $AI_ORCHESTRA_DIR/packages/*/hooks/* パターンか判定する。"""
    rest = strip_hook_interpreter(command)
    if rest is None:
        return False
    return rest.startswith(_PACKAGE_HOOK_PREFIX) and "/hooks/" in rest


def parse_pkg_from_command(command: str) -> str | None:
    """hook コマンドからパッケージ名を抽出する。"""
    rest = strip_hook_interpreter(command)
    if rest is None or not rest.startswith(_PACKAGE_HOOK_PREFIX):
        return None
    tail = rest[len(_PACKAGE_HOOK_PREFIX) :]
    slash_idx = tail.find("/")
    if slash_idx < 0:
        return None
    return tail[:slash_idx]


def parse_hook_entry(value: object) -> tuple[str, str | None, int]:
    """manifest.json の hooks 値から (file, matcher, timeout) を取得する。

    dict 形式の任意キー `timeout`（正の int 秒）を受理する。未指定または不正値
    （0 以下・非 int）は DEFAULT_HOOK_TIMEOUT にフォールバックする（後方互換）。
    """
    if isinstance(value, str):
        return value, None, DEFAULT_HOOK_TIMEOUT
    if isinstance(value, dict):
        return (
            value["file"],
            value.get("matcher"),
            _coerce_timeout(value.get("timeout", DEFAULT_HOOK_TIMEOUT)),
        )
    return "", None, DEFAULT_HOOK_TIMEOUT

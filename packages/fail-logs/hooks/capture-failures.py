#!/usr/bin/env python3
"""PostToolUse / PostToolUseFailure / PermissionDenied hook: AI の失敗イベントを fail-logs に記録する。

検知ロジックは core/failure_detector に委譲し、本フックは
「設定読み込み → 検知 → マスク → JSONL 追記」のオーケストレーションに専念する。

失敗のみを記録する（成功の base-rate は audit の quality_gate が担う）。
ログは .claude/logs/fail-logs/failures.jsonl にセッション横断で追記する。

同じスクリプトを 3 つのイベントに登録し、入力の ``hook_event_name`` で分岐する
（設計 docs/design/fail-logs.md §3(f)）。``PostToolUse`` の経路（イベント名が無い入力を含む）は
従来の処理のまま残し、新しいイベントの行だけに ``tool_use_id`` と強化版マスキングを使う（§3(h)・NF-06）。
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime

# --- sys.path 設定（core/hooks を解決してから import する）---------------------
_hook_dir = os.path.dirname(os.path.abspath(__file__))
if _hook_dir not in sys.path:
    sys.path.insert(0, _hook_dir)

_orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
_repo_core_hooks = os.path.abspath(os.path.join(_hook_dir, "..", "..", "core", "hooks"))
for _candidate in [
    os.path.join(_orchestra_dir, "packages", "core", "hooks") if _orchestra_dir else "",
    _repo_core_hooks,
]:
    if _candidate and os.path.isdir(_candidate) and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

_lib_dir = os.path.abspath(os.path.join(_hook_dir, "..", "lib"))
if os.path.isdir(_lib_dir) and _lib_dir not in sys.path:
    sys.path.insert(0, _lib_dir)

import failure_detector as fd  # noqa: E402

try:
    import masking  # noqa: E402
except Exception:  # noqa: BLE001 - lib が欠けても PostToolUse の経路（NF-06）を止めない
    masking = None
from hook_common import (  # noqa: E402
    load_package_config,
    read_hook_input,
    resolve_log_root,
    resolve_path_within,
    safe_hook_execution,
    sanitized_git_env,
)
from log_migration import (  # noqa: E402
    LOG_DIR_MODE,
    LOG_FILE_MODE,
    migrate_legacy_worktree_log,
)

SCHEMA_VERSION = 1
DEFAULT_LOGS_DIR = os.path.join(".claude", "logs", "fail-logs")
DEFAULT_MAX_EXCERPT_CHARS = 500
LOG_FILE_NAME = "failures.jsonl"
GIT_COMMAND_TIMEOUT_SECONDS = 5
GIT_BUDGET_SECONDS = 3.5
GIT_STEP_TIMEOUT_SECONDS = 1.0

EVENT_POST_TOOL_USE = "PostToolUse"
EVENT_POST_TOOL_USE_FAILURE = "PostToolUseFailure"
EVENT_PERMISSION_DENIED = "PermissionDenied"

# PermissionDenied の行の値（設計 §3(f)）。failure_detector の定数には無いため fail-logs 側で持つ。
FAILURE_PERMISSION_DENIED = "permission_denied"
BY_PERMISSION = "permission"
ERROR_TYPE_PERMISSION = "permission"

# tool_use_id として受け付ける形（ログの肥大と不正な値を防ぐ）。
_TOOL_USE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")

# PostToolUseFailure の Bash の error の 1 行目（実機で確認した書式。設計 §15 の 1）。
_EXIT_CODE_LINE = re.compile(r"^Exit code (-?\d+)$")

_FAILURE_TYPE_BY_KIND = {
    fd.KIND_TEST: fd.FAILURE_TEST,
    fd.KIND_LINT: fd.FAILURE_LINT,
    fd.KIND_CLI: fd.FAILURE_CLI,
}

# 機密情報マスクパターン（audit-cli.py と同等。core 依存のみのため自前で保持）
SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(
        r"\b[A-Za-z0-9_-]{0,20}(api[_-]?key|token|password|secret|credential)\b\s*[:=]\s*\S+",
        re.IGNORECASE,
    ),
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]+", re.IGNORECASE),
    re.compile(r"\bghp_[A-Za-z0-9]{36}\b"),
    re.compile(r"\b(AKIA|ASIA|A3T)[A-Z0-9]{16}\b"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{35}\b"),
    re.compile(r"\bSharedAccessSignature\s*=\s*\S+", re.IGNORECASE),
    re.compile(r"-----BEGIN (RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----"),
]


def _mask_secrets(text: str) -> str:
    """既知の機密情報パターンを [REDACTED] に置換する。"""
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def _coerce_positive_int(value: object, default: int) -> int:
    """config 値を正の整数に変換する。型不正・非正値はデフォルトに落とす。"""
    try:
        result = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return result if result > 0 else default


def _resolve_project_dir(data: dict) -> str:
    """hook 入力からプロジェクトルートを解決する。

    data.cwd → CLAUDE_PROJECT_DIR → os.getcwd() の順。
    """
    cwd = str(data.get("cwd") or "")
    if cwd and os.path.isdir(os.path.join(cwd, ".claude")):
        return cwd
    return os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()


def _resolve_branch(project_dir: str, timeout: float) -> str:
    """記録時点の Git ブランチ名を解決する。取得失敗時は空文字列を返す(fail-safe)。"""
    if timeout <= 0:
        return ""

    deadline = time.monotonic() + timeout
    git_env = sanitized_git_env()
    commands = (
        ["git", "symbolic-ref", "--short", "HEAD"],
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
    )
    for command in commands:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                env=git_env,
                text=True,
                timeout=remaining,
                cwd=project_dir,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError, UnicodeError):
            continue
        if result.returncode == 0:
            return result.stdout.strip()
    return ""


def _append_secure_jsonl(path: str, record: dict) -> None:
    """JSONL に 1 行追記する（排他ロック + 所有者限定パーミッション）。"""
    dir_name = os.path.dirname(path)
    os.makedirs(dir_name, mode=LOG_DIR_MODE, exist_ok=True)
    fd_ = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, LOG_FILE_MODE)
    # O_CREAT のモードは umask の影響を受け、既存ファイルには適用されない。
    # 機密を含みうるログのため chmod で所有者限定を確実にする。
    try:
        os.chmod(path, LOG_FILE_MODE)
    except OSError:
        pass
    with os.fdopen(fd_, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _extract_excerpt(tool_name: str, tool_response: dict, max_chars: int) -> str:
    """ツール応答から失敗内容の抜粋を作る（マスク済み・文字数制限付き）。"""
    text = (
        tool_response.get("stdout", "")
        or tool_response.get("content", "")
        or tool_response.get("error", "")
        or ""
    )
    if not isinstance(text, str):
        text = str(text)
    return _mask_secrets(text[:max_chars])


def _failure_type_for_kind(kind: str) -> str:
    """コマンド種別を失敗種別にする（failure_detector の公開定数だけを使う）。"""
    return _FAILURE_TYPE_BY_KIND.get(kind, fd.FAILURE_TOOL_ERROR)


def _normalize_tool_use_id(value: object) -> str:
    """tool_use_id を決まった形のときだけ残す（それ以外は空）。"""
    text = value if isinstance(value, str) else ""
    return text if _TOOL_USE_ID.fullmatch(text) else ""


def _as_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _bash_command(tool_name: str, tool_input: dict) -> str:
    if tool_name != "Bash":
        return ""
    return str(tool_input.get("command") or "")


def _analyze_failure_event(data: dict, tool_name: str) -> dict | None:
    """PostToolUseFailure の入力から失敗の事実を作る（設計 §3(f)）。中断は記録しない。"""
    if data.get("is_interrupt") is True:
        return None
    command = _bash_command(tool_name, _as_dict(data.get("tool_input")))
    command_kind = fd.classify_command(command)
    raw_error = data.get("error")
    error = raw_error if isinstance(raw_error, str) else str(raw_error or "")
    first_line, _, rest = error.partition("\n")
    match = _EXIT_CODE_LINE.match(first_line.strip()) if tool_name == "Bash" else None
    if match:
        exit_code = int(match.group(1))
        return {
            "failure_type": _failure_type_for_kind(command_kind),
            "error_type": fd.classify_error(rest, exit_code),
            "detected_by": fd.BY_EXIT_CODE,
            "command_kind": command_kind,
            "exit_code": exit_code,
            "command": command,
            "excerpt": rest,
        }
    return {
        "failure_type": fd.FAILURE_TOOL_ERROR,
        "error_type": fd.classify_error(error, None),
        "detected_by": fd.BY_TOOL_ERROR,
        "command_kind": command_kind,
        "exit_code": None,
        "command": command,
        "excerpt": error,
    }


def _analyze_permission_denied(data: dict, tool_name: str) -> dict:
    """PermissionDenied（auto mode の分類器の拒否）の入力から失敗の事実を作る（設計 §3(f)）。"""
    command = _bash_command(tool_name, _as_dict(data.get("tool_input")))
    return {
        "failure_type": FAILURE_PERMISSION_DENIED,
        "error_type": ERROR_TYPE_PERMISSION,
        "detected_by": BY_PERMISSION,
        "command_kind": fd.classify_command(command),
        "exit_code": None,
        "command": command,
        "excerpt": str(data.get("reason") or ""),
    }


def _mask_new_event_texts(command: str, excerpt: str, max_chars: int) -> tuple[str, str]:
    """新しいイベントの command / error_excerpt を、伏せてから切り詰める（設計 §3(h)）。

    強化版マスキングが使えないとき（読み込み・カナリア・呼び出しの失敗）は、2 項目を空にする。
    """
    if not command and not excerpt:
        return "", ""
    if masking is None:
        return "", ""
    masker = masking.load_masker()
    if masker is None:
        return "", ""
    try:
        return (
            masking.mask_then_truncate(command, max_chars, masker),
            masking.mask_then_truncate(excerpt, max_chars, masker),
        )
    except Exception:  # noqa: BLE001 - 伏せ字の失敗は原文を残さない側に倒す
        return "", ""


def _post_tool_use_fields(
    tool_name: str, tool_input: dict, tool_response: dict, max_chars: int
) -> tuple[str, dict] | None:
    """PostToolUse の経路の data を作る（従来の処理。キーの順と値を変えない）。"""
    failure = fd.analyze(tool_name, tool_input, tool_response)
    if failure is None:
        return None
    command = ""
    if tool_name == "Bash":
        command = _mask_secrets(str(tool_input.get("command", ""))[:max_chars])
    fields = {
        "failure_type": failure["failure_type"],
        "error_type": failure["error_type"],
        "detected_by": failure["detected_by"],
        "command_kind": failure["command_kind"],
        "tool": tool_name,
        "command": command,
        "error_excerpt": _extract_excerpt(tool_name, tool_response, max_chars),
        "exit_code": tool_response.get("exit_code"),
    }
    return failure["failure_type"], fields


def _new_event_fields(failure: dict, tool_name: str, max_chars: int) -> dict:
    """PostToolUseFailure / PermissionDenied の行の data を作る（tool_use_id は _persist で最後に足す）。"""
    command, excerpt = _mask_new_event_texts(failure["command"], failure["excerpt"], max_chars)
    return {
        "failure_type": failure["failure_type"],
        "error_type": failure["error_type"],
        "detected_by": failure["detected_by"],
        "command_kind": failure["command_kind"],
        "tool": tool_name,
        "command": command,
        "error_excerpt": excerpt,
        "exit_code": failure["exit_code"],
    }


def _resolve_logs_dir(config: dict) -> str:
    logs_dir_value = config.get("logs_dir")
    return (
        logs_dir_value if isinstance(logs_dir_value, str) and logs_dir_value else DEFAULT_LOGS_DIR
    )


def _persist(
    data: dict,
    project_dir: str,
    config: dict,
    deadline: float,
    fields: dict,
    extra: dict | None = None,
) -> None:
    """root worktree のログに 1 行追記する（log_root の解決は書き込みが確定してから行う）。

    ``extra`` は従来の項目（``branch`` まで）の後ろに足す項目。新しいイベントの ``tool_use_id`` に使う。
    """
    logs_dir = _resolve_logs_dir(config)

    # log_root の解決（git サブプロセス起動）はここまで遅延させる。実際に書き込みが
    # 確定した後（enabled → analyze → targets 判定を通過した後）でのみ実行することで、
    # fail-logs 無効時・成功ツール呼び出し時（大多数）のホットパスを保つ。
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        log_root = project_dir
    else:
        log_root = resolve_log_root(
            project_dir,
            timeout=min(GIT_STEP_TIMEOUT_SECONDS, remaining),
        )

    log_path = resolve_path_within(log_root, logs_dir, LOG_FILE_NAME)
    effective_logs_dir = logs_dir
    if log_path is None:
        effective_logs_dir = DEFAULT_LOGS_DIR
        log_path = resolve_path_within(log_root, DEFAULT_LOGS_DIR, LOG_FILE_NAME)

    remaining = deadline - time.monotonic()
    branch = (
        ""
        if remaining <= 0
        else _resolve_branch(project_dir, min(GIT_STEP_TIMEOUT_SECONDS, remaining))
    )

    record = {
        "v": SCHEMA_VERSION,
        "ts": datetime.now(UTC).isoformat(),
        "sid": str(data.get("session_id") or ""),
        "eid": uuid.uuid4().hex[:12],
        "type": "failure",
        "data": {
            **fields,
            "cwd": str(data.get("cwd") or ""),
            "branch": _mask_secrets(branch),
            **(extra or {}),
        },
    }

    migrate_legacy_worktree_log(
        project_dir,
        log_root,
        effective_logs_dir,
        LOG_FILE_NAME,
    )

    # logs_dir が log_root 外を指す場合（設定経由のパストラバーサル）は
    # 書き込みを黙って捨てず、安全なデフォルトへフォールバックする。
    if log_path is None:
        return
    _append_secure_jsonl(log_path, record)


@safe_hook_execution
def main() -> None:
    """hook のエントリポイント。失敗時のみレコードを追記する。"""
    data = read_hook_input()
    deadline = time.monotonic() + GIT_BUDGET_SECONDS
    if not data:
        return

    project_dir = _resolve_project_dir(data)

    config = load_package_config("fail-logs", "fail-logs.yaml", project_dir)
    if config.get("enabled", True) is False:
        return

    tool_name = str(data.get("tool_name") or "")
    if not tool_name:
        return

    targets = config.get("targets") or {}
    max_chars = _coerce_positive_int(config.get("max_excerpt_chars"), DEFAULT_MAX_EXCERPT_CHARS)
    event = str(data.get("hook_event_name") or EVENT_POST_TOOL_USE)

    if event in (EVENT_POST_TOOL_USE_FAILURE, EVENT_PERMISSION_DENIED):
        if event == EVENT_POST_TOOL_USE_FAILURE:
            failure = _analyze_failure_event(data, tool_name)
        else:
            failure = _analyze_permission_denied(data, tool_name)
        if failure is None or targets.get(failure["failure_type"], True) is False:
            return
        fields = _new_event_fields(failure, tool_name, max_chars)
        extra = {"tool_use_id": _normalize_tool_use_id(data.get("tool_use_id"))}
    else:
        result = _post_tool_use_fields(
            tool_name,
            data.get("tool_input") or {},
            data.get("tool_response") or {},
            max_chars,
        )
        # 失敗種別ごとのトグル
        if result is None or targets.get(result[0], True) is False:
            return
        fields = result[1]
        extra = None

    _persist(data, project_dir, config, deadline, fields, extra)


if __name__ == "__main__":
    main()

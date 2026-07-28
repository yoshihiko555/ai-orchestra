#!/usr/bin/env python3
"""PostToolUse hook: AI の失敗イベントを fail-logs に記録する。

検知ロジックは core/failure_detector に委譲し、本フックは
「設定読み込み → 検知 → マスク → JSONL 追記」のオーケストレーションに専念する。

失敗のみを記録する（成功の base-rate は audit の quality_gate が担う）。
ログは .claude/logs/fail-logs/failures.jsonl にセッション横断で追記する。
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
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

import failure_detector as fd  # noqa: E402
from hook_common import (  # noqa: E402
    load_package_config,
    read_hook_input,
    resolve_log_root,
    resolve_path_within,
    safe_hook_execution,
)

SCHEMA_VERSION = 1
DEFAULT_LOGS_DIR = os.path.join(".claude", "logs", "fail-logs")
DEFAULT_MAX_EXCERPT_CHARS = 500
LOG_FILE_NAME = "failures.jsonl"
LOG_DIR_MODE = 0o700
LOG_FILE_MODE = 0o600

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


def _resolve_branch(project_dir: str) -> str:
    """記録時点の Git ブランチ名を解決する。取得失敗時は空文字列を返す(fail-safe)。"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=project_dir,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
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


@safe_hook_execution
def main() -> None:
    """PostToolUse hook のエントリポイント。失敗時のみレコードを追記する。"""
    data = read_hook_input()
    if not data:
        return

    project_dir = _resolve_project_dir(data)

    config = load_package_config("fail-logs", "fail-logs.yaml", project_dir)
    if config.get("enabled", True) is False:
        return

    tool_name = str(data.get("tool_name") or "")
    if not tool_name:
        return

    tool_input = data.get("tool_input") or {}
    tool_response = data.get("tool_response") or {}

    failure = fd.analyze(tool_name, tool_input, tool_response)
    if failure is None:
        return

    # 失敗種別ごとのトグル
    targets = config.get("targets") or {}
    if targets.get(failure["failure_type"], True) is False:
        return

    max_chars = _coerce_positive_int(config.get("max_excerpt_chars"), DEFAULT_MAX_EXCERPT_CHARS)
    logs_dir_value = config.get("logs_dir")
    logs_dir = (
        logs_dir_value if isinstance(logs_dir_value, str) and logs_dir_value else DEFAULT_LOGS_DIR
    )

    command = ""
    if tool_name == "Bash":
        command = _mask_secrets(str(tool_input.get("command", ""))[:max_chars])

    record = {
        "v": SCHEMA_VERSION,
        "ts": datetime.now(UTC).isoformat(),
        "sid": str(data.get("session_id") or ""),
        "eid": uuid.uuid4().hex[:12],
        "type": "failure",
        "data": {
            "failure_type": failure["failure_type"],
            "error_type": failure["error_type"],
            "detected_by": failure["detected_by"],
            "command_kind": failure["command_kind"],
            "tool": tool_name,
            "command": command,
            "error_excerpt": _extract_excerpt(tool_name, tool_response, max_chars),
            "exit_code": tool_response.get("exit_code"),
            "cwd": str(data.get("cwd") or ""),
            "branch": _resolve_branch(project_dir),
        },
    }

    # log_root の解決（git サブプロセス起動）はここまで遅延させる。実際に書き込みが
    # 確定した後（enabled → analyze → targets 判定を通過した後）でのみ実行することで、
    # fail-logs 無効時・成功ツール呼び出し時（大多数）のホットパスを保つ。
    log_root = resolve_log_root(project_dir)

    # logs_dir が log_root 外を指す場合（設定経由のパストラバーサル）は
    # 書き込みを黙って捨てず、安全なデフォルトへフォールバックする。
    log_path = resolve_path_within(log_root, logs_dir, LOG_FILE_NAME) or resolve_path_within(
        log_root, DEFAULT_LOGS_DIR, LOG_FILE_NAME
    )
    if log_path is None:
        return
    _append_secure_jsonl(log_path, record)


if __name__ == "__main__":
    main()

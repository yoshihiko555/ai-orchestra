#!/usr/bin/env python3
"""
PostToolUse hook: Suggest Codex debugging after test failures.

Triggers after Bash tool calls containing test commands (pytest, npm test, etc.)
when the test run fails.

Also records test results to the shared test-gate state file
(.claude/state/test-gate-checker.json, resolved via
quality_gate_config.resolve_state_path) so that test-gate-checker.py can
reset change counters after successful tests. The state file lives under the
current project_dir (worktree), so separate worktrees of the same repo
naturally get isolated counters. Within one project_dir, state is
additionally scoped per git-common-dir (see
quality_gate_config.get_project_state_key) for backward-compatible schema
consistency with the other quality-gates hooks.
"""

import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

_hook_dir = os.path.dirname(os.path.abspath(__file__))
if _hook_dir not in sys.path:
    sys.path.insert(0, _hook_dir)

# hook_common / event_logger を import するため core/hooks と audit/hooks を sys.path に追加
_orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
_repo_core_hooks = os.path.abspath(os.path.join(_hook_dir, "..", "..", "core", "hooks"))
_repo_audit_hooks = os.path.abspath(os.path.join(_hook_dir, "..", "..", "audit", "hooks"))

for _candidate in [
    os.path.join(_orchestra_dir, "packages", "core", "hooks") if _orchestra_dir else "",
    os.path.join(_orchestra_dir, "packages", "audit", "hooks") if _orchestra_dir else "",
    _repo_core_hooks,
    _repo_audit_hooks,
]:
    if _candidate and os.path.isdir(_candidate) and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

from event_logger import (  # noqa: E402
    emit_event,
    load_trace_state,
    resolve_project_root_from_hook_data,
)
from failure_detector import analyze  # noqa: E402
from hook_common import (  # noqa: E402
    DEFAULT_CODEX_FLAGS,
    DEFAULT_CODEX_MODEL,
    DEFAULT_CODEX_SANDBOX_ANALYSIS,
    load_package_config,
)
from quality_gate_config import (  # noqa: E402
    DEFAULT_TEST_GATE_STATE,
    get_project_state_key,
    load_project_scoped_state,
    resolve_quality_gate_enabled,
    resolve_state_path,
    save_project_scoped_state,
)
from secret_masking import mask_secrets  # noqa: E402

# quality_gate.block_on_failed_test の既定値。2026-07-03 人間レビュー裁定
# (docs/evaluation/quality-gates.md EV-11/12/19) により、明示的な opt-out
# (`false`) が無い限りテスト失敗時は既定でブロックする。
BLOCK_ON_FAILED_TEST_DEFAULT = True

# Test command patterns
TEST_COMMAND_PATTERNS = [
    r"\bpytest\b",
    r"\bnpm\s+test\b",
    r"\bnpm\s+run\s+test\b",
    r"\bpnpm\s+test\b",
    r"\byarn\s+test\b",
    r"\buv\s+run\s+pytest\b",
    r"\bpoe\s+test\b",
    r"\bgo\s+test\b",
    r"\bcargo\s+test\b",
    r"\bmake\s+test\b",
    r"\bruff\s+check\b",
    r"\bmypy\b",
]

# Shared state filename with test-gate-checker.py. The actual path is
# resolved per-project via quality_gate_config.resolve_state_path().
STATE_FILENAME = "test-gate-checker.json"


def is_test_command(command: str) -> bool:
    """Check if the command is a test command."""
    command_lower = command.lower()
    return any(re.search(pattern, command_lower) for pattern in TEST_COMMAND_PATTERNS)


def extract_failure_summary(output: str) -> str:
    """Extract a brief summary of the test failure."""
    lines = output.split("\n")

    # Look for lines containing failure information
    failure_lines = []
    for line in lines:
        if any(
            indicator in line for indicator in ["FAILED", "Error", "AssertionError", "TypeError"]
        ):
            failure_lines.append(line.strip())
            if len(failure_lines) >= 3:
                break

    if failure_lines:
        return "\n".join(failure_lines[:3])
    return "Test failure detected"


def load_test_gate_state(project_dir: str, config: dict | None = None) -> dict:
    """Load the shared test-gate state from file (scoped to the current project).

    `config` may be an already-loaded audit-flags.json dict (see main()) to
    avoid re-reading the config file within the same hook invocation.
    """
    project_key = get_project_state_key(project_dir)
    state_file = Path(resolve_state_path(project_dir, STATE_FILENAME, config=config))
    return load_project_scoped_state(state_file, project_key, DEFAULT_TEST_GATE_STATE)


def save_test_gate_state(project_dir: str, state: dict, config: dict | None = None) -> None:
    """Save the shared test-gate state to file (scoped to the current project).

    `config` may be an already-loaded audit-flags.json dict (see main()) to
    avoid re-reading the config file within the same hook invocation.
    """
    project_key = get_project_state_key(project_dir)
    state_file = Path(resolve_state_path(project_dir, STATE_FILENAME, config=config))
    save_project_scoped_state(state_file, project_key, state)


def record_test_result(
    command: str, passed: bool, project_dir: str, config: dict | None = None
) -> None:
    """Record test result to the shared state file.

    On success: reset change counters and warned flag.
    On failure: keep counters (changes are not yet validated).

    `config` may be an already-loaded audit-flags.json dict (see main()) so
    the load+save pair below reads the config file at most once instead of
    twice (Issue #154 review: architecture-reviewer).
    """
    state = load_test_gate_state(project_dir, config=config)
    state["last_test_result"] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "passed": passed,
        "command": command,
    }
    if passed:
        state["files_modified_since_test"] = []
        state["lines_modified_since_test"] = 0
        state["warned"] = False
    save_test_gate_state(project_dir, state, config=config)


def load_quality_gate_config(project_dir: str, config: dict | None = None) -> dict:
    """audit-flags.json から quality_gate 設定を読み込む。

    `config` が渡された場合は再読み込みしない（main() で読み込み済みの
    audit-flags.json を再利用する）。
    """
    resolved_config = (
        config
        if config is not None
        else load_package_config("audit", "audit-flags.json", project_dir)
    )
    features = resolved_config.get("features", {})
    return features.get("quality_gate", {}) if isinstance(features, dict) else {}


def emit_quality_gate_event(
    data: dict,
    *,
    command: str,
    exit_code: int,
    gate_passed: bool,
    output: str,
    detected_by: str | None = None,
    config: dict | None = None,
) -> bool:
    """品質ゲート結果を audit イベントログに記録する。

    `gate_passed` は failure_detector.analyze による 2 段判定の結果を
    呼び出し側が導出して渡す。`exit_code` は payload 記録用に保持する
    （パイプで終了コードがマスクされても出力パターンで失敗を検知できる）。

    `config` は main() で読み込み済みの audit-flags.json dict（project_dir が
    resolve_project_root_from_hook_data(data) の解決結果と一致する場合のみ
    呼び出し側が渡す想定）。渡されない場合はここで読み込む。

    Returns:
        `block_on_failed_test` によりブロックすべき場合は True。
    """
    project_dir = resolve_project_root_from_hook_data(data)
    quality_gate = load_quality_gate_config(project_dir, config=config)
    if not resolve_quality_gate_enabled(quality_gate):
        return False

    trace = load_trace_state(project_dir=project_dir)
    blocking = (
        bool(quality_gate.get("block_on_failed_test", BLOCK_ON_FAILED_TEST_DEFAULT))
        and not gate_passed
    )

    # EV-22: 秘匿情報パターン（API キー・トークン・秘密鍵等）をマスクしてから
    # 記録する。200 文字切り詰めはマスキングの代替にならないため、切り詰めの
    # 前にマスクを適用する（切り詰め後の残骸でパターンが壊れるのを防ぐ）。
    payload = {
        "command": mask_secrets(command)[:200],
        "exit_code": exit_code,
        "passed": gate_passed,
        "output_excerpt": mask_secrets(output)[:200] if output else "",
        "blocking": blocking,
    }
    if detected_by is not None:
        payload["detected_by"] = detected_by

    emit_event(
        "quality_gate",
        payload,
        session_id=str(data.get("session_id") or ""),
        tid=trace.get("tid", ""),
        project_dir=project_dir,
    )
    return blocking


def _build_codex_command(data: dict) -> str:
    """cli-tools.yaml から Codex コマンド文字列を構築する。"""
    project_dir = data.get("cwd", "") or os.environ.get("CLAUDE_PROJECT_DIR", "")
    config = load_package_config("agent-routing", "cli-tools.yaml", project_dir)
    codex = config.get("codex", {})
    model = codex.get("model", DEFAULT_CODEX_MODEL)
    sandbox = codex.get("sandbox", {}).get("analysis", DEFAULT_CODEX_SANDBOX_ANALYSIS)
    flags = codex.get("flags", DEFAULT_CODEX_FLAGS)
    return f'`codex exec --model {model} --sandbox {sandbox} {flags} "..." < /dev/null 2>/dev/null`'


def main():
    try:
        data = json.load(sys.stdin)
        tool_name = data.get("tool_name", "")
        # test-gate-checker.py と同じ project_dir 解決方法に揃える。
        # resolve_project_root_from_hook_data は cwd に .claude が無い場合に
        # 別のパスへフォールバックするため、ここで使うと共有状態のキーが
        # test-gate-checker.py 側とずれてしまう。
        project_dir = data.get("cwd", "") or os.environ.get("CLAUDE_PROJECT_DIR", "")

        # Only process Bash tool calls
        if tool_name != "Bash":
            sys.exit(0)

        tool_input = data.get("tool_input", {})
        tool_response = data.get("tool_response", {})

        command = tool_input.get("command", "")

        # Check if this is a test command
        if not is_test_command(command):
            sys.exit(0)

        exit_code = tool_response.get("exit_code", 0)
        output = tool_response.get("stdout", "") or tool_response.get("content", "")

        # failure_detector で 2 段判定（exit_code + 出力パターン）に統一する。
        # パイプで exit code がマスクされた失敗も output パターンで検知できる。
        failure = analyze("Bash", tool_input, tool_response)
        gate_passed = failure is None
        analysis_failed = not gate_passed
        detected_by = failure.get("detected_by") if failure else None

        # Read audit-flags.json once and reuse it for record_test_result's
        # load+save pair and (when project_dir resolutions agree)
        # emit_quality_gate_event's quality_gate lookup, instead of reading
        # it independently up to 3x per invocation (Issue #154 review:
        # architecture-reviewer).
        config = load_package_config("audit", "audit-flags.json", project_dir)

        # EV-21: quality_gate.enabled=false のときは提案・警告・ブロック・audit
        # イベント記録だけでなく、record_test_result による状態書き込みも含め
        # 全動作を行わない（完全 no-op）。record_test_result より前にこの
        # チェックを行う必要がある（以前は record_test_result が先に実行され、
        # quality_gate.enabled=false でも状態ファイルへ書き込んでいた）。
        quality_gate = load_quality_gate_config(project_dir, config=config)
        if not resolve_quality_gate_enabled(quality_gate):
            sys.exit(0)

        # Record test result to shared state (success resets counters)
        record_test_result(command, gate_passed, project_dir, config=config)

        # emit_quality_gate_event resolves its own project_dir via
        # resolve_project_root_from_hook_data (which can fall back
        # differently than the data.get("cwd") logic above when cwd lacks a
        # .claude dir). Only reuse the config already read when both
        # resolutions agree, to avoid applying the wrong project's config.
        event_project_dir = resolve_project_root_from_hook_data(data)
        reusable_config = config if event_project_dir == project_dir else None
        blocking = emit_quality_gate_event(
            data,
            command=command,
            exit_code=exit_code,
            gate_passed=gate_passed,
            output=output,
            detected_by=detected_by,
            config=reusable_config,
        )

        if blocking:
            # detected_by を併記する。パイプマスク失敗（exit_code=0 でも
            # 出力パターンで検知）の場合にブロック理由を判別できるようにする。
            print(
                f"[quality-gates] quality gate blocked: test failed "
                f"(exit_code={exit_code}, detected_by={detected_by})",
                file=sys.stderr,
            )
            sys.exit(2)

        # If tests passed, no further action needed
        if not analysis_failed:
            sys.exit(0)

        # EV-21: quality_gate.enabled=false のときは、この Codex 提案
        # （additionalContext）も含め全動作を行わない。このチェックは
        # 関数冒頭（record_test_result より前）で既に行っているため
        # ここでは再実施しない（disabled ならここに到達する前に return 済み）。

        # EV-22: additionalContext に埋め込む前に秘匿情報をマスクする。
        failure_summary = mask_secrets(extract_failure_summary(output))
        codex_cmd = _build_codex_command(data)

        output_data = {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": (
                    "[Codex Debug Suggestion] Test failure detected:\n"
                    f"```\n{failure_summary}\n```\n\n"
                    f"Consider Codex for root cause analysis:\n{codex_cmd}"
                ),
            }
        }
        print(json.dumps(output_data))
        sys.exit(0)

    except Exception as e:
        print(f"Hook error: {e}", file=sys.stderr)
        sys.exit(0)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
PostToolUse hook: Suggest Codex review after Plan agent execution.

Triggers after Task tool calls with Plan agent to suggest
Codex review of the generated preflight plan.
"""

import json
import os
import sys

# hook_common を $AI_ORCHESTRA_DIR/packages/core/hooks/ から読み込む
_orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
if _orchestra_dir:
    _core_hooks = os.path.join(_orchestra_dir, "packages", "core", "hooks")
    if _core_hooks not in sys.path:
        sys.path.insert(0, _core_hooks)

from hook_common import (  # noqa: E402
    DEFAULT_CODEX_FLAGS,
    DEFAULT_CODEX_MODEL,
    DEFAULT_CODEX_SANDBOX_ANALYSIS,
    has_project_config,
    is_cli_enabled,
    load_package_config,
)


def is_plan_agent_task(tool_input: dict) -> bool:
    """Check if this was a Plan agent task."""
    subagent_type = tool_input.get("subagent_type", "").lower()
    prompt = tool_input.get("prompt", "").lower()

    # Check if subagent_type is Plan or plan-related
    if subagent_type in ("plan", "planner"):
        return True

    # Check prompt for planning keywords
    plan_keywords = [
        "計画",
        "プラン",
        "plan",
        "implementation plan",
        "設計計画",
        "実装計画",
    ]
    return any(keyword in prompt for keyword in plan_keywords)


def _build_codex_command(config: dict) -> str:
    """config から Codex コマンド文字列を構築する。"""
    codex = config.get("codex", {})
    model = codex.get("model", DEFAULT_CODEX_MODEL)
    sandbox = codex.get("sandbox", {}).get("analysis", DEFAULT_CODEX_SANDBOX_ANALYSIS)
    flags = codex.get("flags", DEFAULT_CODEX_FLAGS)
    flags_part = f"{flags} " if flags else ""
    return f'`codex exec --model {model} --sandbox {sandbox} {flags_part}"..." < /dev/null 2>/dev/null`'


def main():
    try:
        data = json.load(sys.stdin)
        tool_name = data.get("tool_name", "")

        # Only process Agent tool calls (backward compat: "Task" also accepted)
        if tool_name not in ("Agent", "Task"):
            sys.exit(0)

        # Codex CLI が無効化されている場合は提案をスキップ。
        # codex セクション自体が未定義の場合はデフォルト無効（2026-07-03 人間
        # レビュー裁定、EV-15）。他パッケージが共有する is_cli_enabled の
        # デフォルト（True）には影響しない。
        #
        # project が agent-routing を導入しておらず project-local な
        # cli-tools.yaml が存在しない場合、load_package_config はパッケージ
        # 同梱フォールバック（$AI_ORCHESTRA_DIR 配下）に暗黙フォールバックする。
        # そのフォールバック内の codex.enabled は project の明示 opt-in ではない
        # ため、先に has_project_config でガードする（Issue #129 PR #247
        # レビュー指摘: fallback config を opt-in 扱いしない）。
        project_dir = data.get("cwd", "") or os.environ.get("CLAUDE_PROJECT_DIR", "")
        if not has_project_config("agent-routing", "cli-tools.yaml", project_dir):
            sys.exit(0)
        config = load_package_config("agent-routing", "cli-tools.yaml", project_dir)
        if not is_cli_enabled("codex", config, default=False):
            sys.exit(0)

        tool_input = data.get("tool_input", {})
        tool_response = data.get("tool_response", {})

        # Check if this was a Plan agent task
        if not is_plan_agent_task(tool_input):
            sys.exit(0)

        # Check if the task completed successfully（構造化フィールドのみを根拠にする。
        # str(tool_response) の部分一致は「エラーハンドリング設計」等の正常な
        # plan 内容まで誤って抑制してしまうため使わない）
        if isinstance(tool_response, dict) and (
            tool_response.get("is_error") or tool_response.get("error")
        ):
            sys.exit(0)

        codex_cmd = _build_codex_command(config)
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": (
                    "[Codex Review Suggestion] Preflight plan created. "
                    "Before /startproject, consider Codex review for:\n"
                    "- Architecture alignment\n"
                    "- Potential risks\n"
                    "- Missing considerations\n"
                    "- Task granularity in Plans.md\n\n"
                    f"Use: {codex_cmd}"
                ),
            }
        }
        print(json.dumps(output))
        sys.exit(0)

    except Exception as e:
        print(f"Hook error: {e}", file=sys.stderr)
        sys.exit(0)


if __name__ == "__main__":
    main()

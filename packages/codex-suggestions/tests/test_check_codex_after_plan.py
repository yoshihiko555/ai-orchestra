import json

from tests.module_loader import load_module

check_codex_after_plan = load_module(
    "check_codex_after_plan",
    "packages/codex-suggestions/hooks/check-codex-after-plan.py",
)


# --- is_plan_agent_task ---


def test_is_plan_agent_task_with_plan_subagent() -> None:
    assert check_codex_after_plan.is_plan_agent_task({"subagent_type": "Plan"})


def test_is_plan_agent_task_with_planner_subagent() -> None:
    assert check_codex_after_plan.is_plan_agent_task({"subagent_type": "planner"})


def test_is_plan_agent_task_case_insensitive() -> None:
    assert check_codex_after_plan.is_plan_agent_task({"subagent_type": "PLAN"})
    assert check_codex_after_plan.is_plan_agent_task({"subagent_type": "Planner"})


def test_is_plan_agent_task_with_plan_keyword_in_prompt() -> None:
    assert check_codex_after_plan.is_plan_agent_task(
        {"subagent_type": "general-purpose", "prompt": "Create an implementation plan"}
    )


def test_is_plan_agent_task_with_japanese_keyword_in_prompt() -> None:
    assert check_codex_after_plan.is_plan_agent_task(
        {"subagent_type": "general-purpose", "prompt": "計画を立ててください"}
    )
    assert check_codex_after_plan.is_plan_agent_task(
        {"subagent_type": "general-purpose", "prompt": "実装計画を作成"}
    )
    assert check_codex_after_plan.is_plan_agent_task(
        {"subagent_type": "general-purpose", "prompt": "設計計画をまとめて"}
    )
    assert check_codex_after_plan.is_plan_agent_task(
        {"subagent_type": "general-purpose", "prompt": "プランを考えて"}
    )


def test_is_plan_agent_task_false_for_unrelated_task() -> None:
    assert not check_codex_after_plan.is_plan_agent_task(
        {"subagent_type": "frontend-dev", "prompt": "ログインフォームを実装して"}
    )


def test_is_plan_agent_task_false_for_empty_input() -> None:
    assert not check_codex_after_plan.is_plan_agent_task({})


def test_is_plan_agent_task_false_for_missing_fields() -> None:
    assert not check_codex_after_plan.is_plan_agent_task({"subagent_type": "code-reviewer"})


# --- main (stdout/exit-code integration) ---


def _run_main_with_stdin(data: dict) -> tuple[str, int]:
    """main() を stdin モックで実行し (stdout, exit_code) を返す。"""
    import io
    import sys

    old_stdin = sys.stdin
    old_stdout = sys.stdout
    sys.stdin = io.StringIO(json.dumps(data))
    sys.stdout = io.StringIO()

    exit_code = 0
    try:
        check_codex_after_plan.main()
    except SystemExit as e:
        exit_code = e.code if e.code is not None else 0

    stdout = sys.stdout.getvalue()
    sys.stdin = old_stdin
    sys.stdout = old_stdout
    return stdout, exit_code


def test_main_outputs_suggestion_for_plan_task() -> None:
    data = {
        "tool_name": "Task",
        "tool_input": {"subagent_type": "Plan", "prompt": "計画: 認証機能"},
        "tool_response": {"result": "Plan created successfully"},
    }
    stdout, exit_code = _run_main_with_stdin(data)
    assert exit_code == 0

    output = json.loads(stdout)
    context = output["hookSpecificOutput"]["additionalContext"]
    assert "[Codex Review Suggestion]" in context
    assert "Architecture alignment" in context


def test_main_skips_non_task_tool() -> None:
    data = {
        "tool_name": "Read",
        "tool_input": {"file_path": "/tmp/test.py"},
    }
    stdout, exit_code = _run_main_with_stdin(data)
    assert exit_code == 0
    assert stdout == ""


def test_main_skips_non_plan_task() -> None:
    data = {
        "tool_name": "Task",
        "tool_input": {"subagent_type": "frontend-dev", "prompt": "実装: ボタン"},
        "tool_response": {"result": "Done"},
    }
    stdout, exit_code = _run_main_with_stdin(data)
    assert exit_code == 0
    assert stdout == ""


def test_main_skips_failed_task() -> None:
    data = {
        "tool_name": "Task",
        "tool_input": {"subagent_type": "Plan", "prompt": "plan something"},
        "tool_response": {"error": "Failed to create plan"},
    }
    stdout, exit_code = _run_main_with_stdin(data)
    assert exit_code == 0
    assert stdout == ""


def test_main_handles_invalid_json_gracefully() -> None:
    import io
    import sys

    old_stdin = sys.stdin
    old_stdout = sys.stdout
    old_stderr = sys.stderr
    sys.stdin = io.StringIO("not valid json")
    sys.stdout = io.StringIO()
    sys.stderr = io.StringIO()

    exit_code = 0
    try:
        check_codex_after_plan.main()
    except SystemExit as e:
        exit_code = e.code if e.code is not None else 0

    sys.stdin = old_stdin
    sys.stdout = old_stdout
    sys.stderr = old_stderr
    assert exit_code == 0

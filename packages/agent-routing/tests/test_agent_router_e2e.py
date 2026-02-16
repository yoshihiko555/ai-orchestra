"""agent-router.py の main() エンドツーエンドテスト。

stdin に JSON を流し込み、stdout の hookSpecificOutput を検証する。
"""

from __future__ import annotations

import io
import json
import os
import sys

from tests.module_loader import REPO_ROOT, load_module

os.environ["AI_ORCHESTRA_DIR"] = str(REPO_ROOT)
sys.path.insert(0, str(REPO_ROOT / "packages" / "core" / "hooks"))
sys.path.insert(0, str(REPO_ROOT / "packages" / "agent-routing" / "hooks"))

agent_router = load_module("agent_router", "packages/agent-routing/hooks/agent-router.py")


def _run_hook(prompt: str, cwd: str | None = None) -> dict:
    """agent-router の main() を呼び、stdout の JSON を返す。出力なしなら空辞書。"""
    input_data = {"prompt": prompt, "cwd": cwd or str(REPO_ROOT)}
    captured = io.StringIO()
    stdin_backup = sys.stdin
    stdout_backup = sys.stdout
    try:
        sys.stdin = io.StringIO(json.dumps(input_data))
        sys.stdout = captured
        agent_router.main()
    except SystemExit:
        pass
    finally:
        sys.stdin = stdin_backup
        sys.stdout = stdout_backup

    output = captured.getvalue().strip()
    if not output:
        return {}
    return json.loads(output)


# ---------------------------------------------------------------------------
# エージェント検出時の出力
# ---------------------------------------------------------------------------


class TestHookOutputForAgentDetection:
    def test_tester_agent_detected(self) -> None:
        result = _run_hook("単体テストを追加してください")
        hook_out = result.get("hookSpecificOutput", {})
        ctx = hook_out.get("additionalContext", "")
        assert "Agent Routing" in ctx
        assert "tester" in ctx
        assert 'Task(subagent_type="tester"' in ctx

    def test_codex_agent_includes_cli_suggestion(self) -> None:
        result = _run_hook("デバッグしてほしい")
        hook_out = result.get("hookSpecificOutput", {})
        ctx = hook_out.get("additionalContext", "")
        assert "debugger" in ctx
        assert "Codex CLI" in ctx
        assert "codex exec" in ctx

    def test_claude_direct_agent_has_no_cli_suggestion(self) -> None:
        result = _run_hook("アーキテクチャを設計して")
        hook_out = result.get("hookSpecificOutput", {})
        ctx = hook_out.get("additionalContext", "")
        assert "architect" in ctx
        # claude-direct は CLI 提案なし → Codex CLI/Gemini CLI が含まれない
        assert "Codex CLI" not in ctx
        assert "Gemini CLI" not in ctx

    def test_gemini_agent_includes_gemini_suggestion(self) -> None:
        result = _run_hook("最新のライブラリについてリサーチして")
        hook_out = result.get("hookSpecificOutput", {})
        ctx = hook_out.get("additionalContext", "")
        assert "researcher" in ctx
        assert "Gemini CLI" in ctx
        assert "gemini" in ctx
        assert "-p" in ctx

    def test_hook_event_name_is_user_prompt_submit(self) -> None:
        result = _run_hook("テストを書いて")
        hook_out = result.get("hookSpecificOutput", {})
        assert hook_out.get("hookEventName") == "UserPromptSubmit"


# ---------------------------------------------------------------------------
# エージェント未検出時の Gemini フォールバック
# ---------------------------------------------------------------------------


class TestGeminiFallbackTrigger:
    def test_pdf_trigger_case_mismatch_is_known_bug(self) -> None:
        """BUG: GEMINI_FALLBACK_TRIGGERS の "PDF見て" が大文字なのに
        prompt.lower() と比較するためマッチしない。
        ここでは現在の挙動を記録しておく。"""
        result = _run_hook("このPDF見てください")
        # 現状はマッチしない（大文字 "PDF" vs lower() された "pdf"）
        assert result == {}

    def test_codebase_trigger(self) -> None:
        result = _run_hook("コードベース全体を理解したい")
        hook_out = result.get("hookSpecificOutput", {})
        ctx = hook_out.get("additionalContext", "")
        assert "Gemini CLI" in ctx


# ---------------------------------------------------------------------------
# スキップ条件
# ---------------------------------------------------------------------------


class TestHookSkipConditions:
    def test_short_prompt_produces_no_output(self) -> None:
        result = _run_hook("hi")
        assert result == {}

    def test_empty_prompt_produces_no_output(self) -> None:
        result = _run_hook("")
        assert result == {}

    def test_unrelated_prompt_produces_no_output(self) -> None:
        result = _run_hook("what is the weather today in tokyo")
        assert result == {}

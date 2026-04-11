"""audit hook ロジックのユニットテスト。"""

from __future__ import annotations

import os
import sys

from tests.module_loader import REPO_ROOT, load_module

# hook モジュールを読み込み
_audit_hooks = str(REPO_ROOT / "packages" / "audit" / "hooks")
_core_hooks = str(REPO_ROOT / "packages" / "core" / "hooks")
_routing_hooks = str(REPO_ROOT / "packages" / "agent-routing" / "hooks")
for p in [_audit_hooks, _core_hooks, _routing_hooks]:
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault("AI_ORCHESTRA_DIR", str(REPO_ROOT))

audit_route = load_module("audit_route", "packages/audit/hooks/audit-route.py")
audit_cli = load_module("audit_cli", "packages/audit/hooks/audit-cli.py")
audit_prompt = load_module("audit_prompt", "packages/audit/hooks/audit-prompt.py")


# ---------------------------------------------------------------------------
# detect_route (from audit-route.py)
# ---------------------------------------------------------------------------


class TestDetectRoute:
    def test_bash_codex(self) -> None:
        data = {"tool_name": "Bash", "tool_input": {"command": "codex exec --model gpt-5 'hello'"}}
        route, excerpt, tool = audit_route.detect_route(data)
        assert route == "bash:codex"
        assert "codex" in excerpt
        assert tool == "Bash"

    def test_bash_gemini(self) -> None:
        data = {"tool_name": "Bash", "tool_input": {"command": "gemini -m model -p 'query'"}}
        route, excerpt, tool = audit_route.detect_route(data)
        assert route == "bash:gemini"

    def test_bash_other(self) -> None:
        data = {"tool_name": "Bash", "tool_input": {"command": "ls -la"}}
        route, _, _ = audit_route.detect_route(data)
        assert route is None

    def test_task_agent(self) -> None:
        data = {"tool_name": "Task", "tool_input": {"subagent_type": "backend-python-dev"}}
        route, _, _ = audit_route.detect_route(data)
        assert route == "task:backend-python-dev"

    def test_agent_tool(self) -> None:
        data = {"tool_name": "Agent", "tool_input": {"subagent_type": "researcher"}}
        route, _, _ = audit_route.detect_route(data)
        assert route == "task:researcher"

    def test_skill(self) -> None:
        data = {"tool_name": "Skill", "tool_input": {"skill": "commit"}}
        route, _, _ = audit_route.detect_route(data)
        assert route == "skill:commit"

    def test_unknown_tool(self) -> None:
        data = {"tool_name": "Read", "tool_input": {}}
        route, _, _ = audit_route.detect_route(data)
        assert route is None


# ---------------------------------------------------------------------------
# is_match (from audit-route.py)
# ---------------------------------------------------------------------------


class TestIsMatch:
    def test_exact(self) -> None:
        assert audit_route.is_match("codex", "codex", {})

    def test_skill_matches_claude_direct(self) -> None:
        assert audit_route.is_match("claude-direct", "skill:commit", {})

    def test_alias(self) -> None:
        aliases = {"codex": ["bash:codex"]}
        assert audit_route.is_match("codex", "bash:codex", aliases)

    def test_no_match(self) -> None:
        assert not audit_route.is_match("codex", "gemini", {})

    def test_empty(self) -> None:
        assert not audit_route.is_match("", "codex", {})
        assert not audit_route.is_match("codex", "", {})


# ---------------------------------------------------------------------------
# _parse_actual_route (from audit-route.py)
# ---------------------------------------------------------------------------


class TestParseActualRoute:
    def test_with_colon(self) -> None:
        result = audit_route._parse_actual_route("bash:codex")
        assert result == {"tool": "bash", "detail": "codex"}

    def test_without_colon(self) -> None:
        result = audit_route._parse_actual_route("claude-direct")
        assert result == {"tool": "claude-direct", "detail": None}


# ---------------------------------------------------------------------------
# CLI extraction (from audit-cli.py)
# ---------------------------------------------------------------------------


class TestExtractCodexPrompt:
    def test_double_quotes(self) -> None:
        cmd = 'codex exec --model gpt-5 --full-auto "What is 2+2?" 2>/dev/null'
        assert audit_cli.extract_codex_prompt(cmd) == "What is 2+2?"

    def test_single_quotes(self) -> None:
        cmd = "codex exec --model gpt-5 --full-auto 'Design a REST API' 2>/dev/null"
        assert audit_cli.extract_codex_prompt(cmd) == "Design a REST API"

    def test_no_match(self) -> None:
        cmd = "echo hello"
        assert audit_cli.extract_codex_prompt(cmd) is None


class TestExtractGeminiPrompt:
    def test_double_quotes(self) -> None:
        cmd = 'gemini -m gemini-pro -p "Research topic" 2>/dev/null'
        assert audit_cli.extract_gemini_prompt(cmd) == "Research topic"

    def test_no_match(self) -> None:
        cmd = "gemini --version"
        assert audit_cli.extract_gemini_prompt(cmd) is None


class TestExtractModel:
    def test_codex_model(self) -> None:
        cmd = "codex exec --model gpt-5.3-codex --full-auto 'hello'"
        assert audit_cli.extract_model(cmd) == "gpt-5.3-codex"

    def test_gemini_model(self) -> None:
        cmd = "gemini -m gemini-2.5-pro -p 'hello'"
        assert audit_cli.extract_model(cmd, tool="gemini") == "gemini-2.5-pro"


class TestClassifyError:
    def test_success(self) -> None:
        assert audit_cli._classify_error(0, "") is None

    def test_timeout(self) -> None:
        assert audit_cli._classify_error(1, "Command timed out") == "timeout"

    def test_auth(self) -> None:
        assert audit_cli._classify_error(1, "Unauthorized access") == "auth"

    def test_rate_limit(self) -> None:
        assert audit_cli._classify_error(1, "429 Too Many Requests") == "rate_limit"

    def test_not_found(self) -> None:
        assert audit_cli._classify_error(127, "command not found") == "not_found"

    def test_unknown(self) -> None:
        assert audit_cli._classify_error(1, "something else broke") == "unknown"


# ---------------------------------------------------------------------------
# select_expected_route (from audit-prompt.py)
# ---------------------------------------------------------------------------


class TestSelectExpectedRoute:
    def test_default_route(self) -> None:
        route, rule = audit_prompt.select_expected_route(
            "hello world",
            {},
            {"default_route": "claude-direct", "rules": []},
        )
        assert route == "claude-direct"
        assert rule is None

    def test_keyword_match(self) -> None:
        policy = {
            "default_route": "claude-direct",
            "rules": [
                {"id": "r1", "keywords_any": ["optimize"], "expected_route": "codex", "priority": 1}
            ],
        }
        route, rule = audit_prompt.select_expected_route("please optimize this query", {}, policy)
        assert route == "codex"
        assert rule == "r1"

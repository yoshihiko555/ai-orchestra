"""orchestration-route-audit.py のユニットテスト。"""

from __future__ import annotations

from tests.module_loader import load_module

route_audit = load_module("route_audit", "packages/route-audit/hooks/orchestration-route-audit.py")


class TestDetectRoute:
    """detect_route のテスト。"""

    def test_bash_codex(self):
        """Bash で codex コマンドを検出。"""
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": "codex exec --model gpt-5 'question'"},
        }
        route, cmd = route_audit.detect_route(data)
        assert route == "bash:codex"
        assert "codex" in cmd

    def test_bash_gemini(self):
        """Bash で gemini コマンドを検出。"""
        data = {"tool_name": "Bash", "tool_input": {"command": "gemini -m model -p 'query'"}}
        route, cmd = route_audit.detect_route(data)
        assert route == "bash:gemini"

    def test_bash_other(self):
        """Bash で codex/gemini 以外は None。"""
        data = {"tool_name": "Bash", "tool_input": {"command": "pytest -q"}}
        route, cmd = route_audit.detect_route(data)
        assert route is None
        assert "pytest" in cmd

    def test_task_with_subagent(self):
        """Task ツールで subagent_type を検出。"""
        data = {"tool_name": "Task", "tool_input": {"subagent_type": "backend-python-dev"}}
        route, _ = route_audit.detect_route(data)
        assert route == "task:backend-python-dev"

    def test_task_without_subagent(self):
        """Task ツールで subagent_type なしはデフォルト agent。"""
        data = {"tool_name": "Task", "tool_input": {}}
        route, _ = route_audit.detect_route(data)
        assert route == "task:agent"

    def test_skill(self):
        """Skill ツールを追跡。"""
        data = {"tool_name": "Skill", "tool_input": {"skill": "commit"}}
        route, _ = route_audit.detect_route(data)
        assert route == "skill:commit"

    def test_unknown_tool(self):
        """未知のツールは None。"""
        data = {"tool_name": "Read", "tool_input": {}}
        route, _ = route_audit.detect_route(data)
        assert route is None

    def test_command_truncated(self):
        """長いコマンドは 200 文字に切り詰める。"""
        long_cmd = "x" * 500
        data = {"tool_name": "Bash", "tool_input": {"command": long_cmd}}
        _, cmd = route_audit.detect_route(data)
        assert len(cmd) == 200

    def test_tool_input_non_dict(self):
        """tool_input が dict でない場合のフォールバック。"""
        data = {"tool_name": "Bash", "tool_input": "not a dict", "command": "codex exec 'test'"}
        route, cmd = route_audit.detect_route(data)
        assert route == "bash:codex"


class TestMergedAliases:
    """merged_aliases のテスト。"""

    def test_merges_dynamic_and_static(self):
        """動的と静的 aliases をマージ。"""
        config = {
            "codex": {"enabled": True},
            "gemini": {"enabled": True},
            "agents": {
                "backend-python-dev": {"tool": "codex"},
            },
        }
        policy = {
            "aliases": {
                "codex": ["bash:codex"],
                "custom": ["custom-alias"],
            },
        }
        result = route_audit.merged_aliases(config, policy)
        assert "custom" in result
        assert "custom-alias" in result["custom"]

    def test_empty_policy(self):
        """空の policy でも動作。"""
        config = {"codex": {"enabled": True}, "agents": {}}
        result = route_audit.merged_aliases(config, {})
        assert isinstance(result, dict)

    def test_dedup(self):
        """重複する alias は追加しない。"""
        config = {"agents": {"dev": {"tool": "codex"}}}
        policy = {"aliases": {"codex": ["task:dev"]}}
        result = route_audit.merged_aliases(config, policy)
        # task:dev は動的に追加されているはず
        codex_aliases = result.get("codex", [])
        assert codex_aliases.count("task:dev") <= 1


class TestIsMatch:
    """is_match のテスト。"""

    def test_exact_match(self):
        """完全一致。"""
        assert route_audit.is_match("codex", "codex", {}) is True

    def test_no_match(self):
        """不一致。"""
        assert route_audit.is_match("codex", "gemini", {}) is False

    def test_empty_expected(self):
        """expected が空は False。"""
        assert route_audit.is_match("", "codex", {}) is False

    def test_empty_actual(self):
        """actual が空は False。"""
        assert route_audit.is_match("codex", "", {}) is False

    def test_skill_matches_claude_direct(self):
        """skill:* は claude-direct とマッチ。"""
        assert route_audit.is_match("claude-direct", "skill:commit", {}) is True
        assert route_audit.is_match("claude-direct", "skill:review", {}) is True

    def test_skill_does_not_match_codex(self):
        """skill:* は codex とはマッチしない。"""
        assert route_audit.is_match("codex", "skill:commit", {}) is False

    def test_alias_match(self):
        """alias 経由のマッチ。"""
        policy = {"aliases": {"codex": ["bash:codex", "task:backend-python-dev"]}}
        assert route_audit.is_match("codex", "bash:codex", policy) is True
        assert route_audit.is_match("codex", "task:backend-python-dev", policy) is True

    def test_alias_no_match(self):
        """alias に含まれないルートは不一致。"""
        policy = {"aliases": {"codex": ["bash:codex"]}}
        assert route_audit.is_match("codex", "bash:gemini", policy) is False


class TestTestCmdPattern:
    """TEST_CMD_PATTERN のテスト。"""

    def test_pytest(self):
        """pytest を検出。"""
        assert route_audit.TEST_CMD_PATTERN.search("pytest -q tests/")

    def test_npm_test(self):
        """npm test を検出。"""
        assert route_audit.TEST_CMD_PATTERN.search("npm test")

    def test_go_test(self):
        """go test を検出。"""
        assert route_audit.TEST_CMD_PATTERN.search("go test ./...")

    def test_ruff_check(self):
        """ruff check を検出。"""
        assert route_audit.TEST_CMD_PATTERN.search("ruff check .")

    def test_non_test_command(self):
        """テスト以外のコマンドは検出しない。"""
        assert route_audit.TEST_CMD_PATTERN.search("git status") is None
        assert route_audit.TEST_CMD_PATTERN.search("python app.py") is None

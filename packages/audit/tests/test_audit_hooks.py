"""audit hook ロジックのユニットテスト。"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest

from tests.module_loader import REPO_ROOT, load_module

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
    """`detect_route` のテスト。"""

    def test_bash_codex(self) -> None:
        """Bash で codex コマンドを検出した場合 bash:codex を返すことを確認する。"""
        data = {"tool_name": "Bash", "tool_input": {"command": "codex exec --model gpt-5 'hello'"}}
        route, excerpt, tool = audit_route.detect_route(data)
        assert route == "bash:codex"
        assert "codex" in excerpt
        assert tool == "Bash"

    def test_bash_gemini(self) -> None:
        """Bash で gemini コマンドを検出した場合 bash:gemini を返すことを確認する。"""
        data = {"tool_name": "Bash", "tool_input": {"command": "gemini -m model -p 'query'"}}
        route, _, _ = audit_route.detect_route(data)
        assert route == "bash:gemini"

    def test_bash_agy(self) -> None:
        """Bash で agy コマンドを検出した場合 bash:agy を返すことを確認する。"""
        data = {"tool_name": "Bash", "tool_input": {"command": 'agy -p "query" --model x'}}
        route, excerpt, tool = audit_route.detect_route(data)
        assert route == "bash:agy"
        assert "agy" in excerpt
        assert tool == "Bash"

    def test_bash_agy_after_pipe(self) -> None:
        """パイプの後段に agy が来ても検出できることを確認する。"""
        data = {"tool_name": "Bash", "tool_input": {"command": 'ls | agy -p "query"'}}
        route, _, _ = audit_route.detect_route(data)
        assert route == "bash:agy"

    def test_bash_agy_after_semicolon(self) -> None:
        """セミコロンの後段に agy が来ても検出できることを確認する。"""
        data = {"tool_name": "Bash", "tool_input": {"command": 'ls; agy -p "query"'}}
        route, _, _ = audit_route.detect_route(data)
        assert route == "bash:agy"

    def test_bash_agy_substring_not_matched(self) -> None:
        """`agy` を含む別の単語（誤マッチ候補）には反応しないことを確認する。"""
        data = {"tool_name": "Bash", "tool_input": {"command": "echo legacyagyversion"}}
        route, _, _ = audit_route.detect_route(data)
        assert route is None

    def test_bash_agy_with_codex_in_prompt_body(self) -> None:
        """agy 呼び出しのプロンプト本文に 'codex' が含まれても bash:agy に分類されることを確認する。"""
        data = {
            "tool_name": "Bash",
            "tool_input": {
                "command": "agy -p 'compare this with codex' --model gemini-3.1-pro-high"
            },
        }
        route, _, _ = audit_route.detect_route(data)
        assert route == "bash:agy"

    def test_bash_codex_with_agy_in_prompt_body(self) -> None:
        """codex 呼び出しのプロンプト本文に 'agy' が含まれても bash:codex に分類されることを確認する。"""
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": "codex exec --model gpt-5.5 '...agy...' < /dev/null"},
        }
        route, _, _ = audit_route.detect_route(data)
        assert route == "bash:codex"

    def test_bash_other(self) -> None:
        """Bash だが CLI 呼び出しでない場合は None を返すことを確認する。"""
        data = {"tool_name": "Bash", "tool_input": {"command": "ls -la"}}
        route, _, _ = audit_route.detect_route(data)
        assert route is None

    def test_bash_test_command_is_not_treated_as_route(self) -> None:
        """通常のテスト実行は route 判定せず、quality-gates 側に委譲する。"""
        data = {"tool_name": "Bash", "tool_input": {"command": "pytest -q"}}
        route, excerpt, tool = audit_route.detect_route(data)
        assert route is None
        assert excerpt == "pytest -q"
        assert tool == "Bash"

    def test_task_agent(self) -> None:
        """Task ツール呼び出しで task:<agent_type> を返すことを確認する。"""
        data = {"tool_name": "Task", "tool_input": {"subagent_type": "backend-python-dev"}}
        route, _, _ = audit_route.detect_route(data)
        assert route == "task:backend-python-dev"

    def test_agent_tool(self) -> None:
        """Agent ツール呼び出しでも task:<agent_type> を返すことを確認する。"""
        data = {"tool_name": "Agent", "tool_input": {"subagent_type": "researcher"}}
        route, _, _ = audit_route.detect_route(data)
        assert route == "task:researcher"

    def test_skill(self) -> None:
        """Skill ツール呼び出しで skill:<name> を返すことを確認する。"""
        data = {"tool_name": "Skill", "tool_input": {"skill": "commit"}}
        route, _, _ = audit_route.detect_route(data)
        assert route == "skill:commit"

    def test_unknown_tool(self) -> None:
        """未知のツールでは None を返すことを確認する。"""
        data = {"tool_name": "Read", "tool_input": {}}
        route, _, _ = audit_route.detect_route(data)
        assert route is None


# ---------------------------------------------------------------------------
# is_match (from audit-route.py)
# ---------------------------------------------------------------------------


class TestIsMatch:
    """`is_match` のテスト。"""

    def test_exact(self) -> None:
        """完全一致のケースでマッチすることを確認する。"""
        assert audit_route.is_match("codex", "codex", {})

    def test_skill_matches_via_alias(self) -> None:
        """claude-direct 予測に対し aliases に登録された skill のみマッチすることを確認する。"""
        aliases = {"claude-direct": ["skill:commit", "skill:issue-fix"]}
        assert audit_route.is_match("claude-direct", "skill:commit", aliases)
        # aliases に登録されていない skill はマッチしない
        assert not audit_route.is_match("claude-direct", "skill:unknown", aliases)

    def test_alias(self) -> None:
        """エイリアス経由でマッチすることを確認する。"""
        aliases = {"codex": ["bash:codex"]}
        assert audit_route.is_match("codex", "bash:codex", aliases)

    def test_no_match(self) -> None:
        """該当ルートなしの場合マッチしないことを確認する。"""
        assert not audit_route.is_match("codex", "gemini", {})

    def test_empty(self) -> None:
        """片方が空文字の場合マッチしないことを確認する。"""
        assert not audit_route.is_match("", "codex", {})
        assert not audit_route.is_match("codex", "", {})


# ---------------------------------------------------------------------------
# _parse_actual_route (from audit-route.py)
# ---------------------------------------------------------------------------


class TestParseActualRoute:
    """`_parse_actual_route` のテスト。"""

    def test_with_colon(self) -> None:
        """コロン区切り文字列を tool/detail に分解できることを確認する。"""
        result = audit_route._parse_actual_route("bash:codex")
        assert result == {"tool": "bash", "detail": "codex"}

    def test_without_colon(self) -> None:
        """コロン無しの場合 detail が None になることを確認する。"""
        result = audit_route._parse_actual_route("claude-direct")
        assert result == {"tool": "claude-direct", "detail": None}


# ---------------------------------------------------------------------------
# CLI extraction (from audit-cli.py)
# ---------------------------------------------------------------------------


class TestExtractCodexPrompt:
    """`extract_codex_prompt` のテスト。"""

    def test_double_quotes(self) -> None:
        """ダブルクォートで囲まれたプロンプトを抽出できることを確認する。"""
        cmd = 'codex exec --model gpt-5 --full-auto "What is 2+2?" 2>/dev/null'
        assert audit_cli.extract_codex_prompt(cmd) == "What is 2+2?"

    def test_single_quotes(self) -> None:
        """シングルクォートで囲まれたプロンプトを抽出できることを確認する。"""
        cmd = "codex exec --model gpt-5 --full-auto 'Design a REST API' 2>/dev/null"
        assert audit_cli.extract_codex_prompt(cmd) == "Design a REST API"

    def test_no_match(self) -> None:
        """codex 呼び出しでないコマンドで None を返すことを確認する。"""
        cmd = "echo hello"
        assert audit_cli.extract_codex_prompt(cmd) is None

    def test_stdin_redirect_with_full_auto(self) -> None:
        """--full-auto + stdin 封じ付きコマンドから抽出できることを確認する。"""
        cmd = 'codex exec --model gpt-5.5 --full-auto "What is 2+2?" < /dev/null 2>/dev/null'
        assert audit_cli.extract_codex_prompt(cmd) == "What is 2+2?"

    def test_stdin_redirect_without_full_auto(self) -> None:
        """--full-auto なし + stdin 封じ付きコマンドから抽出できることを確認する。"""
        cmd = 'codex exec --model gpt-5.5 --sandbox read-only "Review this" < /dev/null 2>/dev/null'
        assert audit_cli.extract_codex_prompt(cmd) == "Review this"

    def test_stdin_redirect_nospace(self) -> None:
        """`</dev/null` （スペースなし）でも抽出できることを確認する。"""
        cmd = "codex exec --sandbox read-only 'Review this' </dev/null 2>/dev/null"
        assert audit_cli.extract_codex_prompt(cmd) == "Review this"


class TestExtractGeminiPrompt:
    """`extract_gemini_prompt` のテスト。"""

    def test_double_quotes(self) -> None:
        """ダブルクォートで囲まれたプロンプトを抽出できることを確認する。"""
        cmd = 'gemini -m gemini-pro -p "Research topic" 2>/dev/null'
        assert audit_cli.extract_gemini_prompt(cmd) == "Research topic"

    def test_no_match(self) -> None:
        """-p フラグがない場合 None を返すことを確認する。"""
        cmd = "gemini --version"
        assert audit_cli.extract_gemini_prompt(cmd) is None


class TestExtractAntigravityPrompt:
    """`extract_antigravity_prompt` のテスト。"""

    def test_double_quotes(self) -> None:
        """ダブルクォートで囲まれたプロンプトを抽出できることを確認する。"""
        cmd = 'agy -p "Research topic" --model gemini-3.1-pro-high 2>/dev/null'
        assert audit_cli.extract_antigravity_prompt(cmd) == "Research topic"

    def test_single_quotes(self) -> None:
        """シングルクォートで囲まれたプロンプトを抽出できることを確認する。"""
        cmd = "agy -p 'Compare libraries' 2>/dev/null"
        assert audit_cli.extract_antigravity_prompt(cmd) == "Compare libraries"

    def test_print_long_flag(self) -> None:
        """--print 形式でも抽出できることを確認する。"""
        cmd = 'agy --print "Long form" 2>/dev/null'
        assert audit_cli.extract_antigravity_prompt(cmd) == "Long form"

    def test_no_match(self) -> None:
        """-p フラグがない場合 None を返すことを確認する。"""
        cmd = "agy --version"
        assert audit_cli.extract_antigravity_prompt(cmd) is None


class TestExtractModel:
    """`extract_model` のテスト。"""

    def test_codex_model(self) -> None:
        """codex の --model フラグからモデル名を抽出できることを確認する。"""
        cmd = "codex exec --model gpt-5.3-codex --full-auto 'hello'"
        assert audit_cli.extract_model(cmd) == "gpt-5.3-codex"

    def test_gemini_model(self) -> None:
        """gemini の -m フラグからモデル名を抽出できることを確認する。"""
        cmd = "gemini -m gemini-2.5-pro -p 'hello'"
        assert audit_cli.extract_model(cmd, tool="gemini") == "gemini-2.5-pro"

    def test_antigravity_model(self) -> None:
        """agy の --model フラグからモデル名を抽出できることを確認する。"""
        cmd = "agy -p 'hello' --model gemini-3.1-pro-high"
        assert audit_cli.extract_model(cmd, tool="antigravity") == "gemini-3.1-pro-high"


class TestClassifyError:
    """`_classify_error` のテスト。"""

    def test_success(self) -> None:
        """exit_code=0 では None を返すことを確認する。"""
        assert audit_cli._classify_error(0, "") is None

    def test_timeout(self) -> None:
        """'timed out' を含む出力で timeout を返すことを確認する。"""
        assert audit_cli._classify_error(1, "Command timed out") == "timeout"

    def test_auth(self) -> None:
        """'Unauthorized' を含む出力で auth を返すことを確認する。"""
        assert audit_cli._classify_error(1, "Unauthorized access") == "auth"

    def test_rate_limit(self) -> None:
        """'429' を含む出力で rate_limit を返すことを確認する。"""
        assert audit_cli._classify_error(1, "429 Too Many Requests") == "rate_limit"

    def test_not_found(self) -> None:
        """'command not found' を含む出力で not_found を返すことを確認する。"""
        assert audit_cli._classify_error(127, "command not found") == "not_found"

    def test_unknown(self) -> None:
        """該当パターンに一致しない場合 unknown を返すことを確認する。"""
        assert audit_cli._classify_error(1, "something else broke") == "unknown"


# ---------------------------------------------------------------------------
# select_expected_route (from audit-prompt.py)
# ---------------------------------------------------------------------------


class TestSelectExpectedRoute:
    """`select_expected_route` のテスト。"""

    def test_default_route(self) -> None:
        """ルール非マッチ時はデフォルトルートが返ることを確認する。"""
        route, rule = audit_prompt.select_expected_route(
            "hello world",
            {},
            {"default_route": "claude-direct", "rules": []},
        )
        assert route == "claude-direct"
        assert rule is None

    def test_keyword_match(self) -> None:
        """キーワードルールに一致した場合、該当ルートとルール ID が返ることを確認する。"""
        policy = {
            "default_route": "claude-direct",
            "rules": [
                {
                    "id": "r1",
                    "keywords_any": ["optimize"],
                    "expected_route": "codex",
                    "priority": 1,
                }
            ],
        }
        route, rule = audit_prompt.select_expected_route("please optimize this query", {}, policy)
        assert route == "codex"
        assert rule == "r1"


# ---------------------------------------------------------------------------
# main (from audit-route.py) — policy/config aliases の union 統合テスト
# ---------------------------------------------------------------------------


class TestMainAliasUnion:
    """`main()` を通した alias union の統合テスト。"""

    def test_policy_and_config_claude_direct_aliases_are_unioned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """policy 側の claude-direct aliases が config 側（build_aliases）の
        task:<agent> 群を握りつぶさず union されることを確認する。
        """
        project_dir = tmp_path
        (project_dir / ".claude").mkdir()

        # policy 側に claude-direct の skill エイリアスのみを定義する
        # （code-reviewer の task alias は含まない = config 側からのみ得られる）
        config_dir = project_dir / ".claude" / "config" / "audit"
        config_dir.mkdir(parents=True)
        policy = {
            "version": 3,
            "default_route": "claude-direct",
            "helper_routes": [],
            "rules": [],
            "aliases": {"claude-direct": ["skill:custom-skill"]},
        }
        (config_dir / "delegation-policy.json").write_text(json.dumps(policy), encoding="utf-8")

        # trace state: expected_route=claude-direct（code-reviewer の tool は
        # cli-tools.yaml 上 claude-direct なので task:code-reviewer は config 側の
        # build_aliases() でのみ生成される）
        state_dir = project_dir / ".claude" / "state"
        state_dir.mkdir(parents=True)
        (state_dir / "audit-trace.json").write_text(
            json.dumps({"tid": "t1", "session_id": "s1", "expected_route": "claude-direct"}),
            encoding="utf-8",
        )

        data = {
            "session_id": "s1",
            "cwd": str(project_dir),
            "tool_name": "Task",
            "tool_input": {"subagent_type": "code-reviewer"},
        }
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(data)))

        captured: dict = {}

        def fake_emit_event(event_type: str, payload: dict, **kwargs: object) -> dict:
            captured["type"] = event_type
            captured["payload"] = payload
            return payload

        monkeypatch.setattr(audit_route, "emit_event", fake_emit_event)

        audit_route.main()

        assert captured["type"] == "route_decision"
        # union されていれば task:code-reviewer 経由でマッチする
        assert captured["payload"]["matched"] is True
        assert captured["payload"]["actual"] == {"tool": "task", "detail": "code-reviewer"}

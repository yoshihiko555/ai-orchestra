"""agent-router.py の main() エンドツーエンドテスト。

stdin に JSON を流し込み、stdout の hookSpecificOutput を検証する。

2 階層構成（Issue #341）:

- Tier 1（``TestFixture*``）: tmp_path に独立した cli-tools.yaml / .local.yaml を
  用意し、live config の実値に依存しない振る舞い契約を検証する。
- Tier 2（``TestLiveConfigRoutingContract``）: 実リポジトリの実効 config を
  production helper（``load_config`` / ``get_agent_tool``）で解決し、その結果に
  基づいてマーカーの有無を検証する。tool の具体値（例: debugger==codex）は
  assert しない。meta-harness が ``cli-tools.local.yaml`` を materialize して
  tool を差し替える合法 patch でも pass し続けることを狙う。

期待値マーカー（CODEX_MARKERS / ANTIGRAVITY_MARKERS / WARN_MARKER）は
``build_cli_suggestion`` の出力を呼び出さず、このファイル冒頭で手書き定義する
（呼び出すと tautology になるため）。
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import yaml

from tests.module_loader import REPO_ROOT, load_module

os.environ["AI_ORCHESTRA_DIR"] = str(REPO_ROOT)
sys.path.insert(0, str(REPO_ROOT / "packages" / "core" / "hooks"))
sys.path.insert(0, str(REPO_ROOT / "packages" / "agent-routing" / "hooks"))

agent_router = load_module("agent_router", "packages/agent-routing/hooks/agent-router.py")
route_config = load_module("route_config", "packages/agent-routing/hooks/route_config.py")

# ---------------------------------------------------------------------------
# 期待値マーカー（手書き定数。build_cli_suggestion の呼び出し禁止）
# ---------------------------------------------------------------------------

CODEX_MARKERS: tuple[str, str] = ("Codex CLI", "codex exec")
ANTIGRAVITY_MARKERS: tuple[str, str] = ("Antigravity CLI", "agy -p")
WARN_MARKER = "[WARN]"


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


def _additional_context(result: dict) -> str:
    """_run_hook の戻り値から additionalContext を取り出す。"""
    return result.get("hookSpecificOutput", {}).get("additionalContext", "")


def _assert_marker_count(ctx: str, marker: str, expected_count: int) -> None:
    actual_count = ctx.count(marker)
    assert actual_count == expected_count, (
        f"{marker!r} should appear {expected_count} time(s), got {actual_count} in: {ctx!r}"
    )


def _assert_no_cli_markers(ctx: str) -> None:
    for marker in (*CODEX_MARKERS, *ANTIGRAVITY_MARKERS):
        _assert_marker_count(ctx, marker, 0)


def _write_cli_tools_yaml(project_dir: Path, base: dict, *, local: dict | None = None) -> None:
    """tmp_path 配下に cli-tools.yaml（+ 任意で .local.yaml）を書き込む。"""
    config_dir = project_dir / ".claude" / "config" / "agent-routing"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "cli-tools.yaml").write_text(
        yaml.safe_dump(base, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    if local is not None:
        (config_dir / "cli-tools.local.yaml").write_text(
            yaml.safe_dump(local, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )


# ---------------------------------------------------------------------------
# Tier 1: fixture ベーステスト（live config 非依存の振る舞い契約）
# ---------------------------------------------------------------------------


class TestFixtureCliSuggestionByTool:
    """fixture config の tool 値ごとに CLI 提案マーカーが正しく出ることを検証する。

    live config の実値（debugger==codex 等）には依存しない。既存の
    AGENT_TRIGGERS（トリガー語自体は固定辞書）に載っている agent 名を使い、
    tool 値だけを fixture config で上書きする。
    """

    def test_tool_codex_shows_codex_markers_exactly_once(self, tmp_path: Path) -> None:
        _write_cli_tools_yaml(tmp_path, {"agents": {"debugger": {"tool": "codex"}}})
        ctx = _additional_context(_run_hook("デバッグしてほしい", cwd=str(tmp_path)))
        assert "debugger" in ctx
        for marker in CODEX_MARKERS:
            _assert_marker_count(ctx, marker, 1)
        for marker in ANTIGRAVITY_MARKERS:
            _assert_marker_count(ctx, marker, 0)

    def test_tool_antigravity_shows_antigravity_markers_exactly_once(self, tmp_path: Path) -> None:
        _write_cli_tools_yaml(tmp_path, {"agents": {"researcher": {"tool": "antigravity"}}})
        ctx = _additional_context(
            _run_hook("最新のライブラリについてリサーチして", cwd=str(tmp_path))
        )
        assert "researcher" in ctx
        for marker in ANTIGRAVITY_MARKERS:
            _assert_marker_count(ctx, marker, 1)
        for marker in CODEX_MARKERS:
            _assert_marker_count(ctx, marker, 0)

    def test_tool_claude_direct_shows_no_cli_markers(self, tmp_path: Path) -> None:
        _write_cli_tools_yaml(tmp_path, {"agents": {"architect": {"tool": "claude-direct"}}})
        ctx = _additional_context(_run_hook("アーキテクチャを設計して", cwd=str(tmp_path)))
        assert "architect" in ctx
        _assert_no_cli_markers(ctx)

    def test_tool_auto_shows_no_cli_markers_current_spec(self, tmp_path: Path) -> None:
        """仕様 pin: build_cli_suggestion は tool=auto を None として扱う（提案なし）。"""
        _write_cli_tools_yaml(tmp_path, {"agents": {"tester": {"tool": "auto"}}})
        ctx = _additional_context(_run_hook("単体テストを追加してください", cwd=str(tmp_path)))
        assert "tester" in ctx
        assert "tool: auto" in ctx
        _assert_no_cli_markers(ctx)


class TestFixtureCliDisabledFallback:
    """codex.enabled / antigravity.enabled が false のときの claude-direct フォールバック。"""

    def test_codex_disabled_falls_back_to_claude_direct(self, tmp_path: Path) -> None:
        _write_cli_tools_yaml(
            tmp_path,
            {"codex": {"enabled": False}, "agents": {"debugger": {"tool": "codex"}}},
        )
        ctx = _additional_context(_run_hook("デバッグしてほしい", cwd=str(tmp_path)))
        assert "tool: claude-direct" in ctx
        _assert_no_cli_markers(ctx)

    def test_antigravity_disabled_falls_back_to_claude_direct(self, tmp_path: Path) -> None:
        _write_cli_tools_yaml(
            tmp_path,
            {
                "antigravity": {"enabled": False},
                "agents": {"researcher": {"tool": "antigravity"}},
            },
        )
        ctx = _additional_context(
            _run_hook("最新のライブラリについてリサーチして", cwd=str(tmp_path))
        )
        assert "tool: claude-direct" in ctx
        _assert_no_cli_markers(ctx)


class TestFixtureAntigravityModelAllowlist:
    def test_model_outside_allowlist_shows_warn(self, tmp_path: Path) -> None:
        _write_cli_tools_yaml(
            tmp_path,
            {
                "antigravity": {
                    "model": "unknown-model-x",
                    "model_allowlist": ["gemini-3.1-pro-high"],
                },
                "agents": {"researcher": {"tool": "antigravity"}},
            },
        )
        ctx = _additional_context(
            _run_hook("最新のライブラリについてリサーチして", cwd=str(tmp_path))
        )
        assert WARN_MARKER in ctx


class TestFixtureLocalYamlOverride:
    """評価 worktree の materialize（Issue #341 の中核ケース）を再現する。"""

    def test_local_yaml_overrides_single_agent_tool(self, tmp_path: Path) -> None:
        """base で tool=codex な debugger を local.yaml で tool=auto に上書きすると
        CLI 提案が消える。base の未変更キー（tester）は local の影響を受けない。
        """
        _write_cli_tools_yaml(
            tmp_path,
            {"agents": {"debugger": {"tool": "codex"}, "tester": {"tool": "codex"}}},
            local={"agents": {"debugger": {"tool": "auto"}}},
        )

        debugger_ctx = _additional_context(_run_hook("デバッグしてほしい", cwd=str(tmp_path)))
        assert "tool: auto" in debugger_ctx
        _assert_no_cli_markers(debugger_ctx)

        tester_ctx = _additional_context(
            _run_hook("単体テストを追加してください", cwd=str(tmp_path))
        )
        assert "tool: codex" in tester_ctx
        for marker in CODEX_MARKERS:
            _assert_marker_count(tester_ctx, marker, 1)


class TestFixtureLegacyToolValue:
    def test_legacy_tool_gemini_reads_as_antigravity(self, tmp_path: Path) -> None:
        """旧 tool: gemini は normalize_cli_tools_config により antigravity へ
        読み替えられる（Issue #125）。"""
        _write_cli_tools_yaml(tmp_path, {"agents": {"researcher": {"tool": "gemini"}}})
        ctx = _additional_context(
            _run_hook("最新のライブラリについてリサーチして", cwd=str(tmp_path))
        )
        assert "tool: antigravity" in ctx
        for marker in ANTIGRAVITY_MARKERS:
            _assert_marker_count(ctx, marker, 1)


class TestFixtureUndefinedOrUnknownTool:
    def test_undefined_agent_in_config_shows_no_cli_markers(self, tmp_path: Path) -> None:
        _write_cli_tools_yaml(tmp_path, {"agents": {}})
        ctx = _additional_context(_run_hook("デバッグしてほしい", cwd=str(tmp_path)))
        assert "tool: claude-direct" in ctx
        _assert_no_cli_markers(ctx)

    def test_unknown_tool_value_shows_no_cli_markers(self, tmp_path: Path) -> None:
        _write_cli_tools_yaml(tmp_path, {"agents": {"debugger": {"tool": "some-unknown-tool"}}})
        ctx = _additional_context(_run_hook("デバッグしてほしい", cwd=str(tmp_path)))
        assert "tool: some-unknown-tool" in ctx
        _assert_no_cli_markers(ctx)


class TestFixtureMalformedConfig:
    """config ファイルが壊れている場合の graceful fallback を pin する。

    ``hook_common._read_config_file`` は YAML の読み込み・パース中の例外を
    握って ``{}`` を返す。パース結果が dict 以外の場合も ``{}`` にフォール
    バックする。base 全体が壊れていれば
    ``load_cli_tools_config`` は空 config を返し、``get_agent_tool`` は
    claude-direct にフォールバックする。エージェント検出自体は AGENT_TRIGGERS
    という config 非依存の固定辞書で行われるため、config が壊れていても
    クラッシュせず routing メッセージ自体は出力されることを確認する。
    """

    def test_malformed_yaml_base_falls_back_gracefully(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".claude" / "config" / "agent-routing"
        config_dir.mkdir(parents=True)
        (config_dir / "cli-tools.yaml").write_text("agents: [unterminated\n  tool: codex")

        ctx = _additional_context(_run_hook("デバッグしてほしい", cwd=str(tmp_path)))
        assert "debugger" in ctx
        assert "tool: claude-direct" in ctx
        _assert_no_cli_markers(ctx)

    def test_null_yaml_base_falls_back_gracefully(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".claude" / "config" / "agent-routing"
        config_dir.mkdir(parents=True)
        (config_dir / "cli-tools.yaml").write_text("")

        ctx = _additional_context(_run_hook("デバッグしてほしい", cwd=str(tmp_path)))
        assert "debugger" in ctx
        assert "tool: claude-direct" in ctx
        _assert_no_cli_markers(ctx)


# ---------------------------------------------------------------------------
# Tier 2: live-config 契約テスト（実効 config 追従）
# ---------------------------------------------------------------------------


def _assert_markers_for_resolved_tool(ctx: str, tool: str) -> None:
    """resolved tool 値に応じてマーカーの有無を検証する（tool の具体値は assert しない）。"""
    if tool == "codex":
        for marker in CODEX_MARKERS:
            _assert_marker_count(ctx, marker, 1)
        for marker in ANTIGRAVITY_MARKERS:
            _assert_marker_count(ctx, marker, 0)
        return
    if tool == "antigravity":
        for marker in ANTIGRAVITY_MARKERS:
            _assert_marker_count(ctx, marker, 1)
        for marker in CODEX_MARKERS:
            _assert_marker_count(ctx, marker, 0)
        return
    # claude-direct / auto / その他未知の値は CLI 提案なし
    _assert_no_cli_markers(ctx)


class TestLiveConfigRoutingContract:
    """実効 config（cli-tools.yaml + .local.yaml 上書き）に追従する契約テスト。

    tool の具体値（例: debugger==codex）は assert しない。production helper
    （``route_config.load_config`` / ``get_agent_tool``）で解決した実効値に
    基づいてマーカーの有無を分岐する。meta-harness が
    ``cli-tools.local.yaml`` を materialize して tool を差し替える合法 patch
    でも、このテストは解決結果に追従して pass し続ける（Issue #341）。

    限界: 期待値の分岐に ``get_agent_tool`` 自身を使うため、同関数のロジック
    回帰はこの Tier では検出できない。その担保は Tier 1 の fixture テスト
    （tool 値ごとの完全固定検証）が受け持つ。
    """

    @staticmethod
    def _resolved_tool(agent_name: str) -> str:
        config = route_config.load_config({"cwd": str(REPO_ROOT)})
        return route_config.get_agent_tool(agent_name, config)

    def test_debugger_trigger_matches_resolved_tool(self) -> None:
        ctx = _additional_context(_run_hook("デバッグしてほしい"))
        assert "debugger" in ctx
        _assert_markers_for_resolved_tool(ctx, self._resolved_tool("debugger"))

    def test_architect_trigger_matches_resolved_tool(self) -> None:
        ctx = _additional_context(_run_hook("アーキテクチャを設計して"))
        assert "architect" in ctx
        _assert_markers_for_resolved_tool(ctx, self._resolved_tool("architect"))

    def test_researcher_trigger_matches_resolved_tool(self) -> None:
        ctx = _additional_context(_run_hook("最新のライブラリについてリサーチして"))
        assert "researcher" in ctx
        _assert_markers_for_resolved_tool(ctx, self._resolved_tool("researcher"))

    def test_pdf_trigger_routes_to_researcher_matches_resolved_tool(self) -> None:
        ctx = _additional_context(_run_hook("このPDF見てください"))
        assert "researcher" in ctx
        _assert_markers_for_resolved_tool(ctx, self._resolved_tool("researcher"))

    def test_codebase_trigger_matches_resolved_tool(self) -> None:
        ctx = _additional_context(_run_hook("コードベース全体を理解したい"))
        assert "researcher" in ctx
        _assert_markers_for_resolved_tool(ctx, self._resolved_tool("researcher"))


# ---------------------------------------------------------------------------
# エージェント検出時の出力（config 値に依存しない既存契約）
# ---------------------------------------------------------------------------


class TestHookOutputForAgentDetection:
    def test_tester_agent_detected(self) -> None:
        result = _run_hook("単体テストを追加してください")
        ctx = _additional_context(result)
        assert "Agent Routing" in ctx
        assert "tester" in ctx
        assert 'Task(subagent_type="tester"' in ctx

    def test_hook_event_name_is_user_prompt_submit(self) -> None:
        result = _run_hook("テストを書いて")
        hook_out = result.get("hookSpecificOutput", {})
        assert hook_out.get("hookEventName") == "UserPromptSubmit"


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

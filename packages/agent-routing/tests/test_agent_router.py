"""agent-routing パッケージのルーティングロジックテスト。

テスト対象:
- route_config.detect_agent: プロンプトからエージェントを検出
- route_config.get_agent_tool: エージェントの tool を取得
- route_config.build_aliases: tool ごとの alias リストを構築
- route_config.build_cli_suggestion: CLI コマンド提案文字列を構築
"""

from __future__ import annotations

import sys

from tests.module_loader import REPO_ROOT, load_module

sys.path.insert(0, str(REPO_ROOT / "packages" / "core" / "hooks"))
route_config = load_module("route_config", "packages/agent-routing/hooks/route_config.py")


# ---------------------------------------------------------------------------
# is_task_notification
# ---------------------------------------------------------------------------


def test_is_task_notification_returns_true_for_notification_block() -> None:
    prompt = "<task-notification>\n<task-id>abc</task-id>\n</task-notification>"
    assert route_config.is_task_notification(prompt) is True


def test_is_task_notification_allows_leading_whitespace_before_notification() -> None:
    prompt = " \n\t<task-notification>\n<task-id>abc</task-id>"
    assert route_config.is_task_notification(prompt) is True


def test_is_task_notification_returns_false_for_normal_prompt() -> None:
    assert route_config.is_task_notification("テストを書いて") is False


def test_is_task_notification_returns_false_when_notification_tag_is_in_body() -> None:
    prompt = "この文章の <task-notification> タグを説明して"
    assert route_config.is_task_notification(prompt) is False


# ---------------------------------------------------------------------------
# detect_agent: 日本語トリガー
# ---------------------------------------------------------------------------


def test_detect_agent_detects_tester() -> None:
    agent, trigger = route_config.detect_agent("単体テストのカバレッジを上げたい")
    assert agent == "tester"
    assert trigger in {"テスト", "単体テスト", "カバレッジ"}


def test_detect_agent_detects_debugger() -> None:
    agent, trigger = route_config.detect_agent("このエラーをデバッグしたい")
    assert agent == "debugger"
    assert trigger in {"デバッグ", "エラー"}


def test_detect_agent_detects_researcher() -> None:
    agent, trigger = route_config.detect_agent("最新のライブラリについて調べてください")
    assert agent == "researcher"
    assert trigger == "調べて"


def test_detect_agent_detects_architect() -> None:
    agent, trigger = route_config.detect_agent("システムのアーキテクチャを設計して")
    assert agent == "architect"
    assert trigger == "アーキテクチャ"


def test_detect_agent_detects_planner() -> None:
    agent, trigger = route_config.detect_agent("タスクを計画してほしい")
    assert agent == "planner"
    assert trigger == "計画"


# ---------------------------------------------------------------------------
# detect_agent: 英語トリガー
# ---------------------------------------------------------------------------


def test_detect_agent_detects_api_designer_from_english_prompt() -> None:
    agent, trigger = route_config.detect_agent("please help with api design and endpoint naming")
    assert agent == "api-designer"
    assert trigger in {"api design", "endpoint"}


def test_detect_agent_detects_frontend_dev_from_english_prompt() -> None:
    agent, trigger = route_config.detect_agent("build a react component for the dashboard")
    assert agent == "frontend-dev"
    assert trigger.lower() == "react"


def test_detect_agent_detects_security_reviewer() -> None:
    agent, trigger = route_config.detect_agent("run a security review on this code")
    assert agent == "security-reviewer"
    assert trigger == "security review"


def test_detect_agent_detects_adversarial_reviewer() -> None:
    agent, trigger = route_config.detect_agent("この実装の敵対的検証をして")
    assert agent == "adversarial-reviewer"
    assert trigger == "敵対的検証"


def test_detect_agent_detects_finding_verifier() -> None:
    agent, trigger = route_config.detect_agent("この指摘の反証をして")
    assert agent == "finding-verifier"
    assert trigger == "指摘の反証"


def test_detect_agent_finding_verifier_not_shadowed_by_code_reviewer() -> None:
    agent, trigger = route_config.detect_agent("反証機能のコードレビューをして")
    assert agent == "code-reviewer"
    assert trigger == "コードレビュー"


def test_detect_agent_finding_verifier_phrase_not_shadowed_by_code_reviewer() -> None:
    agent, trigger = route_config.detect_agent("レビュー指摘の検証機能をコードレビューして")
    assert agent == "code-reviewer"
    assert trigger == "コードレビュー"


def test_detect_agent_finding_verifier_phrase_not_shadowed_by_code_reviewer_en() -> None:
    agent, trigger = route_config.detect_agent("finding verification code review")
    assert agent == "code-reviewer"
    assert trigger == "code review"


def test_detect_agent_adversarial_review_not_shadowed_by_code_reviewer() -> None:
    agent, trigger = route_config.detect_agent("この実装の敵対的レビューをして")
    assert agent == "adversarial-reviewer"
    assert trigger == "敵対的レビュー"


def test_detect_agent_robustness_review_not_shadowed_by_code_reviewer() -> None:
    agent, trigger = route_config.detect_agent("堅牢性レビューをして")
    assert agent == "adversarial-reviewer"
    assert trigger == "堅牢性レビュー"


# ---------------------------------------------------------------------------
# detect_agent: 該当なし
# ---------------------------------------------------------------------------


def test_detect_agent_returns_none_when_no_match() -> None:
    agent, trigger = route_config.detect_agent("just saying hello")
    assert agent is None
    assert trigger == ""


def test_detect_agent_returns_none_for_empty_prompt() -> None:
    agent, trigger = route_config.detect_agent("")
    assert agent is None
    assert trigger == ""


def test_detect_agent_none_allowlist_preserves_existing_first_match() -> None:
    agent, trigger = route_config.detect_agent("plan this implementation in python", None)
    assert agent == "planner"
    assert trigger == "plan"


def test_detect_agent_skips_disallowed_agent_and_finds_next_match() -> None:
    agent, trigger = route_config.detect_agent(
        "plan this implementation in python", {"backend-python-dev"}
    )
    assert agent == "backend-python-dev"
    assert trigger.lower() == "python"


def test_detect_agent_keeps_allowed_debugger_for_bug_fix() -> None:
    agent, trigger = route_config.detect_agent("bug fix", {"debugger", "general-purpose"})
    assert agent == "debugger"
    assert trigger == "bug"


def test_detect_agent_excludes_disallowed_non_implementation_role() -> None:
    agent, trigger = route_config.detect_agent("要件を整理する", {"general-purpose"})
    assert agent is None
    assert trigger == ""


def test_detect_agent_empty_allowlist_returns_none() -> None:
    agent, trigger = route_config.detect_agent("Pythonで実装する", set())
    assert agent is None
    assert trigger == ""


# ---------------------------------------------------------------------------
# detect_agent: 単語境界による誤検知回帰テスト
# ---------------------------------------------------------------------------


def test_detect_agent_does_not_misfire_frontend_dev_on_quick() -> None:
    """ "quick" の部分文字列 "ui" で frontend-dev が誤検知されないこと。"""
    agent, _ = route_config.detect_agent("Let's build a quick prototype")
    assert agent != "frontend-dev"


def test_detect_agent_does_not_misfire_tester_on_latest() -> None:
    """ "latest" の部分文字列 "test" で tester が誤検知されないこと。"""
    agent, _ = route_config.detect_agent("what is the latest release")
    assert agent != "tester"


def test_detect_agent_does_not_misfire_backend_go_dev() -> None:
    """ "go" は単語境界で判定されるが、辞書順で先に "plan" にマッチするため
    backend-go-dev が誤って選ばれないこと（planner が選ばれるのは正当）。
    """
    agent, _ = route_config.detect_agent("let's go over the plan")
    assert agent != "backend-go-dev"


def test_detect_agent_still_detects_legitimate_ascii_trigger() -> None:
    """単語境界化後も正当な ASCII トリガーは引き続き検出される。"""
    agent, trigger = route_config.detect_agent("write a test for this")
    assert agent == "tester"
    assert trigger == "test"


def test_detect_agent_still_detects_legitimate_mixed_ja_ascii_prompt() -> None:
    """日本語文中の ASCII トリガー（React / UI）も引き続き検出される。"""
    agent, trigger = route_config.detect_agent("React の UI を実装して")
    assert agent == "frontend-dev"
    assert trigger in {"React", "UI"}


def test_detect_agent_detects_ascii_trigger_adjacent_to_japanese() -> None:
    agent, _ = route_config.detect_agent("ReactのUIを実装して")
    assert agent == "frontend-dev"


def test_detect_agent_detects_dotted_ascii_trigger_adjacent_to_japanese() -> None:
    agent, _ = route_config.detect_agent("Next.jsで画面を作る")
    assert agent == "frontend-dev"


def test_detect_agent_itself_does_not_require_request_form() -> None:
    """loop-harness は Issue のタイトルとラベルを渡すため、detect_agent は節に分けない。"""
    agent, _ = route_config.detect_agent("feat: Python の依存を整理する\nenhancement")
    assert agent == "backend-python-dev"


# ---------------------------------------------------------------------------
# _routing_request_clauses: 依頼形ゲート（EV-34）
# ---------------------------------------------------------------------------


def _requested_agent(prompt: str) -> str | None:
    """agent-router と同じく、依頼の節だけを detect_agent に渡した結果を返す。"""
    clauses = route_config._routing_request_clauses(prompt)
    agent, _ = route_config.detect_agent("\n".join(clauses))
    return agent


def test_request_clauses_keep_japanese_request() -> None:
    assert route_config._routing_request_clauses("単体テストを追加してください") == [
        "単体テストを追加してください"
    ]
    assert _requested_agent("単体テストを追加してください") == "tester"


def test_request_clauses_keep_english_request() -> None:
    assert _requested_agent("Please write a unit test for the parser") == "tester"
    assert _requested_agent("Can you investigate why the deploy is slow?") == "researcher"


def test_request_clauses_keep_english_build_request() -> None:
    assert _requested_agent("Please build a React component for the dashboard") == "frontend-dev"


def test_request_clauses_keep_english_plan_request() -> None:
    assert _requested_agent("Plan the implementation milestones") == "planner"


def test_request_clauses_keep_more_english_request_verbs() -> None:
    for prompt in (
        "Please refactor the parser",
        "Please optimize the parser",
        "Please test the parser",
        "Please document the parser",
        "Please analyze the parser",
        "Please update the parser",
    ):
        assert route_config._routing_request_clauses(prompt) == [prompt]


def test_request_clauses_keep_japanese_compound_words() -> None:
    for prompt in (
        "変更前後で性能を比べて",
        "最後でエラーが出る原因を調べて",
        "午後で空いている時間を調べて",
        "明日以後で空いている時間を調べて",
    ):
        assert route_config._routing_request_clauses(prompt) == [prompt]


def test_request_clauses_keep_english_request_with_negated_outcome() -> None:
    assert _requested_agent("Can you debug why the tests don't pass?") == "debugger"
    assert _requested_agent("Could you investigate why builds do not finish?") == "researcher"


def test_request_clauses_drop_japanese_report() -> None:
    assert route_config._routing_request_clauses("単体テストを追加しました。全部通っています") == []


def test_request_clauses_drop_english_report() -> None:
    assert _requested_agent("I wrote unit tests for the parser yesterday.") is None


def test_request_clauses_drop_completed_or_deferred_work() -> None:
    assert _requested_agent("テストはもう追加してもらったので確認して") is None
    assert _requested_agent("デバッグは後で確認してください") is None
    assert _requested_agent("I want to review the schema later") is None
    assert route_config._routing_request_clauses("単体テストは後で追加してください") == []
    assert route_config._routing_request_clauses("デバッグは後で確認してください") == []


def test_request_clauses_drop_negated_request() -> None:
    assert _requested_agent("今回はテストを追加しなくていいので確認して") is None
    assert _requested_agent("Please do not run the tests, I already ran them") is None


def test_request_clauses_drop_directly_negated_english_verbs() -> None:
    for prompt in (
        "Can you review the schema, but do not build the parser",
        "Can you review the schema, but don't plan the rollout",
        "Can you review the schema, but do not refactor the parser",
        "Can you review the schema, but don't optimize the parser",
        "Can you review the schema, but do not test the parser",
        "Can you review the schema, but don't document the parser",
        "Can you review the schema, but do not analyze the parser",
        "Can you review the schema, but don't update the parser",
        "Can you review the schema, but don't run the parser",
    ):
        assert route_config._routing_request_clauses(prompt) == []


def test_request_clauses_keep_only_the_request_sentence() -> None:
    clauses = route_config._routing_request_clauses(
        "テストを書きました。次はREADMEを書いてください"
    )
    assert clauses == ["次はREADMEを書いてください"]
    assert _requested_agent("テストを書きました。次はREADMEを書いてください") == "docs-writer"


def test_request_clauses_split_english_sentences() -> None:
    clauses = route_config._routing_request_clauses(
        "I already wrote tests. Can you review the schema?"
    )
    assert [clause.strip() for clause in clauses] == ["Can you review the schema"]


def test_request_clauses_keep_periods_inside_names() -> None:
    prompt = "Next.js の README.md を直して"
    assert route_config._routing_request_clauses(prompt) == [prompt]


def test_request_clauses_drop_quoted_lines() -> None:
    prompt = "> 単体テストを追加してください\nこれは前回の依頼の写しです"
    assert route_config._routing_request_clauses(prompt) == []


def test_request_clauses_drop_fenced_code() -> None:
    prompt = "```\nplease debug this\n```\nこのログの意味は？"
    assert _requested_agent(prompt) is None


def test_request_clauses_drop_pasted_block_when_outer_text_requests() -> None:
    prompt = "<pasted_content>エラーが出ました。デバッグしてください</pasted_content>\nこのメモを整理して"
    assert _requested_agent(prompt) is None


def test_request_clauses_read_pasted_block_when_it_is_the_whole_request() -> None:
    prompt = "<pasted_content>単体テストを追加してください</pasted_content>"
    assert _requested_agent(prompt) == "tester"


def test_request_clauses_drop_nested_pasted_block() -> None:
    prompt = (
        "<pasted_content>a<pasted_content>b</pasted_content>c</pasted_content>\nこのメモを整理して"
    )
    assert route_config._routing_request_clauses(prompt) == ["このメモを整理して"]
    assert route_config._remove_pasted_blocks(prompt) == ("c</pasted_content>\nこのメモを整理して")


def test_remove_pasted_blocks_preserve_unmatched_tags() -> None:
    for prompt in (
        "メモ<pasted_content>本文",
        "本文</pasted_content>メモ",
        "本文</pasted_content>メモ<pasted_content>続き",
    ):
        assert route_config._remove_pasted_blocks(prompt) == prompt


def test_remove_pasted_blocks_remove_case_insensitive_multiline_blocks() -> None:
    prompt = (
        '前<Pasted_Content source="memo">\n本文\n</PASTED_CONTENT>中'
        "<pasted_content>別の本文</pasted_content>後<pasted_content>未完の本文"
    )
    assert route_config._remove_pasted_blocks(prompt) == "前中後<pasted_content>未完の本文"


def test_remove_pasted_blocks_bound_tag_attributes() -> None:
    attributes = " " * 256
    prompt = f"前<pasted_content{attributes}>本文</pasted_content{attributes}>後"
    assert route_config._remove_pasted_blocks(prompt) == "前後"
    for prompt in (
        f"前<pasted_content{attributes} >本文</pasted_content>後",
        f"前<pasted_content>本文</pasted_content{attributes} >後",
    ):
        assert route_config._remove_pasted_blocks(prompt) == prompt


def test_request_clauses_handle_many_unclosed_pasted_tags_quickly() -> None:
    import time

    prompt = "<pasted_content>" * 20_000 + "\n単体テストを追加してください"
    started = time.monotonic()
    clauses = route_config._routing_request_clauses(prompt)
    assert time.monotonic() - started < 2.0
    assert clauses == ["単体テストを追加してください"]


def test_request_clauses_handle_many_incomplete_pasted_tags_quickly() -> None:
    import time

    prompt = "<pasted_content" * 20_000
    started = time.monotonic()
    clauses = route_config._routing_request_clauses(prompt)
    assert time.monotonic() - started < 2.0
    assert clauses == []


def test_request_clauses_empty_prompt() -> None:
    assert route_config._routing_request_clauses("") == []


# ---------------------------------------------------------------------------
# get_agent_tool
# ---------------------------------------------------------------------------


def test_get_agent_tool_returns_configured_tool() -> None:
    config = {"agents": {"tester": {"tool": "codex", "sandbox": "workspace-write"}}}
    assert route_config.get_agent_tool("tester", config) == "codex"


def test_get_agent_tool_from_config_multiple() -> None:
    config = {
        "agents": {
            "architect": {"tool": "claude-direct"},
            "ai-dev": {"tool": "codex"},
        }
    }
    assert route_config.get_agent_tool("architect", config) == "claude-direct"
    assert route_config.get_agent_tool("ai-dev", config) == "codex"


def test_get_agent_tool_returns_claude_direct_for_missing_agent() -> None:
    config = {"agents": {}}
    assert route_config.get_agent_tool("unknown", config) == "claude-direct"


def test_get_agent_tool_returns_claude_direct_for_non_dict_config() -> None:
    config = {"agents": {"broken": "not-a-dict"}}
    assert route_config.get_agent_tool("broken", config) == "claude-direct"


def test_get_agent_tool_empty_config() -> None:
    assert route_config.get_agent_tool("architect", {}) == "claude-direct"


# ---------------------------------------------------------------------------
# build_aliases
# ---------------------------------------------------------------------------


def test_build_aliases_from_config() -> None:
    config = {
        "agents": {
            "architect": {"tool": "claude-direct"},
            "debugger": {"tool": "codex"},
            "researcher": {"tool": "antigravity"},
            "general-purpose": {"tool": "auto"},
        }
    }
    aliases = route_config.build_aliases(config)
    assert "task:architect" in aliases["claude-direct"]
    assert "task:debugger" in aliases["codex"]
    assert "task:researcher" in aliases["antigravity"]
    assert "task:general-purpose" in aliases["auto"]


def test_build_aliases_legacy_gemini_tool_value() -> None:
    """旧 tool 値 gemini は antigravity に読み替えられる。"""
    config = {"agents": {"researcher": {"tool": "gemini"}}}
    aliases = route_config.build_aliases(config)
    assert "task:researcher" in aliases["antigravity"]


def test_build_aliases_follows_config_change() -> None:
    """config 変更で aliases が自動追従。"""
    v1 = {"agents": {"architect": {"tool": "claude-direct"}}}
    v2 = {"agents": {"architect": {"tool": "codex"}}}
    assert "task:architect" in route_config.build_aliases(v1)["claude-direct"]
    assert "task:architect" in route_config.build_aliases(v2)["codex"]
    assert "task:architect" not in route_config.build_aliases(v2).get("claude-direct", [])


def test_build_aliases_base_aliases_present() -> None:
    """基本 aliases（bash:codex 等）は常に存在。"""
    aliases = route_config.build_aliases({})
    assert "bash:codex" in aliases["codex"]
    assert "bash:agy" in aliases["antigravity"]
    assert "bash:codex" in aliases["auto"]
    assert "bash:agy" in aliases["auto"]


# ---------------------------------------------------------------------------
# build_cli_suggestion
# ---------------------------------------------------------------------------


def test_build_cli_suggestion_codex() -> None:
    config = {
        "codex": {
            "model": "gpt-5.3-codex",
            "sandbox": {"analysis": "read-only"},
            "flags": "--full-auto",
        },
    }
    result = route_config.build_cli_suggestion("codex", "tester", "テスト", config)
    assert result is not None
    assert "Codex CLI" in result
    assert "gpt-5.3-codex" in result
    assert "read-only" in result
    assert "--full-auto" in result


def test_build_cli_suggestion_antigravity() -> None:
    config = {"antigravity": {"model": "gemini-3.1-pro-high"}}
    result = route_config.build_cli_suggestion("antigravity", "researcher", "調べて", config)
    assert result is not None
    assert "Antigravity CLI" in result
    assert "gemini-3.1-pro-high" in result
    assert "agy -p" in result


def test_build_cli_suggestion_antigravity_no_model() -> None:
    config = {"antigravity": {"model": ""}}
    result = route_config.build_cli_suggestion("antigravity", "researcher", "調べて", config)
    assert result is not None
    assert "--model" not in result


def test_build_cli_suggestion_claude_direct_returns_none() -> None:
    config = {}
    result = route_config.build_cli_suggestion("claude-direct", "planner", "計画", config)
    assert result is None


def test_build_cli_suggestion_antigravity_no_stdin_redirect() -> None:
    """EV-11: agy -p は非対話完結のため stdin リダイレクト不要。

    `< /dev/null` は含まれないこと（`2>/dev/null` は許容されるため
    "/dev/null" 全体ではなく "< /dev/null" を厳密に検証する）。
    """
    config = {"antigravity": {"model": "gemini-3.1-pro-high"}}
    result = route_config.build_cli_suggestion("antigravity", "researcher", "調べて", config)
    assert result is not None
    assert "< /dev/null" not in result


def test_build_cli_suggestion_codex_includes_stdin_redirect() -> None:
    """EV-10: Codex CLI 呼び出しは stdin を `< /dev/null` で封じる。

    codex-delegation.md「Non-Interactive 実行」の規則（stdin を開いたままだと
    codex exec が入力待ちで無限ハングする）に従い、提案文字列に `< /dev/null` が
    含まれることを正で確認する。antigravity 側の否定（EV-11）と対になる観点。
    """
    config = {
        "codex": {
            "model": "gpt-5.3-codex",
            "sandbox": {"analysis": "read-only"},
            "flags": "--full-auto",
        },
    }
    result = route_config.build_cli_suggestion("codex", "tester", "テスト", config)
    assert result is not None
    assert "< /dev/null" in result


def test_build_cli_suggestion_codex_shows_analysis_sandbox_only() -> None:
    """EV-22: hook 提案は分析用（analysis）sandbox のみを表示する現状実装（仕様未文書化）の確認。

    implementation sandbox の値は提案文字列に含まれないこと。
    """
    config = {
        "codex": {
            "model": "gpt-5.3-codex",
            "sandbox": {"analysis": "dummy-analysis-mode", "implementation": "dummy-impl-mode"},
            "flags": "--full-auto",
        },
    }
    result = route_config.build_cli_suggestion("codex", "tester", "テスト", config)
    assert result is not None
    assert "dummy-analysis-mode" in result
    assert "dummy-impl-mode" not in result


def test_request_clauses_handle_many_blank_lines_quickly() -> None:
    """空行が続く入力でも引用行の除去が二乗時間にならない。"""
    import time

    started = time.monotonic()
    clauses = route_config._routing_request_clauses("\n" * 100_000 + "単体テストを追加してください")
    assert time.monotonic() - started < 2.0
    assert clauses == ["単体テストを追加してください"]


def test_request_clauses_drop_indented_quoted_lines() -> None:
    prompt = "  > 単体テストを追加してください\n\t> README を直してください\n引用だけです"
    assert route_config._routing_request_clauses(prompt) == []

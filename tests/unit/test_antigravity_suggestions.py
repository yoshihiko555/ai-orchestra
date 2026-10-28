"""antigravity-suggestions hook のユニットテスト。"""

from __future__ import annotations

import io
import json

import pytest

from tests.module_loader import load_module

antigravity_hook = load_module(
    "suggest_antigravity",
    "packages/antigravity-suggestions/hooks/suggest-antigravity-research.py",
)


class TestShouldSuggestAntigravity:
    """should_suggest_antigravity のテスト。"""

    def test_simple_lookup_skipped(self):
        """SIMPLE_LOOKUP_PATTERNS はスキップ。"""
        assert antigravity_hook.should_suggest_antigravity("error message python")[0] is False
        assert antigravity_hook.should_suggest_antigravity("check version")[0] is False
        assert antigravity_hook.should_suggest_antigravity("release notes v2")[0] is False
        assert antigravity_hook.should_suggest_antigravity("changelog update")[0] is False

    @pytest.mark.parametrize(
        "query",
        [
            "python documentation for asyncio",
            "react best practice for state management",
            "python library",
            "django guide",
            "asyncio example",
            "react framework tutorial",
            "software architecture patterns",
            "API reference specification",
        ],
    )
    def test_generic_research_words_alone_do_not_suggest(self, query):
        """資料名や汎用語だけでは明確な調査意図と扱わない。"""
        assert antigravity_hook.should_suggest_antigravity(query)[0] is False

    @pytest.mark.parametrize(
        "query",
        [
            "compare asyncio and trio",
            "compare NumPy and pandas for arrays",
            "compare difflib and RapidFuzz for comparing strings",
            "react vs vue 2026",
            "postgres versus mysql for analytics",
            "Next.js と Remix の違い",
            "which is better: uv or poetry",
            "asyncio vs trio",
            "asyncio vs. trio",
            "asyncio versus trio",
            "asyncio と trio の違い",
            "asyncio と trio はどっちがよいか",
            "asyncio or trio which is better",
            "comparison of library options",
            "tradeoff between queues and streams",
            "evaluate authentication providers",
            "alternatives to Redis",
            "migrate from Django 4 to 5",
            "API versioning migration patterns",
            "PostgreSQL と MySQL を比較したい",
            "認証ライブラリを選定したい",
            "Django 4 から 5 への移行方針",
        ],
    )
    def test_explicit_research_intent_suggests(self, query):
        """比較・評価・代替・移行などの明確な意図があれば提案する。"""
        should_suggest, reason = antigravity_hook.should_suggest_antigravity(query)
        assert should_suggest is True
        assert reason

    @pytest.mark.parametrize(
        "query",
        [
            "compare",
            "how to compare two strings in python",
            "how to compare two numbers in javascript",
            "compare integers in Python",
            "how to compare two arrays in numpy",
            "compare two lists for equality",
            "compare two strings and compare two numbers in python",
            "django migration error message",
            "alembic migration error message",
            "django migrate traceback",
            "migration stack trace",
            "移行時のエラーを調べたい",
            "移行時の例外の直し方",
        ],
    )
    def test_comparison_operations_and_migration_troubleshooting_do_not_suggest(self, query):
        """値の比較方法や単純な移行トラブル調査だけでは提案しない。"""
        assert antigravity_hook.should_suggest_antigravity(query) == (False, "")

    @pytest.mark.parametrize(
        "query",
        [
            "compare Django and Flask migration error handling",
            "Django vs Flask migration error handling",
            "Django versus Flask migration traceback",
            "移行エラーが出るので Django と Flask を比較したい",
            "移行時の例外を踏まえて代替ライブラリを選定したい",
            "alternatives to Django migrations after a stack trace",
        ],
    )
    def test_explicit_selection_outranks_migration_troubleshooting(self, query):
        """トラブル調査の語があっても明確な比較・代替選定は提案する。"""
        should_suggest, reason = antigravity_hook.should_suggest_antigravity(query)
        assert should_suggest is True
        assert reason

    def test_library_comparison(self):
        """library + comparison で True。"""
        result, reason = antigravity_hook.should_suggest_antigravity(
            "comparison of library options"
        )
        assert result is True

    def test_migration_guide(self):
        """migration キーワードで True。"""
        result, reason = antigravity_hook.should_suggest_antigravity(
            "django migration guide v4 to v5"
        )
        assert result is True

    def test_long_query_alone_does_not_suggest(self):
        """クエリ長だけでは提案しない。"""
        long_query = "How to implement a custom authentication middleware in FastAPI " * 3
        assert len(long_query) > 100
        assert antigravity_hook.should_suggest_antigravity(long_query)[0] is False

    @pytest.mark.parametrize("query", ["", "get info"])
    @pytest.mark.parametrize(
        "url",
        [
            "https://docs.example.com/api-reference",
            "https://acme.test/comparison/migration/alternatives",
        ],
    )
    def test_url_alone_does_not_suggest(self, query, url):
        """URL に調査語があっても query に意図がなければ提案しない。"""
        assert antigravity_hook.should_suggest_antigravity(query, url=url)[0] is False

    def test_lookup_word_does_not_override_explicit_research_intent(self):
        """単純確認の語と共存しても明確な比較・移行意図は提案する。"""
        assert (
            antigravity_hook.should_suggest_antigravity("compare latest version migration options")[
                0
            ]
            is True
        )

    def test_simple_query_no_indicator(self):
        """短いクエリで indicator なしは False。"""
        result, _ = antigravity_hook.should_suggest_antigravity("how to fix this")
        assert result is False

    def test_simple_lookup_in_url(self):
        """URL を伴う単純確認でも提案しない。"""
        result, _ = antigravity_hook.should_suggest_antigravity(
            "check", url="https://releases.acme.test/changelog"
        )
        assert result is False

    def test_empty_query(self):
        """空クエリは False。"""
        result, _ = antigravity_hook.should_suggest_antigravity("")
        assert result is False


class TestBuildAntigravityCommand:
    """_build_antigravity_command のテスト。"""

    def test_with_model(self):
        """モデル指定付きコマンド。"""
        config = {"antigravity": {"model": "gemini-3.1-pro-high"}}
        result = antigravity_hook._build_antigravity_command(config)
        assert "--model gemini-3.1-pro-high" in result
        assert "agy" in result

    def test_no_model(self):
        """モデル未指定。"""
        result = antigravity_hook._build_antigravity_command({})
        assert "--model" not in result
        assert "agy" in result

    def test_model_not_in_allowlist_warns(self):
        """allowlist 未掲載モデルは WARN を併記。"""
        config = {
            "antigravity": {
                "model": "totally-bogus",
                "model_allowlist": ["gemini-3.1-pro-high"],
            }
        }
        result = antigravity_hook._build_antigravity_command(config)
        assert "[WARN]" in result

    def test_model_in_allowlist_no_warn(self):
        """allowlist 掲載モデルは WARN なし。"""
        config = {
            "antigravity": {
                "model": "gemini-3.1-pro-high",
                "model_allowlist": ["gemini-3.1-pro-high"],
            }
        }
        result = antigravity_hook._build_antigravity_command(config)
        assert "[WARN]" not in result


class TestAntigravityMain:
    """suggest-antigravity-research main() のテスト。"""

    def test_antigravity_disabled_exits(self, monkeypatch):
        """Antigravity 無効時は exit(0)。"""
        monkeypatch.setattr(antigravity_hook, "is_cli_enabled", lambda *a: False)
        monkeypatch.setattr(antigravity_hook, "load_cli_tools_config", lambda *a: {})
        monkeypatch.setattr(
            "sys.stdin",
            io.StringIO(
                json.dumps(
                    {
                        "tool_name": "WebSearch",
                        "tool_input": {"query": "compare asyncio and trio"},
                        "cwd": "/project",
                    }
                )
            ),
        )
        with pytest.raises(SystemExit, match="0"):
            antigravity_hook.main()

    def test_legacy_gemini_disabled_exits(self, monkeypatch, capsys):
        """旧 gemini.enabled: false（.local.yaml 残存）でも提案を抑制する。

        load_cli_tools_config は base/local 正規化・マージ済みの結果を返すため、
        ここではその結果として得られる正規化後の dict（antigravity.enabled: False
        にフォールバック済み）をモックする。
        """
        monkeypatch.setattr(
            antigravity_hook,
            "load_cli_tools_config",
            lambda *a: {
                "antigravity": {"enabled": False},
                "gemini": {"enabled": False},
            },
        )
        monkeypatch.setattr(
            "sys.stdin",
            io.StringIO(
                json.dumps(
                    {
                        "tool_name": "WebSearch",
                        "tool_input": {"query": "compare asyncio and trio"},
                        "cwd": "/project",
                    }
                )
            ),
        )
        with pytest.raises(SystemExit, match="0"):
            antigravity_hook.main()

        captured = capsys.readouterr()
        assert captured.out.strip() == ""

    def test_websearch_suggestion(self, monkeypatch, capsys):
        """WebSearch の明確な調査意図に対して提案する。"""
        monkeypatch.setattr(antigravity_hook, "is_cli_enabled", lambda *a: True)
        monkeypatch.setattr(antigravity_hook, "load_cli_tools_config", lambda *a: {})
        monkeypatch.setattr(
            "sys.stdin",
            io.StringIO(
                json.dumps(
                    {
                        "tool_name": "WebSearch",
                        "tool_input": {"query": "compare asyncio and trio"},
                        "cwd": "/project",
                    }
                )
            ),
        )
        with pytest.raises(SystemExit, match="0"):
            antigravity_hook.main()

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert "Antigravity Suggestion" in output["hookSpecificOutput"]["additionalContext"]
        assert "agy" in output["hookSpecificOutput"]["additionalContext"]

    def test_webfetch_with_research_prompt(self, monkeypatch, capsys):
        """WebFetch は URL でなく prompt の明確な調査意図で提案する。"""
        monkeypatch.setattr(antigravity_hook, "is_cli_enabled", lambda *a: True)
        monkeypatch.setattr(antigravity_hook, "load_cli_tools_config", lambda *a: {})
        monkeypatch.setattr(
            "sys.stdin",
            io.StringIO(
                json.dumps(
                    {
                        "tool_name": "WebFetch",
                        "tool_input": {
                            "url": "https://docs.example.com/api-reference",
                            "prompt": "compare API migration options",
                        },
                        "cwd": "/project",
                    }
                )
            ),
        )
        with pytest.raises(SystemExit, match="0"):
            antigravity_hook.main()

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert "Antigravity Suggestion" in output["hookSpecificOutput"]["additionalContext"]

    def test_no_suggestion(self, monkeypatch, capsys):
        """indicator なしの場合は出力なし。"""
        monkeypatch.setattr(antigravity_hook, "is_cli_enabled", lambda *a: True)
        monkeypatch.setattr(antigravity_hook, "load_cli_tools_config", lambda *a: {})
        monkeypatch.setattr(
            "sys.stdin",
            io.StringIO(
                json.dumps(
                    {
                        "tool_name": "WebSearch",
                        "tool_input": {"query": "simple lookup"},
                        "cwd": "/project",
                    }
                )
            ),
        )
        with pytest.raises(SystemExit, match="0"):
            antigravity_hook.main()

        captured = capsys.readouterr()
        assert captured.out.strip() == ""

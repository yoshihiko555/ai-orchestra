"""gemini-suggestions hook のユニットテスト。"""

from __future__ import annotations

import io
import json

import pytest

from tests.module_loader import load_module

gemini_hook = load_module(
    "suggest_gemini", "packages/gemini-suggestions/hooks/suggest-gemini-research.py"
)


class TestShouldSuggestGemini:
    """should_suggest_gemini のテスト。"""

    def test_simple_lookup_skipped(self):
        """SIMPLE_LOOKUP_PATTERNS はスキップ。"""
        assert gemini_hook.should_suggest_gemini("error message python")[0] is False
        assert gemini_hook.should_suggest_gemini("check version")[0] is False
        assert gemini_hook.should_suggest_gemini("release notes v2")[0] is False
        assert gemini_hook.should_suggest_gemini("changelog update")[0] is False

    def test_research_indicator_match(self):
        """RESEARCH_INDICATORS にマッチする場合は True。"""
        result, reason = gemini_hook.should_suggest_gemini("python documentation for asyncio")
        assert result is True
        assert "documentation" in reason

    def test_best_practice(self):
        """best practice キーワードで True。"""
        result, reason = gemini_hook.should_suggest_gemini(
            "react best practice for state management"
        )
        assert result is True

    def test_library_comparison(self):
        """library + comparison で True。"""
        result, reason = gemini_hook.should_suggest_gemini("comparison of library options")
        assert result is True

    def test_migration_guide(self):
        """migration キーワードで True。"""
        result, reason = gemini_hook.should_suggest_gemini("django migration guide v4 to v5")
        assert result is True

    def test_complex_query(self):
        """100 文字超のクエリは True。"""
        long_query = "How to implement a custom authentication middleware in FastAPI " * 3
        assert len(long_query) > 100
        result, reason = gemini_hook.should_suggest_gemini(long_query)
        assert result is True
        assert "Complex" in reason

    def test_url_indicator(self):
        """URL に indicator がある場合も True。"""
        result, reason = gemini_hook.should_suggest_gemini(
            "", url="https://docs.example.com/api-reference"
        )
        assert result is True

    def test_simple_query_no_indicator(self):
        """短いクエリで indicator なしは False。"""
        result, _ = gemini_hook.should_suggest_gemini("how to fix this")
        assert result is False

    def test_simple_lookup_in_url(self):
        """URL に simple lookup pattern がある場合はスキップ。"""
        result, _ = gemini_hook.should_suggest_gemini(
            "check", url="https://releases.example.com/changelog"
        )
        assert result is False

    def test_empty_query(self):
        """空クエリは False。"""
        result, _ = gemini_hook.should_suggest_gemini("")
        assert result is False


class TestBuildGeminiCommand:
    """_build_gemini_command のテスト。"""

    def test_with_model(self):
        """モデル指定付きコマンド。"""
        config = {"gemini": {"model": "gemini-2.5-pro"}}
        result = gemini_hook._build_gemini_command(config)
        assert "-m gemini-2.5-pro" in result
        assert "gemini" in result

    def test_no_model(self):
        """モデル未指定。"""
        result = gemini_hook._build_gemini_command({})
        assert "-m" not in result


class TestGeminiMain:
    """suggest-gemini-research main() のテスト。"""

    def test_gemini_disabled_exits(self, monkeypatch):
        """Gemini 無効時は exit(0)。"""
        monkeypatch.setattr(gemini_hook, "is_cli_enabled", lambda *a: False)
        monkeypatch.setattr(gemini_hook, "load_package_config", lambda *a: {})
        monkeypatch.setattr(
            "sys.stdin",
            io.StringIO(
                json.dumps(
                    {
                        "tool_name": "WebSearch",
                        "tool_input": {"query": "python documentation asyncio"},
                        "cwd": "/project",
                    }
                )
            ),
        )
        with pytest.raises(SystemExit, match="0"):
            gemini_hook.main()

    def test_websearch_suggestion(self, monkeypatch, capsys):
        """WebSearch で indicator マッチ時に提案出力。"""
        monkeypatch.setattr(gemini_hook, "is_cli_enabled", lambda *a: True)
        monkeypatch.setattr(gemini_hook, "load_package_config", lambda *a: {})
        monkeypatch.setattr(
            "sys.stdin",
            io.StringIO(
                json.dumps(
                    {
                        "tool_name": "WebSearch",
                        "tool_input": {"query": "python documentation asyncio"},
                        "cwd": "/project",
                    }
                )
            ),
        )
        with pytest.raises(SystemExit, match="0"):
            gemini_hook.main()

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert "Gemini Suggestion" in output["hookSpecificOutput"]["additionalContext"]

    def test_webfetch_with_url(self, monkeypatch, capsys):
        """WebFetch の URL で indicator マッチ時に提案出力。"""
        monkeypatch.setattr(gemini_hook, "is_cli_enabled", lambda *a: True)
        monkeypatch.setattr(gemini_hook, "load_package_config", lambda *a: {})
        monkeypatch.setattr(
            "sys.stdin",
            io.StringIO(
                json.dumps(
                    {
                        "tool_name": "WebFetch",
                        "tool_input": {
                            "url": "https://docs.example.com/api-reference",
                            "prompt": "get info",
                        },
                        "cwd": "/project",
                    }
                )
            ),
        )
        with pytest.raises(SystemExit, match="0"):
            gemini_hook.main()

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert "Gemini Suggestion" in output["hookSpecificOutput"]["additionalContext"]

    def test_no_suggestion(self, monkeypatch, capsys):
        """indicator なしの場合は出力なし。"""
        monkeypatch.setattr(gemini_hook, "is_cli_enabled", lambda *a: True)
        monkeypatch.setattr(gemini_hook, "load_package_config", lambda *a: {})
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
            gemini_hook.main()

        captured = capsys.readouterr()
        assert captured.out.strip() == ""

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

    def test_research_indicator_match(self):
        """RESEARCH_INDICATORS にマッチする場合は True。"""
        result, reason = antigravity_hook.should_suggest_antigravity(
            "python documentation for asyncio"
        )
        assert result is True
        assert "documentation" in reason

    def test_best_practice(self):
        """best practice キーワードで True。"""
        result, reason = antigravity_hook.should_suggest_antigravity(
            "react best practice for state management"
        )
        assert result is True

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

    def test_complex_query(self):
        """100 文字超のクエリは True。"""
        long_query = "How to implement a custom authentication middleware in FastAPI " * 3
        assert len(long_query) > 100
        result, reason = antigravity_hook.should_suggest_antigravity(long_query)
        assert result is True
        assert "Complex" in reason

    def test_url_indicator(self):
        """URL に indicator がある場合も True。"""
        result, reason = antigravity_hook.should_suggest_antigravity(
            "", url="https://docs.example.com/api-reference"
        )
        assert result is True

    def test_simple_query_no_indicator(self):
        """短いクエリで indicator なしは False。"""
        result, _ = antigravity_hook.should_suggest_antigravity("how to fix this")
        assert result is False

    def test_simple_lookup_in_url(self):
        """URL に simple lookup pattern がある場合はスキップ。

        注意: ホスト名に "example" を含めると RESEARCH_INDICATORS の
        "example" に意図せずマッチしてしまう（Fix 4 で research indicator を
        simple lookup より優先するよう変更したため）。この test の意図は
        純粋な simple lookup 抑制の確認なので、indicator と衝突しないホスト名
        （他テストと同様の "acme.test"）を使う。
        """
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
        monkeypatch.setattr(antigravity_hook, "load_package_config", lambda *a: {})
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
            antigravity_hook.main()

    def test_legacy_gemini_disabled_exits(self, monkeypatch, capsys):
        """旧 gemini.enabled: false（.local.yaml 残存）でも提案を抑制する。"""
        monkeypatch.setattr(
            antigravity_hook,
            "load_package_config",
            lambda *a: {"gemini": {"enabled": False}},
        )
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
            antigravity_hook.main()

        captured = capsys.readouterr()
        assert captured.out.strip() == ""

    def test_websearch_suggestion(self, monkeypatch, capsys):
        """WebSearch で indicator マッチ時に提案出力。"""
        monkeypatch.setattr(antigravity_hook, "is_cli_enabled", lambda *a: True)
        monkeypatch.setattr(antigravity_hook, "load_package_config", lambda *a: {})
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
            antigravity_hook.main()

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert "Antigravity Suggestion" in output["hookSpecificOutput"]["additionalContext"]
        assert "agy" in output["hookSpecificOutput"]["additionalContext"]

    def test_webfetch_with_url(self, monkeypatch, capsys):
        """WebFetch の URL で indicator マッチ時に提案出力。"""
        monkeypatch.setattr(antigravity_hook, "is_cli_enabled", lambda *a: True)
        monkeypatch.setattr(antigravity_hook, "load_package_config", lambda *a: {})
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
            antigravity_hook.main()

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert "Antigravity Suggestion" in output["hookSpecificOutput"]["additionalContext"]

    def test_no_suggestion(self, monkeypatch, capsys):
        """indicator なしの場合は出力なし。"""
        monkeypatch.setattr(antigravity_hook, "is_cli_enabled", lambda *a: True)
        monkeypatch.setattr(antigravity_hook, "load_package_config", lambda *a: {})
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

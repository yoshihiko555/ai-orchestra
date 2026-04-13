"""dashboard-html.py のユニットテスト。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.module_loader import load_module

# event_logger を先にロード（dashboard-html が import するため）
event_logger = load_module("event_logger", "packages/audit/hooks/event_logger.py")
dashboard_stats = load_module("dashboard_stats", "packages/audit/scripts/dashboard_stats.py")
# ファイル名はハイフン (dashboard-html.py) だが Python モジュール名にハイフンは
# 使えないため、load_module ではアンダースコアに読み替えて登録する。
dashboard_html = load_module("dashboard_html", "packages/audit/scripts/dashboard-html.py")

generate_html = dashboard_html.generate_html
_js_dumps = dashboard_html._js_dumps


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def sample_events() -> list[dict]:
    """テスト用イベント群を返す。"""
    return [
        {"type": "session_start", "sid": "s1", "data": {}},
        {"type": "session_end", "sid": "s1", "data": {}},
        {
            "type": "route_decision",
            "data": {"expected": "codex", "actual": "codex", "matched": True},
        },
        {
            "type": "cli_call",
            "data": {"tool": "codex", "success": True},
        },
        {
            "type": "subagent_start",
            "data": {"agent_type": "Explore"},
        },
        {"type": "subagent_end", "data": {"agent_type": "Explore"}},
        {"type": "quality_gate", "data": {"passed": True}},
    ]


# ---------------------------------------------------------------------------
# TestGenerateHtml
# ---------------------------------------------------------------------------
class TestGenerateHtml:
    """generate_html のテスト。"""

    def test_returns_valid_html(self, sample_events: list[dict]) -> None:
        result = generate_html(sample_events)
        assert "<!DOCTYPE html>" in result
        assert "</html>" in result

    def test_contains_title(self, sample_events: list[dict]) -> None:
        result = generate_html(sample_events, title="Test Dashboard")
        assert "Test Dashboard" in result

    def test_contains_chart_js_cdn(self, sample_events: list[dict]) -> None:
        result = generate_html(sample_events)
        assert "cdn.jsdelivr.net/npm/chart.js" in result

    def test_shows_session_filter(self, sample_events: list[dict]) -> None:
        result = generate_html(sample_events, session_id="test-session")
        assert "test-session" in result

    def test_shows_all_sessions_by_default(self, sample_events: list[dict]) -> None:
        result = generate_html(sample_events)
        assert "All Sessions" in result

    def test_contains_summary_cards(self, sample_events: list[dict]) -> None:
        result = generate_html(sample_events)
        assert "summary-card" in result
        assert "Sessions" in result
        assert "Total Events" in result

    def test_contains_chart_canvases(self, sample_events: list[dict]) -> None:
        result = generate_html(sample_events)
        assert 'id="routeChart"' in result
        assert 'id="cliChart"' in result
        assert 'id="distChart"' in result

    def test_empty_events_no_error(self) -> None:
        result = generate_html([])
        assert "<!DOCTYPE html>" in result
        assert "No data" in result

    def test_contains_event_count(self, sample_events: list[dict]) -> None:
        result = generate_html(sample_events)
        assert "7 events" in result


class TestJsDumps:
    """_js_dumps の XSS エスケープテスト。"""

    def test_escapes_closing_script_tag(self) -> None:
        result = _js_dumps(["</script>"])
        assert "<\\/script>" in result
        assert "</script>" not in result

    def test_normal_strings_unchanged(self) -> None:
        result = _js_dumps(["hello", "world"])
        assert result == '["hello", "world"]'

    def test_nested_html(self) -> None:
        result = _js_dumps({"key": "</style></script>"})
        assert "</script>" not in result
        assert "<\\/script>" in result


class TestGenerateHtmlGracefulDegradation:
    """ログ欠損時のグレースフル表示テスト。"""

    def test_only_session_events(self) -> None:
        events = [{"type": "session_start", "sid": "s1", "data": {}}]
        result = generate_html(events)
        assert "<!DOCTYPE html>" in result
        assert "No data" in result

    def test_missing_data_field(self) -> None:
        events = [{"type": "route_decision"}]
        result = generate_html(events)
        assert "<!DOCTYPE html>" in result

    def test_output_to_file(self, sample_events: list[dict], tmp_path: Path) -> None:
        out = tmp_path / "dashboard.html"
        content = generate_html(sample_events)
        out.write_text(content, encoding="utf-8")
        assert out.exists()
        assert out.stat().st_size > 0

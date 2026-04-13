"""dashboard_stats.py のユニットテスト。"""

from __future__ import annotations

import pytest

from tests.module_loader import load_module

# dashboard_stats は scripts/ にあり event_logger に依存しないため直接ロード
dashboard_stats = load_module("dashboard_stats", "packages/audit/scripts/dashboard_stats.py")

calc_session_stats = dashboard_stats.calc_session_stats
calc_route_stats = dashboard_stats.calc_route_stats
calc_cli_stats = dashboard_stats.calc_cli_stats
calc_subagent_stats = dashboard_stats.calc_subagent_stats
calc_quality_stats = dashboard_stats.calc_quality_stats
calc_event_distribution = dashboard_stats.calc_event_distribution


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def sample_events() -> list[dict]:
    """テスト用イベント群を返す。"""
    return [
        {"type": "session_start", "sid": "s1", "data": {}},
        {"type": "session_start", "sid": "s2", "data": {}},
        {"type": "session_end", "sid": "s1", "data": {}},
        {
            "type": "route_decision",
            "data": {"expected": "codex", "actual": "codex", "matched": True},
        },
        {
            "type": "route_decision",
            "data": {"expected": "codex", "actual": "gemini", "matched": False},
        },
        {
            "type": "route_decision",
            "data": {"expected": "gemini", "actual": "gemini", "matched": True},
        },
        {
            "type": "cli_call",
            "data": {"tool": "codex", "success": True},
        },
        {
            "type": "cli_call",
            "data": {"tool": "codex", "success": False, "error_type": "timeout"},
        },
        {
            "type": "cli_call",
            "data": {"tool": "gemini", "success": True},
        },
        {
            "type": "subagent_start",
            "data": {"agent_type": "Explore"},
        },
        {
            "type": "subagent_start",
            "data": {"agent_type": "Explore"},
        },
        {
            "type": "subagent_start",
            "data": {"agent_type": "backend-python-dev"},
        },
        {"type": "subagent_end", "data": {"agent_type": "Explore"}},
        {"type": "subagent_end", "data": {"agent_type": "Explore"}},
        {
            "type": "quality_gate",
            "data": {"passed": True},
        },
        {
            "type": "quality_gate",
            "data": {"passed": True},
        },
        {
            "type": "quality_gate",
            "data": {"passed": False},
        },
    ]


# ---------------------------------------------------------------------------
# TestCalcSessionStats
# ---------------------------------------------------------------------------
class TestCalcSessionStats:
    """calc_session_stats のテスト。"""

    def test_empty_events(self) -> None:
        result = calc_session_stats([])
        assert result["total_sessions"] == 0
        assert result["session_starts"] == 0
        assert result["session_ends"] == 0

    def test_counts_sessions(self, sample_events: list[dict]) -> None:
        result = calc_session_stats(sample_events)
        assert result["session_starts"] == 2
        assert result["session_ends"] == 1


# ---------------------------------------------------------------------------
# TestCalcRouteStats
# ---------------------------------------------------------------------------
class TestCalcRouteStats:
    """calc_route_stats のテスト。"""

    def test_empty_events(self) -> None:
        result = calc_route_stats([])
        assert result["total"] == 0
        assert result["match_rate"] == 0.0

    def test_accuracy(self, sample_events: list[dict]) -> None:
        result = calc_route_stats(sample_events)
        assert result["total"] == 3
        assert result["matched"] == 2
        assert result["mismatched"] == 1
        assert abs(result["match_rate"] - 66.7) < 0.1


# ---------------------------------------------------------------------------
# TestCalcCliStats
# ---------------------------------------------------------------------------
class TestCalcCliStats:
    """calc_cli_stats のテスト。"""

    def test_empty_events(self) -> None:
        result = calc_cli_stats([])
        assert result["total"] == 0
        assert result["success_rate"] == 0.0

    def test_counts_tools(self, sample_events: list[dict]) -> None:
        result = calc_cli_stats(sample_events)
        assert result["total"] == 3
        assert result["codex"] == 2
        assert result["gemini"] == 1
        assert result["success"] == 2


# ---------------------------------------------------------------------------
# TestCalcSubagentStats
# ---------------------------------------------------------------------------
class TestCalcSubagentStats:
    """calc_subagent_stats のテスト。"""

    def test_empty_events(self) -> None:
        result = calc_subagent_stats([])
        assert result["total_starts"] == 0

    def test_counts_by_type(self, sample_events: list[dict]) -> None:
        result = calc_subagent_stats(sample_events)
        assert result["total_starts"] == 3
        assert result["total_ends"] == 2
        assert result["by_agent_type"]["Explore"] == 2
        assert result["by_agent_type"]["backend-python-dev"] == 1


# ---------------------------------------------------------------------------
# TestCalcQualityStats
# ---------------------------------------------------------------------------
class TestCalcQualityStats:
    """calc_quality_stats のテスト。"""

    def test_empty_events(self) -> None:
        result = calc_quality_stats([])
        assert result["total"] == 0
        assert result["passed"] == 0
        assert result["failed"] == 0

    def test_counts_pass_fail(self, sample_events: list[dict]) -> None:
        result = calc_quality_stats(sample_events)
        assert result["total"] == 3
        assert result["passed"] == 2
        assert result["failed"] == 1


# ---------------------------------------------------------------------------
# TestCalcEventDistribution
# ---------------------------------------------------------------------------
class TestCalcEventDistribution:
    """calc_event_distribution のテスト。"""

    def test_empty_events(self) -> None:
        result = calc_event_distribution([])
        assert result == {}

    def test_distribution(self, sample_events: list[dict]) -> None:
        result = calc_event_distribution(sample_events)
        assert result["session_start"] == 2
        assert result["route_decision"] == 3
        assert result["cli_call"] == 3
        assert result["quality_gate"] == 3

    def test_none_and_empty_type_mapped_to_unknown(self) -> None:
        events = [
            {"type": None, "data": {}},
            {"type": "", "data": {}},
            {"data": {}},
        ]
        result = calc_event_distribution(events)
        assert result["unknown"] == 3

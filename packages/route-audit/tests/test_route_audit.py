"""route-audit 監査基盤のユニットテスト。"""

from __future__ import annotations

import os
import sys

from tests.module_loader import REPO_ROOT, load_module

os.environ["AI_ORCHESTRA_DIR"] = str(REPO_ROOT)
core_hooks = REPO_ROOT / "packages" / "core" / "hooks"
if str(core_hooks) not in sys.path:
    sys.path.insert(0, str(core_hooks))

orchestration_route_audit = load_module(
    "orchestration_route_audit",
    "packages/route-audit/hooks/orchestration-route-audit.py",
)
is_match = orchestration_route_audit.is_match
merged_aliases = orchestration_route_audit.merged_aliases

from route_config import build_aliases  # noqa: E402

orchestration_kpi_report = load_module(
    "orchestration_kpi_report",
    "packages/route-audit/scripts/orchestration-kpi-report.py",
)
build_scorecard = orchestration_kpi_report.build_scorecard

dashboard = load_module("dashboard", "packages/route-audit/scripts/dashboard.py")
calc_route_stats = dashboard.calc_route_stats


# ========== is_match テスト ==========


class TestIsMatch:
    """is_match() のテスト。"""

    def test_exact_match(self) -> None:
        policy: dict = {"aliases": {}}
        assert is_match("codex", "codex", policy) is True

    def test_alias_match(self) -> None:
        policy = {"aliases": {"codex": ["bash:codex"]}}
        assert is_match("codex", "bash:codex", policy) is True

    def test_no_match(self) -> None:
        policy = {"aliases": {"codex": ["bash:codex"]}}
        assert is_match("codex", "bash:gemini", policy) is False

    def test_empty_expected(self) -> None:
        policy: dict = {"aliases": {}}
        assert is_match("", "bash:codex", policy) is False

    def test_empty_actual(self) -> None:
        policy: dict = {"aliases": {}}
        assert is_match("codex", "", policy) is False

    def test_no_wildcard_match(self) -> None:
        """明示されていない aliases はマッチしない。"""
        policy = {
            "aliases": {
                "claude-direct": ["task:architect"],
            }
        }
        assert is_match("claude-direct", "task:Explore", policy) is False
        assert is_match("claude-direct", "task:Plan", policy) is False

    def test_dynamic_aliases_match(self) -> None:
        """動的 aliases（config 由来）でのマッチを確認。"""
        config = {"agents": {"architect": {"tool": "claude-direct"}}}
        aliases = build_aliases(config)
        policy = {"aliases": aliases}
        assert is_match("claude-direct", "task:architect", policy) is True

    def test_merged_aliases(self) -> None:
        """動的 + 静的 aliases のマージを確認。"""
        config = {"agents": {"architect": {"tool": "claude-direct"}}}
        policy = {"aliases": {"claude-direct": ["skill:commit"]}}
        merged = merged_aliases(config, policy)
        assert "task:architect" in merged["claude-direct"]
        assert "skill:commit" in merged["claude-direct"]

    def test_merged_aliases_no_duplicates(self) -> None:
        """マージ時に重複エントリが発生しない。"""
        config = {"agents": {"debugger": {"tool": "codex"}}}
        policy = {"aliases": {"codex": ["bash:codex"]}}
        merged = merged_aliases(config, policy)
        assert merged["codex"].count("bash:codex") == 1

    def test_claude_direct_aliases(self) -> None:
        """claude-direct の aliases マッチを確認。"""
        policy = {"aliases": {"claude-direct": ["skill:commit", "skill:memory-tidy"]}}
        assert is_match("claude-direct", "skill:commit", policy) is True
        assert is_match("claude-direct", "skill:memory-tidy", policy) is True
        assert is_match("claude-direct", "skill:unknown", policy) is False


# ========== build_scorecard テスト ==========


class TestBuildScorecard:
    """build_scorecard() のテスト。"""

    def test_helper_excluded_from_match_rate(self) -> None:
        """is_helper: true のレコードは一致率の判定から除外される。"""
        route_rows = [
            {
                "prompt_id": "p1",
                "timestamp": "2026-01-01T00:00:00",
                "actual_route": "task:Explore",
                "expected_route": "codex",
                "matched": False,
                "is_helper": True,
            },
            {
                "prompt_id": "p1",
                "timestamp": "2026-01-01T00:00:01",
                "actual_route": "bash:codex",
                "expected_route": "codex",
                "matched": True,
                "is_helper": False,
            },
            {
                "prompt_id": "p2",
                "timestamp": "2026-01-01T00:00:02",
                "actual_route": "task:Explore",
                "expected_route": "gemini",
                "matched": False,
                "is_helper": True,
            },
        ]
        card = build_scorecard(route_rows, [])

        assert card["summary"]["observed_prompts"] == 2
        assert card["summary"]["effective_prompts"] == 1
        assert card["summary"]["helper_only_prompts"] == 1
        assert card["metrics"]["expected_route_match_rate"] == 100.0

    def test_helper_only_prompt_excluded_from_denominator(self) -> None:
        """ヘルパーのみのプロンプトは分母から除外。"""
        route_rows = [
            {
                "prompt_id": "p1",
                "timestamp": "2026-01-01T00:00:00",
                "actual_route": "task:Plan",
                "expected_route": "codex",
                "matched": False,
                "is_helper": True,
            },
            {
                "prompt_id": "p2",
                "timestamp": "2026-01-01T00:00:01",
                "actual_route": "task:Plan",
                "expected_route": "gemini",
                "matched": False,
                "is_helper": True,
            },
        ]
        card = build_scorecard(route_rows, [])

        assert card["summary"]["observed_prompts"] == 2
        assert card["summary"]["effective_prompts"] == 0
        assert card["summary"]["helper_only_prompts"] == 2
        assert card["metrics"]["expected_route_match_rate"] == 0.0

    def test_no_helper_records(self) -> None:
        """is_helper フィールドが無い（旧ログ）場合は全て実効扱い。"""
        route_rows = [
            {
                "prompt_id": "p1",
                "timestamp": "2026-01-01T00:00:00",
                "actual_route": "bash:codex",
                "expected_route": "codex",
                "matched": True,
            },
            {
                "prompt_id": "p2",
                "timestamp": "2026-01-01T00:00:01",
                "actual_route": "bash:gemini",
                "expected_route": "codex",
                "matched": False,
            },
        ]
        card = build_scorecard(route_rows, [])

        assert card["summary"]["observed_prompts"] == 2
        assert card["summary"]["effective_prompts"] == 2
        assert card["summary"]["helper_only_prompts"] == 0
        assert card["metrics"]["expected_route_match_rate"] == 50.0

    def test_effective_prompts_count(self) -> None:
        """effective_prompts = observed_prompts - helper_only_prompts。"""
        route_rows = [
            {
                "prompt_id": "p1",
                "timestamp": "2026-01-01T00:00:00",
                "actual_route": "task:Explore",
                "expected_route": "codex",
                "matched": False,
                "is_helper": True,
            },
            {
                "prompt_id": "p1",
                "timestamp": "2026-01-01T00:00:01",
                "actual_route": "bash:codex",
                "expected_route": "codex",
                "matched": True,
                "is_helper": False,
            },
            {
                "prompt_id": "p2",
                "timestamp": "2026-01-01T00:00:02",
                "actual_route": "task:Explore",
                "expected_route": "gemini",
                "matched": False,
                "is_helper": True,
            },
            {
                "prompt_id": "p3",
                "timestamp": "2026-01-01T00:00:03",
                "actual_route": "bash:gemini",
                "expected_route": "gemini",
                "matched": True,
                "is_helper": False,
            },
        ]
        card = build_scorecard(route_rows, [])

        assert card["summary"]["observed_prompts"] == 3
        assert card["summary"]["effective_prompts"] == 2
        assert card["summary"]["helper_only_prompts"] == 1


# ========== calc_route_stats テスト ==========


class TestCalcRouteStats:
    """dashboard.py の calc_route_stats() テスト。"""

    def test_helpers_excluded(self) -> None:
        """is_helper: true のイベントは集計から除外。"""
        events = [
            {
                "event_type": "route_audit",
                "data": {"matched": False, "is_helper": True},
            },
            {
                "event_type": "route_audit",
                "data": {"matched": True, "is_helper": False},
            },
            {
                "event_type": "route_audit",
                "data": {"matched": False, "is_helper": False},
            },
        ]
        result = calc_route_stats(events)

        assert result["total"] == 2
        assert result["matched"] == 1
        assert result["helpers_excluded"] == 1
        assert result["rate"] == 50.0

    def test_no_helpers(self) -> None:
        """ヘルパーなしの場合は全件集計。"""
        events = [
            {"event_type": "route_audit", "data": {"matched": True}},
            {"event_type": "route_audit", "data": {"matched": True}},
        ]
        result = calc_route_stats(events)

        assert result["total"] == 2
        assert result["matched"] == 2
        assert result["helpers_excluded"] == 0
        assert result["rate"] == 100.0

    def test_all_helpers(self) -> None:
        """全てヘルパーの場合、total=0 で rate=0。"""
        events = [
            {
                "event_type": "route_audit",
                "data": {"matched": False, "is_helper": True},
            },
        ]
        result = calc_route_stats(events)

        assert result["total"] == 0
        assert result["helpers_excluded"] == 1
        assert result["rate"] == 0.0

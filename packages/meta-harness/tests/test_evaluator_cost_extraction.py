"""cost 抽出のテスト（Sec2-2, Sec14-1 注意点1・2）。

budget 打ち切り時に result イベントのトップレベル usage.* が 0 化し、
modelUsage.<model>.inputTokens/outputTokens へフォールバックする挙動を検証する。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.module_loader import load_module

ev = load_module(
    "meta_harness_evaluator_cost_extraction",
    "packages/meta-harness/lib/evaluator.py",
)
mh = load_module(
    "meta_harness_common_cost_extraction",
    "packages/meta-harness/lib/meta_harness_common.py",
)


def _write_jsonl(path: Path, events: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")


class TestExtractCostNormal:
    def test_extracts_all_fields_from_result_event(self, tmp_path: Path) -> None:
        events_path = tmp_path / "events.jsonl"
        _write_jsonl(
            events_path,
            [
                {"type": "system", "subtype": "init"},
                {
                    "type": "result",
                    "subtype": "success",
                    "total_cost_usd": 1.2,
                    "usage": {"input_tokens": 10073, "output_tokens": 582},
                    "duration_ms": 74400,
                    "num_turns": 2,
                },
            ],
        )
        cost = ev.extract_cost(events_path)
        assert cost["input_tokens"] == 10073
        assert cost["output_tokens"] == 582
        assert cost["total_tokens"] == 10073 + 582
        assert cost["total_cost_usd"] == 1.2
        assert cost["duration_ms"] == 74400
        assert cost["num_turns"] == 2

    def test_extracts_cache_tokens_and_tags_cli_source(self, tmp_path: Path) -> None:
        """Issue #378: cache_creation/read_input_tokens は usage から抽出し、
        cache_neutral_source は "cli" にタグ付けされる（cache_neutral_cost_usd 自体は
        _apply_cache_neutral_cost() の責務なのでここでは抽出しない）。"""
        events_path = tmp_path / "events.jsonl"
        _write_jsonl(
            events_path,
            [
                {
                    "type": "result",
                    "subtype": "success",
                    "total_cost_usd": 1.2,
                    "usage": {
                        "input_tokens": 2482,
                        "output_tokens": 1465,
                        "cache_creation_input_tokens": 60011,
                        "cache_read_input_tokens": 292951,
                    },
                },
            ],
        )
        cost = ev.extract_cost(events_path)
        assert cost["cache_creation_input_tokens"] == 60011
        assert cost["cache_read_input_tokens"] == 292951
        assert cost["cache_neutral_source"] == "cli"
        assert "cache_neutral_cost_usd" not in cost

    def test_ignores_non_result_events_and_takes_last_result(self, tmp_path: Path) -> None:
        events_path = tmp_path / "events.jsonl"
        _write_jsonl(
            events_path,
            [
                {"type": "result", "subtype": "success", "total_cost_usd": 0.1, "usage": {}},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
                {
                    "type": "result",
                    "subtype": "success",
                    "total_cost_usd": 2.5,
                    "usage": {"input_tokens": 5, "output_tokens": 5},
                },
            ],
        )
        cost = ev.extract_cost(events_path)
        assert cost["total_cost_usd"] == 2.5


class TestExtractCostBudgetExceededFallback:
    def test_falls_back_to_model_usage_when_top_level_usage_is_zero(self, tmp_path: Path) -> None:
        events_path = tmp_path / "events.jsonl"
        _write_jsonl(
            events_path,
            [
                {
                    "type": "result",
                    "subtype": "error_max_budget_usd",
                    "total_cost_usd": 0.74,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                    "modelUsage": {
                        "claude-sonnet": {"inputTokens": 49699, "outputTokens": 1200},
                        "claude-haiku": {"inputTokens": 100, "outputTokens": 20},
                    },
                }
            ],
        )
        cost = ev.extract_cost(events_path)
        assert cost["input_tokens"] == 49699 + 100
        assert cost["output_tokens"] == 1200 + 20
        assert cost["total_tokens"] == 49699 + 100 + 1200 + 20

    def test_falls_back_cache_tokens_and_tags_model_usage_source(self, tmp_path: Path) -> None:
        events_path = tmp_path / "events.jsonl"
        _write_jsonl(
            events_path,
            [
                {
                    "type": "result",
                    "subtype": "error_max_budget_usd",
                    "total_cost_usd": 0.74,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                    "modelUsage": {
                        "claude-sonnet": {
                            "inputTokens": 49699,
                            "outputTokens": 1200,
                            "cacheCreationInputTokens": 4000,
                            "cacheReadInputTokens": 8000,
                        },
                        "claude-haiku": {
                            "inputTokens": 100,
                            "outputTokens": 20,
                            "cacheCreationInputTokens": 10,
                            "cacheReadInputTokens": 20,
                        },
                    },
                }
            ],
        )
        cost = ev.extract_cost(events_path)
        assert cost["cache_creation_input_tokens"] == 4000 + 10
        assert cost["cache_read_input_tokens"] == 8000 + 20
        assert cost["cache_neutral_source"] == "model_usage"

    def test_does_not_fall_back_when_subtype_is_success_even_if_usage_zero(
        self, tmp_path: Path
    ) -> None:
        events_path = tmp_path / "events.jsonl"
        _write_jsonl(
            events_path,
            [
                {
                    "type": "result",
                    "subtype": "success",
                    "total_cost_usd": 0.0,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                    "modelUsage": {"claude-sonnet": {"inputTokens": 999, "outputTokens": 999}},
                }
            ],
        )
        cost = ev.extract_cost(events_path)
        assert cost["input_tokens"] == 0
        assert cost["output_tokens"] == 0

    def test_has_budget_exceeded_detects_subtype(self, tmp_path: Path) -> None:
        events_path = tmp_path / "events.jsonl"
        _write_jsonl(events_path, [{"type": "result", "subtype": "error_max_budget_usd"}])
        assert ev._has_budget_exceeded(events_path) is True

    def test_has_budget_exceeded_false_for_success(self, tmp_path: Path) -> None:
        events_path = tmp_path / "events.jsonl"
        _write_jsonl(events_path, [{"type": "result", "subtype": "success"}])
        assert ev._has_budget_exceeded(events_path) is False


class TestExtractCostMissingOrEmpty:
    def test_missing_events_file_returns_zero_cost(self, tmp_path: Path) -> None:
        cost = ev.extract_cost(tmp_path / "does-not-exist.jsonl")
        assert cost == ev.ZERO_COST

    def test_empty_events_file_returns_zero_cost(self, tmp_path: Path) -> None:
        events_path = tmp_path / "events.jsonl"
        events_path.write_text("", encoding="utf-8")
        cost = ev.extract_cost(events_path)
        assert cost == ev.ZERO_COST

    def test_has_budget_exceeded_false_for_missing_file(self, tmp_path: Path) -> None:
        assert ev._has_budget_exceeded(tmp_path / "does-not-exist.jsonl") is False

    def test_malformed_lines_are_skipped(self, tmp_path: Path) -> None:
        events_path = tmp_path / "events.jsonl"
        events_path.write_text(
            "not json at all\n"
            + json.dumps({"type": "result", "subtype": "success", "total_cost_usd": 0.5})
            + "\n",
            encoding="utf-8",
        )
        cost = ev.extract_cost(events_path)
        assert cost["total_cost_usd"] == 0.5


class TestCountToolUses:
    def test_counts_tool_use_content_items_across_assistant_events(self, tmp_path: Path) -> None:
        events_path = tmp_path / "events.jsonl"
        _write_jsonl(
            events_path,
            [
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "..."},
                            {"type": "tool_use", "name": "Read"},
                        ]
                    },
                },
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "tool_use", "name": "Write"}]},
                },
                {"type": "user", "message": {"content": []}},
            ],
        )
        assert ev._count_tool_uses(events_path) == 2

    def test_zero_when_no_tool_use_items(self, tmp_path: Path) -> None:
        events_path = tmp_path / "events.jsonl"
        _write_jsonl(
            events_path,
            [{"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}],
        )
        assert ev._count_tool_uses(events_path) == 0


def _config_with_pricing(input_price: float, output_price: float) -> dict:
    return {
        "evaluate": {
            "isolation": {
                "broker": {
                    "pricing_upper_bound_usd_per_million": {
                        "input": input_price,
                        "output": output_price,
                    }
                }
            }
        }
    }


class TestApplyCacheNeutralCost:
    """`_apply_cache_neutral_cost()` の usage ソース優先順位（Issue #378、決定2）を検証する。"""

    def test_computes_from_cli_sourced_cost(self) -> None:
        cost = {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_creation_input_tokens": 200,
            "cache_read_input_tokens": 700,
            "cache_neutral_source": "cli",
        }
        result = ev._apply_cache_neutral_cost(cost, None, _config_with_pricing(2.0, 10.0))
        # (100+200+700)*2/1e6 + 50*10/1e6 = 0.002 + 0.0005
        assert result["cache_neutral_cost_usd"] == pytest.approx(0.0025)
        assert result["cache_neutral_source"] == "cli"
        assert result["cache_creation_input_tokens"] == 200
        assert result["cache_read_input_tokens"] == 700

    def test_preserves_model_usage_source_tag(self) -> None:
        cost = {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_creation_input_tokens": 1,
            "cache_read_input_tokens": 1,
            "cache_neutral_source": "model_usage",
        }
        result = ev._apply_cache_neutral_cost(cost, None, _config_with_pricing(3.0, 15.0))
        assert result["cache_neutral_source"] == "model_usage"

    def test_falls_back_to_broker_metrics_usage_when_all_tokens_zero(self) -> None:
        cost = dict(ev.ZERO_COST)
        isolation_metadata = {
            "broker": {
                "metrics": {
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "cache_creation_input_tokens": 50,
                        "cache_read_input_tokens": 30,
                    }
                }
            }
        }
        result = ev._apply_cache_neutral_cost(
            cost, isolation_metadata, _config_with_pricing(2.0, 10.0)
        )
        assert result["cache_neutral_source"] == "broker_metrics"
        assert result["cache_creation_input_tokens"] == 50
        assert result["cache_read_input_tokens"] == 30
        # (100+50+30)*2/1e6 + 20*10/1e6 = 0.00036 + 0.0002
        assert result["cache_neutral_cost_usd"] == pytest.approx(0.00056)

    def test_does_not_use_broker_metrics_when_cli_data_present(self) -> None:
        """CLI/modelUsage が既にデータを持つ場合、broker metrics（judge コスト混入
        スコープ）へは絶対にフォールバックしない（決定2の core assertion）。"""
        cost = {
            "input_tokens": 1,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_neutral_source": "cli",
        }
        isolation_metadata = {
            "broker": {
                "metrics": {
                    "usage": {
                        "input_tokens": 999999,
                        "output_tokens": 999999,
                        "cache_creation_input_tokens": 999999,
                        "cache_read_input_tokens": 999999,
                    }
                }
            }
        }
        result = ev._apply_cache_neutral_cost(
            cost, isolation_metadata, _config_with_pricing(2.0, 10.0)
        )
        assert result["cache_neutral_source"] == "cli"
        assert result["cache_neutral_cost_usd"] == pytest.approx(1 * 2.0 / 1_000_000)

    def test_zero_when_no_source_available(self) -> None:
        cost = dict(ev.ZERO_COST)
        result = ev._apply_cache_neutral_cost(cost, None, _config_with_pricing(2.0, 10.0))
        assert result["cache_neutral_cost_usd"] == 0.0
        assert result["cache_neutral_source"] == "cli"

    def test_skips_broker_metrics_when_anomaly_flagged(self) -> None:
        """anomaly マーカー（_mark_isolation_metrics_stale 経由）が立った broker metrics
        は total_cost_usd 側と同様に信用しない（不在扱い）。"""
        cost = dict(ev.ZERO_COST)
        isolation_metadata = {
            "broker": {
                "metrics": {
                    "anomaly": True,
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    },
                }
            }
        }
        result = ev._apply_cache_neutral_cost(
            cost, isolation_metadata, _config_with_pricing(2.0, 10.0)
        )
        assert result["cache_neutral_source"] == "cli"
        assert result["cache_neutral_cost_usd"] == 0.0

    def test_skips_broker_metrics_when_budget_exceeded_flagged(self) -> None:
        cost = dict(ev.ZERO_COST)
        isolation_metadata = {
            "broker": {
                "metrics": {
                    "budget_exceeded": True,
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    },
                }
            }
        }
        result = ev._apply_cache_neutral_cost(
            cost, isolation_metadata, _config_with_pricing(2.0, 10.0)
        )
        assert result["cache_neutral_source"] == "cli"
        assert result["cache_neutral_cost_usd"] == 0.0

    def test_falls_back_to_defaults_pricing_when_config_missing_key(self) -> None:
        cost = {
            "input_tokens": 1_000_000,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_neutral_source": "cli",
        }
        result = ev._apply_cache_neutral_cost(cost, None, {})
        default_input_price = mh.DEFAULTS["evaluate"]["isolation"]["broker"][
            "pricing_upper_bound_usd_per_million"
        ]["input"]
        assert result["cache_neutral_cost_usd"] == pytest.approx(default_input_price)

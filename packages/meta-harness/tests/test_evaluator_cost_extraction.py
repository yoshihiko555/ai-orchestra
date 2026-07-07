"""cost 抽出のテスト（Sec2-2, Sec14-1 注意点1・2）。

budget 打ち切り時に result イベントのトップレベル usage.* が 0 化し、
modelUsage.<model>.inputTokens/outputTokens へフォールバックする挙動を検証する。
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.module_loader import load_module

ev = load_module(
    "meta_harness_evaluator_cost_extraction",
    "packages/meta-harness/lib/evaluator.py",
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

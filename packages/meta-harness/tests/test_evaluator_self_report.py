"""self-report パース + ペナルティのテスト（EV-23, Sec3-1）。

skill-evolution の `[skill-self-report]` パーサロジックを流用した挙動を検証する。
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.module_loader import load_module

ev = load_module(
    "meta_harness_evaluator_self_report",
    "packages/meta-harness/lib/evaluator.py",
)
mh = load_module(
    "meta_harness_common_self_report",
    "packages/meta-harness/lib/meta_harness_common.py",
)


def _write_assistant_text(path: Path, text: str) -> None:
    event = {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
    path.write_text(json.dumps(event) + "\n", encoding="utf-8")


class TestParseSelfReportFound:
    def test_parses_valid_block(self, tmp_path: Path) -> None:
        events_path = tmp_path / "events.jsonl"
        _write_assistant_text(
            events_path,
            'done.\n[skill-self-report]{"ambiguities": 1, "discretion_fills": 2, '
            '"retries": 0}[/skill-self-report]',
        )
        report = ev.parse_self_report(events_path)
        assert report == {"ambiguities": 1, "discretion_fills": 2, "retries": 0}

    def test_takes_last_block_when_multiple_present(self, tmp_path: Path) -> None:
        events_path = tmp_path / "events.jsonl"
        text = (
            '[skill-self-report]{"ambiguities": 1, "discretion_fills": 0, "retries": 0}'
            "[/skill-self-report]\n"
            'later: [skill-self-report]{"ambiguities": 9, "discretion_fills": 0, "retries": 0}'
            "[/skill-self-report]"
        )
        _write_assistant_text(events_path, text)
        report = ev.parse_self_report(events_path)
        assert report["ambiguities"] == 9


class TestParseSelfReportMissingOrUnparsable:
    def test_missing_block_returns_none(self, tmp_path: Path) -> None:
        events_path = tmp_path / "events.jsonl"
        _write_assistant_text(events_path, "task complete, no self report here")
        assert ev.parse_self_report(events_path) is None

    def test_unparsable_json_returns_none(self, tmp_path: Path) -> None:
        events_path = tmp_path / "events.jsonl"
        _write_assistant_text(events_path, "[skill-self-report]not valid json[/skill-self-report]")
        assert ev.parse_self_report(events_path) is None

    def test_non_object_json_returns_none(self, tmp_path: Path) -> None:
        events_path = tmp_path / "events.jsonl"
        _write_assistant_text(events_path, "[skill-self-report][1, 2, 3][/skill-self-report]")
        assert ev.parse_self_report(events_path) is None

    def test_missing_events_file_returns_none(self, tmp_path: Path) -> None:
        assert ev.parse_self_report(tmp_path / "does-not-exist.jsonl") is None


class TestComputeSelfReportAndPenalty:
    _CONFIG = {"scoring": {"penalty_missing_report": 6}}

    def test_missing_report_applies_penalty_missing_report(self, tmp_path: Path) -> None:
        events_path = tmp_path / "events.jsonl"
        _write_assistant_text(events_path, "no report")
        report, penalty = ev.compute_self_report_and_penalty(events_path, self._CONFIG)
        assert report is None
        assert penalty == 6.0

    def test_present_report_sums_three_fields_as_penalty(self, tmp_path: Path) -> None:
        events_path = tmp_path / "events.jsonl"
        _write_assistant_text(
            events_path,
            '[skill-self-report]{"ambiguities": 1, "discretion_fills": 1, "retries": 1}'
            "[/skill-self-report]",
        )
        report, penalty = ev.compute_self_report_and_penalty(events_path, self._CONFIG)
        assert report == {"ambiguities": 1, "discretion_fills": 1, "retries": 1}
        assert penalty == 3.0

    def test_non_numeric_fields_are_coerced_safely(self, tmp_path: Path) -> None:
        events_path = tmp_path / "events.jsonl"
        _write_assistant_text(
            events_path,
            '[skill-self-report]{"ambiguities": "not-a-number", "discretion_fills": 2, '
            '"retries": 0}[/skill-self-report]',
        )
        report, penalty = ev.compute_self_report_and_penalty(events_path, self._CONFIG)
        assert report["ambiguities"] == 0  # 安全側にフォールバック
        assert penalty == 2.0


class TestQualityScoreMissingReportPenaltyZeroesOutTerm:
    """EV-23: penalty_missing_report（既定6）は 30 - 6*5 = 0 となり、寄与を完全にゼロにする。"""

    def test_default_penalty_missing_report_zeroes_quality_term(self) -> None:
        config = {
            "scoring": {
                "critical_weight": 70,
                "penalty_base": 30,
                "penalty_per_item": 5,
                "penalty_missing_report": 6,
            }
        }
        score_with_full_critical = mh.quality_score(1.0, 6.0, config)
        assert score_with_full_critical == 70.0  # 70 + max(0, 30 - 30) = 70

    def test_honest_report_with_zero_penalty_scores_higher_than_missing(self) -> None:
        config = {
            "scoring": {
                "critical_weight": 70,
                "penalty_base": 30,
                "penalty_per_item": 5,
                "penalty_missing_report": 6,
            }
        }
        honest_zero_penalty = mh.quality_score(1.0, 0.0, config)
        missing_report_penalty = mh.quality_score(1.0, 6.0, config)
        assert honest_zero_penalty > missing_report_penalty

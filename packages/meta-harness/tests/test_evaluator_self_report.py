"""self-report パース + ペナルティのテスト（EV-23, Sec3-1）。

skill-evolution の `[skill-self-report]` パーサロジックを流用した挙動を検証する。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

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


def _write_assistant_turns(path: Path, texts: list[str]) -> None:
    """複数の assistant turn（events.jsonl の複数イベント）を順番に書き出す。"""
    events = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
        for text in texts
    ]
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")


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


class TestWriteCandidateFinalReportArtifact:
    """Issue #297 / PR #326 レビュー2巡目指摘: critical/checks オラクルが候補の最終応答テキストを
    worktree_dir 経由で参照できるようにするブリッジ（`CANDIDATE_FINAL_REPORT_RELATIVE_PATH`）。"""

    def test_writes_extracted_assistant_text_to_relative_path(self, tmp_path: Path) -> None:
        worktree_dir = tmp_path / "worktree"
        worktree_dir.mkdir()
        events_path = tmp_path / "events.jsonl"
        _write_assistant_text(events_path, "AC はまだ合意されていません。定義しますか?")

        ev._write_candidate_final_report_artifact(worktree_dir, events_path)

        destination = worktree_dir / ev.CANDIDATE_FINAL_REPORT_RELATIVE_PATH
        assert destination.is_file()
        assert "AC はまだ合意されていません" in destination.read_text(encoding="utf-8")

    def test_redacts_secrets_before_writing(self, tmp_path: Path) -> None:
        worktree_dir = tmp_path / "worktree"
        worktree_dir.mkdir()
        events_path = tmp_path / "events.jsonl"
        secret = "sk-" + "a" * 40
        _write_assistant_text(events_path, f"token: {secret}")

        ev._write_candidate_final_report_artifact(worktree_dir, events_path)

        destination = worktree_dir / ev.CANDIDATE_FINAL_REPORT_RELATIVE_PATH
        assert secret not in destination.read_text(encoding="utf-8")

    def test_missing_events_file_is_a_silent_no_op(self, tmp_path: Path) -> None:
        worktree_dir = tmp_path / "worktree"
        worktree_dir.mkdir()

        ev._write_candidate_final_report_artifact(worktree_dir, tmp_path / "does-not-exist.jsonl")

        assert not (worktree_dir / ev.CANDIDATE_FINAL_REPORT_RELATIVE_PATH).exists()

    def test_only_last_assistant_turn_is_written_when_multiple_turns_present(
        self, tmp_path: Path
    ) -> None:
        """PR #326 レビュー2巡目指摘 (Codex P1): `events.jsonl` に複数の assistant turn がある
        場合、全 turn を連結した `_extract_assistant_text` ではなく最後の turn だけを書き出す
        必要がある。中間ターンで AC 確認に触れても最終応答で撤回した候補は、最終応答のみを見た
        場合に不合格となるべきであり、この artifact にも最終応答だけが残っていなければならない。
        """
        worktree_dir = tmp_path / "worktree"
        worktree_dir.mkdir()
        events_path = tmp_path / "events.jsonl"
        _write_assistant_turns(
            events_path,
            [
                "AC はまだ合意されていません。定義しますか?",
                "作業が完了しました。Phase 3 を追加しました。",
            ],
        )

        ev._write_candidate_final_report_artifact(worktree_dir, events_path)

        destination = worktree_dir / ev.CANDIDATE_FINAL_REPORT_RELATIVE_PATH
        content = destination.read_text(encoding="utf-8")
        assert "作業が完了しました" in content
        assert "AC はまだ合意されていません" not in content

    def test_oracle_dir_symlinked_outside_worktree_is_rejected_and_target_untouched(
        self, tmp_path: Path
    ) -> None:
        """CodeRabbit レビュー指摘 (High): 候補が `.claude/meta-harness-oracle` を worktree 外
        への symlink に差し替えても、評価プロセス権限で外部ターゲットへ書き込んではならない
        （fail-open: 書込みスキップのみで例外は外へ伝播しない）。"""
        worktree_dir = tmp_path / "worktree"
        worktree_dir.mkdir()
        (worktree_dir / ".claude").mkdir()
        external_target = tmp_path / "external"
        external_target.mkdir()
        (worktree_dir / ".claude" / "meta-harness-oracle").symlink_to(
            external_target, target_is_directory=True
        )

        events_path = tmp_path / "events.jsonl"
        _write_assistant_text(events_path, "AC はまだ合意されていません。定義しますか?")

        ev._write_candidate_final_report_artifact(worktree_dir, events_path)

        assert list(external_target.iterdir()) == []

    def test_trailing_malformed_line_skips_write_and_does_not_use_stale_response(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """CodeRabbit レビュー指摘 (High): `_iter_jsonl` は `JSONDecodeError` の行を黙って破棄
        するため、`events.jsonl` の末尾が途中書き込みで壊れていると、直前の（古い）assistant
        turn が「最終応答」として拾われてしまう。末尾の非空行が壊れている場合は final-report を
        書かず、fail-open で警告ログのみ残す。"""
        worktree_dir = tmp_path / "worktree"
        worktree_dir.mkdir()
        events_path = tmp_path / "events.jsonl"
        stale_event = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "AC はまだ合意されていません。定義しますか?"}
                    ]
                },
            }
        )
        events_path.write_text(
            stale_event + "\n" + '{"type": "assistant", "message": {"content": [{"type": "text"',
            encoding="utf-8",
        )

        with caplog.at_level(logging.WARNING):
            ev._write_candidate_final_report_artifact(worktree_dir, events_path)

        assert not (worktree_dir / ev.CANDIDATE_FINAL_REPORT_RELATIVE_PATH).exists()
        assert any("fail-open" in record.message for record in caplog.records)

    def test_final_report_symlinked_to_existing_file_is_rejected_and_target_untouched(
        self, tmp_path: Path
    ) -> None:
        """`final-report.md` 自体が（worktree 内外いずれかの）既存ファイルへの symlink に
        差し替えられていても、そのファイルへ透過的に書き込んではならない。"""
        worktree_dir = tmp_path / "worktree"
        worktree_dir.mkdir()
        oracle_dir = worktree_dir / ".claude" / "meta-harness-oracle"
        oracle_dir.mkdir(parents=True)
        victim = worktree_dir / "victim.md"
        victim.write_text("do not touch", encoding="utf-8")
        (oracle_dir / "final-report.md").symlink_to(victim)

        events_path = tmp_path / "events.jsonl"
        _write_assistant_text(events_path, "AC はまだ合意されていません。定義しますか?")

        ev._write_candidate_final_report_artifact(worktree_dir, events_path)

        assert victim.read_text(encoding="utf-8") == "do not touch"

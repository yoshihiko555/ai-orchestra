"""graded checks の意味論テスト（ADR-20260814-050 決定2・決定6）。

- graded 宣言シナリオ: quality = graded_pass_rate * 100（self_report penalty は非採点化、
  記録のみ）。verdict 判定機構（`_determine_verdict`）は critical のみを見るため graded の
  fail で verdict が変わらないこと。
- graded 未宣言シナリオ: 従来式（`quality_score(critical_pass_rate, penalty, config)`）の
  ままであること（後方互換）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.module_loader import load_module

ev = load_module(
    "meta_harness_evaluator_graded_scoring",
    "packages/meta-harness/lib/evaluator.py",
)
mh = load_module(
    "meta_harness_common_graded_scoring",
    "packages/meta-harness/lib/meta_harness_common.py",
)

_SCHEMA_DIR = Path("packages/meta-harness/schemas").resolve()
_PACKAGE_DIR = Path("packages/meta-harness").resolve()
_SCENARIO_PATH = Path(
    "packages/meta-harness/scenarios/claude-harness/summarize-readme.yaml"
).resolve()


def _check(check_id: str, passed: bool) -> dict:
    return {"id": check_id, "passed": passed, "oracle": "command_exit", "detail": ""}


def _run_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    scenario_extra: dict,
    critical_passed: bool = True,
    graded_checks: list[dict] | None = None,
) -> dict:
    def fake_lifecycle(**kwargs):
        # test_evaluator_artifacts.py の fake_lifecycle と同じ isolation.json 供給パターン
        # （isolation metadata 欠落を hard_failure に強制変換する run_single_attempt のガードを
        # 迂回するため）。
        isolation = {
            "backend": "srt",
            "srt_version": "1.0.0",
            "settings_sha256": "f" * 64,
            "platform_profile_input_sha256": "a" * 64,
        }
        staging = kwargs["staging_dir"]
        staging.mkdir(parents=True, exist_ok=True)
        (staging / "isolation.json").write_text(json.dumps(isolation) + "\n", encoding="utf-8")
        checks = [
            {"id": "c1", "passed": critical_passed, "oracle": "artifact_exists", "detail": ""}
        ]
        return checks, [], graded_checks or [], False, []

    monkeypatch.setattr(ev, "_run_attempt_lifecycle", fake_lifecycle)

    scenario = {
        "id": "summarize-readme",
        "prompt": "irrelevant",
        "setup": [],
        "critical": [{"id": "c1", "text": "n/a", "oracle": "artifact_exists", "path": "x.md"}],
        "checks": [],
        "holdout": False,
        **scenario_extra,
    }
    manifest = {"source_commit": "a" * 40, "config_hash": "b" * 64}
    cli_capabilities = {"claude_version": "2.1.202", "ok": True}
    return ev.run_single_attempt(
        main_root=tmp_path,
        config=mh.DEFAULTS,
        schema_dir=_SCHEMA_DIR,
        package_dir=_PACKAGE_DIR,
        project_dir=tmp_path,
        cand_id="cand-20260707-120000-slug-ab12",
        cand_dir=tmp_path / "cand",
        manifest=manifest,
        target="claude-harness",
        routing_config_base_hash=None,
        scenario=scenario,
        scenario_path=_SCENARIO_PATH,
        suite_hash="c" * 64,
        evaluator_hash="d" * 64,
        attempt=1,
        attempts_total=1,
        cli_capabilities=cli_capabilities,
    )


_GRADED_DECLARATION = {
    "graded": [
        {"id": "g1", "text": "t", "oracle": "command_exit", "command": "true"},
        {"id": "g2", "text": "t", "oracle": "command_exit", "command": "true"},
        {"id": "g3", "text": "t", "oracle": "command_exit", "command": "true"},
    ]
}


class TestGradedDeclaredScenario:
    def test_quality_is_graded_pass_rate_times_100(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        graded = [_check("g1", True), _check("g2", True), _check("g3", False)]
        result = _run_attempt(
            tmp_path,
            monkeypatch,
            scenario_extra=_GRADED_DECLARATION,
            critical_passed=True,
            graded_checks=graded,
        )
        assert result["graded_pass_rate"] == pytest.approx(2 / 3)
        assert result["quality_score"] == pytest.approx(200 / 3)
        assert result["graded"] == graded

    def test_graded_fail_does_not_flip_verdict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """verdict 判定機構は critical のみを見るため無改修（ADR-20260814-050 決定1）。
        graded が全滅していても critical が通っていれば verdict=pass のまま。"""
        graded = [_check("g1", False), _check("g2", False), _check("g3", False)]
        result = _run_attempt(
            tmp_path,
            monkeypatch,
            scenario_extra=_GRADED_DECLARATION,
            critical_passed=True,
            graded_checks=graded,
        )
        assert result["verdict"] == "pass"
        assert result["quality_score"] == 0.0

    def test_critical_fail_still_flips_verdict_regardless_of_graded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        graded = [_check("g1", True), _check("g2", True), _check("g3", True)]
        result = _run_attempt(
            tmp_path,
            monkeypatch,
            scenario_extra=_GRADED_DECLARATION,
            critical_passed=False,
            graded_checks=graded,
        )
        assert result["verdict"] == "fail"
        # gate は fail でも graded 側の quality 式自体は変わらない（gate と graded は独立）。
        assert result["quality_score"] == pytest.approx(100.0)

    def test_penalty_is_recorded_but_does_not_affect_quality(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """self_report が欠落すると penalty_missing_report（既定6）が付くが、graded 宣言時は
        quality に一切算入されない（ADR-20260814-050 決定2: penalty の非採点化）。"""
        graded = [_check("g1", True)]
        result = _run_attempt(
            tmp_path,
            monkeypatch,
            scenario_extra={
                "graded": [{"id": "g1", "text": "t", "oracle": "command_exit", "command": "true"}]
            },
            critical_passed=True,
            graded_checks=graded,
        )
        assert result["penalty"] == 6.0
        assert result["quality_score"] == 100.0


class TestGradedUndeclaredScenario:
    def test_uses_classic_quality_formula_and_omits_graded_fields(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = _run_attempt(
            tmp_path,
            monkeypatch,
            scenario_extra={},
            critical_passed=True,
            graded_checks=[],
        )
        assert "graded" not in result
        assert "graded_pass_rate" not in result
        # critical_pass_rate=1.0, self_report 欠落による penalty_missing_report=6（既定）
        expected = mh.quality_score(1.0, 6.0, mh.DEFAULTS)
        assert result["quality_score"] == expected


class TestResultGradedFieldsPairContract:
    """result.schema.json の `graded`/`graded_pass_rate` は独立した任意プロパティとして
    定義されているため、一方だけが存在する result も schema 検証は通ってしまう
    （`validate_against_schema` は dependentRequired 非対応）。生成側（`run_single_attempt`
    の `if graded_declared:` 分岐）が両方を必ず対で設定することを、schema ではなくここで
    コード契約として固定する（result.schema.json の `graded`/`graded_pass_rate` description
    が本テストを参照している）。"""

    def test_graded_declared_scenario_sets_both_fields_together(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        graded = [_check("g1", True)]
        result = _run_attempt(
            tmp_path,
            monkeypatch,
            scenario_extra=_GRADED_DECLARATION,
            critical_passed=True,
            graded_checks=graded,
        )
        assert "graded" in result
        assert "graded_pass_rate" in result

    def test_graded_undeclared_scenario_omits_both_fields_together(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = _run_attempt(
            tmp_path,
            monkeypatch,
            scenario_extra={},
            critical_passed=True,
            graded_checks=[],
        )
        assert "graded" not in result
        assert "graded_pass_rate" not in result

"""品質スコア計算・critical hard gate・verdict 判定のテスト（Sec3-2）。"""

from __future__ import annotations

from pathlib import Path

from tests.module_loader import load_module

ev = load_module(
    "meta_harness_evaluator_scoring",
    "packages/meta-harness/lib/evaluator.py",
)
mh = load_module(
    "meta_harness_common_scoring",
    "packages/meta-harness/lib/meta_harness_common.py",
)

_CONFIG = {
    "scoring": {
        "critical_weight": 70,
        "penalty_base": 30,
        "penalty_per_item": 5,
        "penalty_missing_report": 6,
    }
}


def _check(passed: bool, check_id: str = "c1") -> dict:
    return {"id": check_id, "passed": passed, "oracle": "command_exit", "detail": ""}


class TestQualityScoreFormula:
    def test_full_pass_zero_penalty_is_max_score(self) -> None:
        assert mh.quality_score(1.0, 0.0, _CONFIG) == 100.0

    def test_full_pass_with_penalty_reduces_only_the_penalty_term(self) -> None:
        # 70 (critical) + max(0, 30 - 2*5) = 70 + 20 = 90
        assert mh.quality_score(1.0, 2.0, _CONFIG) == 90.0

    def test_penalty_never_makes_the_quality_term_negative(self) -> None:
        # 70 + max(0, 30 - 100*5) = 70 + 0 = 70
        assert mh.quality_score(1.0, 100.0, _CONFIG) == 70.0

    def test_partial_critical_pass_rate_scales_the_critical_term(self) -> None:
        # 0.5 * 70 + max(0, 30 - 0) = 35 + 30 = 65
        assert mh.quality_score(0.5, 0.0, _CONFIG) == 65.0


class TestCriticalHardGate:
    """Sec3-2: critical_pass_rate < 1.0 の場合、quality_score に関わらず verdict=fail。"""

    def test_verdict_fail_when_any_critical_check_fails(self) -> None:
        checks = [_check(True, "c1"), _check(False, "c2")]
        verdict = ev._determine_verdict(hard_failure=False, critical_checks=checks)
        assert verdict == "fail"

    def test_verdict_pass_when_all_critical_checks_pass(self) -> None:
        checks = [_check(True, "c1"), _check(True, "c2")]
        verdict = ev._determine_verdict(hard_failure=False, critical_checks=checks)
        assert verdict == "pass"

    def test_verdict_error_on_hard_failure_regardless_of_checks(self) -> None:
        checks = [_check(True, "c1")]
        verdict = ev._determine_verdict(hard_failure=True, critical_checks=checks)
        assert verdict == "error"

    def test_verdict_error_when_no_critical_checks_present(self) -> None:
        verdict = ev._determine_verdict(hard_failure=False, critical_checks=[])
        assert verdict == "error"


class TestEvaluatorHash:
    def test_docker_execution_sources_are_included(self) -> None:
        labels = {label for label, _path in ev._EVALUATOR_SOURCE_FILES}
        assert {
            "lib/scenario_docker.py",
            "lib/scenario_docker_profile.py",
            "lib/scenario_isolation.py",
            "lib/scenario_process.py",
            "docker/broker/broker.py",
            "docker/scenario/Dockerfile",
        } <= labels

    def test_hash_changes_when_backend_source_changes(self, tmp_path: Path) -> None:
        evaluator = tmp_path / "evaluator.py"
        backend = tmp_path / "scenario_docker.py"
        evaluator.write_text("evaluator-v1", encoding="utf-8")
        backend.write_text("backend-v1", encoding="utf-8")
        sources = (("lib/evaluator.py", evaluator), ("lib/scenario_docker.py", backend))

        before = ev._compute_evaluator_hash(sources, _CONFIG["scoring"])
        backend.write_text("backend-v2", encoding="utf-8")
        after = ev._compute_evaluator_hash(sources, _CONFIG["scoring"])

        assert before != after


class TestPassRate:
    def test_empty_checks_is_zero(self) -> None:
        assert ev._pass_rate([]) == 0.0

    def test_all_pass_is_one(self) -> None:
        assert ev._pass_rate([_check(True), _check(True)]) == 1.0

    def test_half_pass_is_half(self) -> None:
        assert ev._pass_rate([_check(True), _check(False)]) == 0.5

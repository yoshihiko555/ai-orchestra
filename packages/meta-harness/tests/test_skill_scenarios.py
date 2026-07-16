"""実 skill scenario suite の所有権・train/holdout・実行 envelope テスト。"""

from __future__ import annotations

import json
from pathlib import Path

from tests.module_loader import load_module

load_module(
    "meta_harness_common",
    "packages/meta-harness/lib/meta_harness_common.py",
)
ev = load_module(
    "meta_harness_evaluator_skill_scenarios",
    "packages/meta-harness/lib/evaluator.py",
)

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_DIR = REPO_ROOT / "packages" / "meta-harness"
SCHEMA_DIR = PACKAGE_DIR / "schemas"


def test_skill_suites_have_one_train_and_one_holdout() -> None:
    for target in ("skill:handoff", "skill:issue-create"):
        paths = ev.validate_target_suite(PACKAGE_DIR, SCHEMA_DIR, target)
        scenarios = [ev.load_scenario(path, SCHEMA_DIR) for path in paths]

        assert len(scenarios) == 2
        assert sum(not scenario["holdout"] for scenario in scenarios) == 1
        assert sum(scenario["holdout"] for scenario in scenarios) == 1
        assert {scenario["target"] for scenario in scenarios} == {target}


def test_skill_scenarios_pin_minimal_output_envelope() -> None:
    for target in ("skill:handoff", "skill:issue-create"):
        for path in ev.validate_target_suite(PACKAGE_DIR, SCHEMA_DIR, target):
            scenario = ev.load_scenario(path, SCHEMA_DIR)
            execution = ev._effective_scenario_execution(scenario, {})

            assert execution["allowed_tools_source"] == "scenario"
            assert execution["max_output_tokens"] == 1024
            assert execution["max_output_tokens_source"] == "scenario"
            assert "Skill" in execution["model_tools"]


def test_issue_create_fixture_is_packaged() -> None:
    fixture = PACKAGE_DIR / "scenarios" / "fixtures" / "fake-gh.py"

    assert fixture.is_file()
    assert "issue-create-call.json" in fixture.read_text(encoding="utf-8")


def test_routing_config_suite_dispatch_and_split_minimum(tmp_path: Path) -> None:
    package_dir = tmp_path / "meta-harness"
    suite_dir = package_dir / "scenarios" / "routing-config"
    suite_dir.mkdir(parents=True)

    def scenario(scenario_id: str, holdout: bool) -> dict:
        return {
            "schema_version": "1.0",
            "id": scenario_id,
            "target": "routing-config",
            "description": scenario_id,
            "prompt": "verify routing config",
            "critical": [
                {
                    "id": "probe",
                    "text": "probe succeeds",
                    "oracle": "command_exit",
                    "command": "true",
                }
            ],
            "holdout": holdout,
        }

    (suite_dir / "train.yaml").write_text(
        json.dumps(scenario("routing-train", False)), encoding="utf-8"
    )
    assert ev.scenario_suite_dir(package_dir, "routing-config") == suite_dir
    try:
        ev.validate_target_suite(package_dir, SCHEMA_DIR, "routing-config")
    except ValueError as exc:
        assert "train >= 1 and holdout >= 1" in str(exc)
    else:
        raise AssertionError("routing-config suite without holdout must be rejected")

    (suite_dir / "holdout.yaml").write_text(
        json.dumps(scenario("routing-holdout", True)), encoding="utf-8"
    )
    paths = ev.validate_target_suite(package_dir, SCHEMA_DIR, "routing-config")
    assert [path.name for path in paths] == ["holdout.yaml", "train.yaml"]

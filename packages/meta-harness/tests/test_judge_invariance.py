"""routing-config 候補から judge を不変に保つ契約（C-4 / EV-91）。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

from tests.module_loader import load_module

ev = load_module(
    "meta_harness_evaluator_judge_invariance",
    "packages/meta-harness/lib/evaluator.py",
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_DIR = REPO_ROOT / "packages" / "meta-harness" / "schemas"
ROUTING_SCENARIOS_DIR = REPO_ROOT / "packages" / "meta-harness" / "scenarios" / "routing-config"
ROUTING_CONFIG_FILE = "agent-routing/cli-tools.yaml"


def _oracle_values(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        oracle = value.get("oracle")
        if isinstance(oracle, str):
            yield oracle
        for nested in value.values():
            yield from _oracle_values(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _oracle_values(nested)


def test_config_patch_ceiling_cannot_target_judge_configuration() -> None:
    for entry in ev.mh.CONFIG_PATCH_ALLOWLIST_CEILING:
        file_value, key_path = entry.split("#", 1)
        assert file_value == ROUTING_CONFIG_FILE, (
            "routing-config ceiling changes require explicit judge-invariance design sign-off: "
            f"{entry}"
        )
        assert not key_path.startswith("judge."), (
            "judge.* must remain outside CONFIG_PATCH_ALLOWLIST_CEILING; "
            f"explicit design sign-off is required for {entry}"
        )


def test_routing_config_scenarios_do_not_use_rubric_judge() -> None:
    for scenario_path in sorted(ROUTING_SCENARIOS_DIR.glob("*.yaml")):
        scenario = yaml.safe_load(scenario_path.read_text(encoding="utf-8"))
        assert "rubric_judge" not in set(_oracle_values(scenario)), (
            "routing-config scenarios must not use rubric_judge without explicit "
            f"judge-invariance design sign-off: {scenario_path}"
        )


def test_judge_resolution_uses_meta_harness_config_only(tmp_path: Path) -> None:
    meta_config_path = tmp_path / ".claude/config/meta-harness/meta-harness.yaml"
    meta_config_path.parent.mkdir(parents=True)
    meta_config_path.write_text(
        "judge:\n  tool: design-approved-judge\n",
        encoding="utf-8",
    )
    routing_config_path = tmp_path / ".claude/config/agent-routing/cli-tools.yaml"
    routing_config_path.parent.mkdir(parents=True)
    routing_config_path.write_text(
        "judge:\n  tool: codex\n",
        encoding="utf-8",
    )

    config = ev.mh.load_config(tmp_path)
    verdict = ev.run_rubric_judge("deterministic rubric", tmp_path, config, SCHEMA_DIR)

    assert config["judge"]["tool"] == "design-approved-judge"
    assert verdict.backend == "design-approved-judge"
    assert verdict.error is True

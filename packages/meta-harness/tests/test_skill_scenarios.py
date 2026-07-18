"""実 skill scenario suite の所有権・train/holdout・実行 envelope テスト。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

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


def test_routing_config_suite_uses_deterministic_critical_oracles() -> None:
    paths = ev.validate_target_suite(PACKAGE_DIR, SCHEMA_DIR, "routing-config")
    scenarios = [ev.load_scenario(path, SCHEMA_DIR) for path in paths]

    assert len(scenarios) == 4
    assert sum(not scenario["holdout"] for scenario in scenarios) == 2
    assert sum(scenario["holdout"] for scenario in scenarios) == 2
    mechanical = [
        scenario for scenario in scenarios if scenario["id"].startswith("verify-routing-config")
    ]
    assert len(mechanical) == 2
    for scenario in mechanical:
        critical = {item["id"]: item for item in scenario["critical"]}
        assert critical["routing-local-config-exists"]["oracle"] == "artifact_exists"
        assert critical["routing-local-config-is-effective"]["oracle"] == "command_exit"
        assert critical["agent-routing-regression-suite-passes"]["oracle"] == "command_exit"
        assert (
            "packages/agent-routing/tests"
            in critical["agent-routing-regression-suite-passes"]["command"]
        )
    behavioral = [scenario for scenario in scenarios if scenario["id"].startswith("route-")]
    assert {scenario["id"] for scenario in behavioral} == {
        "route-debugger-behavior",
        "route-model-behavior-holdout",
    }
    for scenario in scenarios:
        assert all(item["oracle"] != "rubric_judge" for item in scenario["critical"])
        assert scenario["budget"]["max_budget_usd"] <= 3.0
    for scenario in behavioral:
        assert scenario["allowed_tools"] == ["Read", "Write"]
        assert {item["oracle"] for item in scenario["critical"]} == {
            "artifact_exists",
            "command_exit",
        }

    assert (PACKAGE_DIR / "scenarios/fixtures/assert-routing-config-layer.py").is_file()
    assert (PACKAGE_DIR / "scenarios/fixtures/assert-routing-behavior.py").is_file()


def _routing_config_oracle_fixture():
    return load_module(
        "assert_routing_config_layer_fixture",
        "packages/meta-harness/scenarios/fixtures/assert-routing-config-layer.py",
    )


def _routing_behavior_oracle_fixture():
    return load_module(
        "assert_routing_behavior_fixture",
        "packages/meta-harness/scenarios/fixtures/assert-routing-behavior.py",
    )


def _stub_hook_common() -> None:
    # `assert-routing-config-layer.py` の `main()` は
    # `sys.path.insert(0, str(project_root / "packages/core/hooks"))` の後に
    # `from hook_common import load_cli_tools_config` する。テストでは架空の
    # tmp_path project_root を使うため、実物の hook_common を `sys.modules["hook_common"]`
    # へ事前登録しておく（import 解決はキャッシュ優先のため、fake project_root 配下に
    # packages/core/hooks が実在しなくても解決できる）。
    load_module("hook_common", "packages/core/hooks/hook_common.py")
    load_module("route_config", "packages/agent-routing/hooks/route_config.py")


def _write_routing_config_files(
    project_root: Path, *, local_overrides: dict, base: dict | None = None
) -> None:
    base = base if base is not None else {"codex": {"model": "base-model"}}
    config_dir = project_root / ".claude" / "config" / "agent-routing"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "cli-tools.yaml").write_text(yaml.safe_dump(base), encoding="utf-8")
    (config_dir / "cli-tools.local.yaml").write_text(
        yaml.safe_dump(local_overrides), encoding="utf-8"
    )


def _write_applied_config_patch(project_root: Path, items: list[dict]) -> None:
    patch_dir = project_root / ".claude" / "meta-harness"
    patch_dir.mkdir(parents=True, exist_ok=True)
    (patch_dir / "applied-config-patch.json").write_text(
        json.dumps(items, ensure_ascii=False), encoding="utf-8"
    )


class TestRoutingConfigOracleFixtureScopedAllowlist:
    """`assert-routing-config-layer.py` の scoped allowlist チェック（PR #252 R2-4）。"""

    def test_passes_when_patch_keys_match_despite_unrelated_local_override(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """候補が patch した `codex.model` が正しく反映されていれば、無関係な
        既存 local override（`unrelated.nested.value`）が混在していても oracle は通る
        （patch scope に絞る前は全リーフを見て失敗していた）。"""
        _stub_hook_common()
        fixture = _routing_config_oracle_fixture()
        _write_routing_config_files(
            tmp_path,
            local_overrides={
                "codex": {"model": "patched-model"},
                "unrelated": {"nested": {"value": True}},
            },
        )
        _write_applied_config_patch(
            tmp_path,
            [
                {
                    "file": "agent-routing/cli-tools.yaml",
                    "key_path": "codex.model",
                    "value": "patched-model",
                }
            ],
        )
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))

        fixture.main()

    def test_fails_when_patched_key_is_not_effective(self, tmp_path: Path, monkeypatch) -> None:
        _stub_hook_common()
        fixture = _routing_config_oracle_fixture()
        _write_routing_config_files(
            tmp_path,
            local_overrides={"codex": {"model": "different-model"}},
        )
        _write_applied_config_patch(
            tmp_path,
            [
                {
                    "file": "agent-routing/cli-tools.yaml",
                    "key_path": "codex.model",
                    "value": "patched-model",
                }
            ],
        )
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))

        with pytest.raises(AssertionError, match="layering mismatch"):
            fixture.main()

    def test_fails_without_patch_artifact_when_local_has_non_allowlisted_key(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """`applied-config-patch.json` が無い場合は従来どおり全リーフを厳格に検査する
        （後方互換フォールバック）。"""
        _stub_hook_common()
        fixture = _routing_config_oracle_fixture()
        _write_routing_config_files(
            tmp_path,
            local_overrides={"unrelated": {"nested": {"value": True}}},
        )
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(tmp_path))

        with pytest.raises(AssertionError, match="non-allowlisted"):
            fixture.main()


@pytest.mark.parametrize(
    ("scenario_id", "base", "patched_value", "artifact_name", "artifact"),
    [
        (
            "train",
            {"agents": {"debugger": {"tool": "codex"}}},
            "claude-direct",
            "routing-behavior-train.json",
            {
                "resolved_key": "agents.debugger.tool",
                "resolved_value": "codex",
                "action": "delegate-debug-analysis",
                "result": {"first_duplicate": 1},
            },
        ),
        (
            "holdout",
            {"antigravity": {"model": "gemini-3.1-pro-high"}},
            "gemini-3.5-flash-high",
            "routing-behavior-holdout.json",
            {
                "resolved_key": "antigravity.model",
                "resolved_value": "gemini-3.1-pro-high",
                "action": "prioritize-depth",
                "result": {"steps": ["inspect", "cross-check", "synthesize"]},
            },
        ),
    ],
)
def test_routing_behavior_oracle_outcome_varies_with_materialized_value(
    tmp_path: Path,
    scenario_id: str,
    base: dict,
    patched_value: str,
    artifact_name: str,
    artifact: dict,
) -> None:
    _stub_hook_common()
    fixture = _routing_behavior_oracle_fixture()
    _write_routing_config_files(tmp_path, local_overrides={}, base=base)
    artifact_path = tmp_path / artifact_name
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")

    fixture.assert_behavior(tmp_path, scenario_id, Path(artifact_name))

    key_path = str(artifact["resolved_key"])
    first, second, *rest = key_path.split(".")
    local: dict = {first: {second: patched_value}}
    if rest:
        local = {first: {second: {rest[0]: patched_value}}}
    _write_routing_config_files(tmp_path, local_overrides=local, base=base)

    with pytest.raises(AssertionError, match="materialized routing config"):
        fixture.assert_behavior(tmp_path, scenario_id, Path(artifact_name))


@pytest.mark.parametrize(
    ("configured_tool", "expected"),
    [
        (
            "codex",
            {
                "resolved_value": "codex",
                "action": "delegate-debug-analysis",
                "result": {"first_duplicate": 1},
            },
        ),
        (
            "antigravity",
            {
                "resolved_value": "antigravity",
                "action": "research-sequence-pattern",
                "result": {"unique_count": 4},
            },
        ),
        (
            "claude-direct",
            {
                "resolved_value": "claude-direct",
                "action": "solve-sequence-directly",
                "result": {"sorted": [1, 1, 3, 4, 5]},
            },
        ),
        (
            "auto",
            {
                "resolved_value": "auto",
                "action": "select-debug-route",
                "result": {"selected_tool": "codex"},
            },
        ),
    ],
)
def test_routing_behavior_oracle_covers_agent_router_tool_values(
    tmp_path: Path, configured_tool: str, expected: dict
) -> None:
    _stub_hook_common()
    fixture = _routing_behavior_oracle_fixture()
    _write_routing_config_files(
        tmp_path,
        base={
            "codex": {"enabled": True},
            "antigravity": {"enabled": True},
            "agents": {"debugger": {"tool": configured_tool}},
        },
        local_overrides={},
    )
    artifact_name = "routing-behavior-train.json"
    artifact = {
        "resolved_key": "agents.debugger.tool",
        **expected,
    }
    (tmp_path / artifact_name).write_text(json.dumps(artifact), encoding="utf-8")

    fixture.assert_behavior(tmp_path, "train", Path(artifact_name))


def test_routing_behavior_oracle_uses_disabled_cli_fallback(
    tmp_path: Path,
) -> None:
    _stub_hook_common()
    fixture = _routing_behavior_oracle_fixture()
    _write_routing_config_files(
        tmp_path,
        base={
            "codex": {"enabled": True},
            "agents": {"debugger": {"tool": "codex"}},
        },
        local_overrides={"codex": {"enabled": False}},
    )
    artifact_name = "routing-behavior-train.json"
    artifact_path = tmp_path / artifact_name
    artifact_path.write_text(
        json.dumps(
            {
                "resolved_key": "agents.debugger.tool",
                "resolved_value": "claude-direct",
                "action": "solve-sequence-directly",
                "result": {"sorted": [1, 1, 3, 4, 5]},
            }
        ),
        encoding="utf-8",
    )

    fixture.assert_behavior(tmp_path, "train", Path(artifact_name))

    artifact_path.write_text(
        json.dumps(
            {
                "resolved_key": "agents.debugger.tool",
                "resolved_value": "codex",
                "action": "delegate-debug-analysis",
                "result": {"first_duplicate": 1},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="materialized routing config"):
        fixture.assert_behavior(tmp_path, "train", Path(artifact_name))

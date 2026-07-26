"""実 skill scenario suite の所有権・train/holdout・実行 envelope テスト。"""

from __future__ import annotations

import datetime
import hashlib
import json
import subprocess
import sys
import tempfile
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
    # `skill:task-state` is excluded here: like `routing-config` (see
    # `test_task_state_suite_covers_add_phase_ac_behavior` and
    # `test_routing_config_suite_uses_deterministic_critical_oracles` below), it carries 2 train +
    # 2 holdout scenarios rather than the 1+1 baseline (Issue #297 / PR #326 review round 1: the
    # extra pair locks down `add-phase`'s Acceptance Criteria behavior specifically, alongside the
    # original mark-task-done/record-decision pair).
    for target in (
        "skill:handoff",
        "skill:issue-create",
        "skill:codex-system",
        "skill:antigravity-system",
        "skill:issue-fix",
    ):
        paths = ev.validate_target_suite(PACKAGE_DIR, SCHEMA_DIR, target)
        scenarios = [ev.load_scenario(path, SCHEMA_DIR) for path in paths]

        assert len(scenarios) == 2
        assert sum(not scenario["holdout"] for scenario in scenarios) == 1
        assert sum(scenario["holdout"] for scenario in scenarios) == 1
        assert {scenario["target"] for scenario in scenarios} == {target}


def test_task_state_suite_covers_add_phase_ac_behavior() -> None:
    """Issue #297 / PR #326 review round 1: the `add-phase` Acceptance Criteria behavior contract
    (agreed-upon AC reflected verbatim; a direct call with no agreed AC must not fabricate one and
    must ask the user instead) needs its own dedicated scenario pair, so `skill:task-state` grows
    from the 1 train + 1 holdout baseline to 2 + 2 (mirroring how `routing-config` already carries
    more than the baseline pair; see `test_routing_config_suite_uses_deterministic_critical_oracles`)."""
    paths = ev.validate_target_suite(PACKAGE_DIR, SCHEMA_DIR, "skill:task-state")
    scenarios = [ev.load_scenario(path, SCHEMA_DIR) for path in paths]

    assert len(scenarios) == 4
    assert sum(not scenario["holdout"] for scenario in scenarios) == 2
    assert sum(scenario["holdout"] for scenario in scenarios) == 2
    assert {scenario["target"] for scenario in scenarios} == {"skill:task-state"}

    by_id = {scenario["id"]: scenario for scenario in scenarios}
    assert by_id.keys() == {
        "mark-task-done",
        "record-architecture-decision-holdout",
        "add-phase-with-agreed-ac",
        "add-phase-direct-call-confirms-ac-holdout",
    }
    assert by_id["add-phase-with-agreed-ac"]["holdout"] is False
    assert by_id["add-phase-direct-call-confirms-ac-holdout"]["holdout"] is True

    with_ac_commands = [item["command"] for item in by_id["add-phase-with-agreed-ac"]["critical"]]
    assert any("add-phase-with-ac" in command for command in with_ac_commands)

    no_ac_commands = [
        item["command"] for item in by_id["add-phase-direct-call-confirms-ac-holdout"]["critical"]
    ]
    assert any("add-phase-no-ac" in command for command in no_ac_commands)


def test_skill_scenarios_pin_minimal_output_envelope() -> None:
    for target in (
        "skill:handoff",
        "skill:issue-create",
        "skill:codex-system",
        "skill:antigravity-system",
        "skill:issue-fix",
        "skill:task-state",
    ):
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


def _cli_skill_route_oracle_fixture():
    return load_module(
        "assert_cli_skill_route_fixture",
        "packages/meta-harness/scenarios/fixtures/assert-cli-skill-route.py",
    )


def test_cli_skill_route_oracle_matches_codex_analysis_resolution(tmp_path: Path) -> None:
    _stub_hook_common()
    fixture = _cli_skill_route_oracle_fixture()
    _write_routing_config_files(
        tmp_path,
        base={
            "codex": {
                "enabled": True,
                "model": "codex-model-x",
                "flags": "--full-auto",
                "sandbox": {"analysis": "read-only"},
            },
            "antigravity": {"enabled": True},
            "agents": {"debugger": {"tool": "codex"}},
        },
        local_overrides={},
    )
    artifact = {
        "engine": "codex",
        "resolved_tool": "codex",
        "codex_enabled": True,
        "antigravity_enabled": True,
        "model": "codex-model-x",
        "sandbox": "read-only",
        "flags": "--full-auto",
    }
    artifact_path = tmp_path / "codex-route-decision.json"
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")

    fixture.assert_route(tmp_path, "codex", Path("codex-route-decision.json"))

    artifact_path.write_text(json.dumps({**artifact, "model": "wrong-model"}), encoding="utf-8")
    with pytest.raises(AssertionError, match="materialized cli-tools config"):
        fixture.assert_route(tmp_path, "codex", Path("codex-route-decision.json"))


def test_cli_skill_route_oracle_uses_sandbox_analysis_fallback_default(tmp_path: Path) -> None:
    """No `codex.sandbox.analysis` key configured -> effective default, never the literal
    "analysis" (PR #264 review point 3)."""
    _stub_hook_common()
    fixture = _cli_skill_route_oracle_fixture()
    _write_routing_config_files(
        tmp_path,
        base={
            "codex": {"enabled": True, "model": "codex-model-x", "flags": "--full-auto"},
            "antigravity": {"enabled": True},
            "agents": {"debugger": {"tool": "codex"}},
        },
        local_overrides={},
    )
    artifact = {
        "engine": "codex",
        "resolved_tool": "codex",
        "codex_enabled": True,
        "antigravity_enabled": True,
        "model": "codex-model-x",
        "sandbox": "read-only",
        "flags": "--full-auto",
    }
    artifact_path = tmp_path / "codex-route-decision.json"
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")

    fixture.assert_route(tmp_path, "codex", Path("codex-route-decision.json"))

    artifact_path.write_text(json.dumps({**artifact, "sandbox": "analysis"}), encoding="utf-8")
    with pytest.raises(AssertionError, match="materialized cli-tools config"):
        fixture.assert_route(tmp_path, "codex", Path("codex-route-decision.json"))


def test_cli_skill_route_oracle_matches_codex_disabled_fallback(tmp_path: Path) -> None:
    _stub_hook_common()
    fixture = _cli_skill_route_oracle_fixture()
    _write_routing_config_files(
        tmp_path,
        base={
            "codex": {"enabled": True},
            "antigravity": {"enabled": False},
            "agents": {"debugger": {"tool": "codex"}},
        },
        local_overrides={"codex": {"enabled": False}},
    )
    artifact = {
        "engine": "claude-direct",
        "resolved_tool": "claude-direct",
        "codex_enabled": False,
        "antigravity_enabled": False,
    }
    artifact_path = tmp_path / "codex-route-decision.json"
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")

    fixture.assert_route(tmp_path, "codex", Path("codex-route-decision.json"))

    artifact_path.write_text(
        json.dumps({**artifact, "engine": "codex", "resolved_tool": "codex"}), encoding="utf-8"
    )
    with pytest.raises(AssertionError, match="materialized cli-tools config"):
        fixture.assert_route(tmp_path, "codex", Path("codex-route-decision.json"))


def test_cli_skill_route_oracle_resolves_auto_to_codex_when_codex_enabled(tmp_path: Path) -> None:
    """`agents.debugger.tool: auto` must resolve via build_aliases codex-first priority,
    not collapse straight to claude-direct (PR #264 review point 4)."""
    _stub_hook_common()
    fixture = _cli_skill_route_oracle_fixture()
    _write_routing_config_files(
        tmp_path,
        base={
            "codex": {
                "enabled": True,
                "model": "codex-model-x",
                "flags": "--full-auto",
                "sandbox": {"analysis": "read-only"},
            },
            "antigravity": {"enabled": True},
            "agents": {"debugger": {"tool": "auto"}},
        },
        local_overrides={},
    )
    artifact = {
        "engine": "codex",
        "resolved_tool": "codex",
        "codex_enabled": True,
        "antigravity_enabled": True,
        "model": "codex-model-x",
        "sandbox": "read-only",
        "flags": "--full-auto",
    }
    artifact_path = tmp_path / "codex-route-decision.json"
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")

    fixture.assert_route(tmp_path, "codex", Path("codex-route-decision.json"))


def test_cli_skill_route_oracle_resolves_auto_to_antigravity_when_codex_disabled(
    tmp_path: Path,
) -> None:
    """`agents.debugger.tool: auto` with codex disabled must fall through to the next
    enabled alias (antigravity), matching build_aliases' codex -> antigravity ->
    claude-direct order, instead of collapsing to claude-direct."""
    _stub_hook_common()
    fixture = _cli_skill_route_oracle_fixture()
    _write_routing_config_files(
        tmp_path,
        base={
            "codex": {"enabled": False},
            "antigravity": {
                "enabled": True,
                "model": "gemini-3.1-pro-high",
                "model_allowlist": ["gemini-3.1-pro-high"],
            },
            "agents": {"debugger": {"tool": "auto"}},
        },
        local_overrides={},
    )
    artifact = {
        "engine": "antigravity",
        "resolved_tool": "antigravity",
        "codex_enabled": False,
        "antigravity_enabled": True,
        "model": "gemini-3.1-pro-high",
        "allowlist_warning": False,
    }
    artifact_path = tmp_path / "codex-route-decision.json"
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")

    fixture.assert_route(tmp_path, "codex", Path("codex-route-decision.json"))


def test_cli_skill_route_oracle_resolves_auto_to_antigravity_when_both_enabled_for_researcher(
    tmp_path: Path,
) -> None:
    """`agents.researcher.tool: auto` is a research task, so it must prefer Antigravity
    first even though Codex is also enabled -- the opposite priority from the debugger
    probe (PR #264 review round 2, task-dependent auto priority)."""
    _stub_hook_common()
    fixture = _cli_skill_route_oracle_fixture()
    _write_routing_config_files(
        tmp_path,
        base={
            "codex": {
                "enabled": True,
                "model": "codex-model-x",
                "flags": "--full-auto",
                "sandbox": {"analysis": "read-only"},
            },
            "antigravity": {
                "enabled": True,
                "model": "gemini-3.1-pro-high",
                "model_allowlist": ["gemini-3.1-pro-high"],
            },
            "agents": {"researcher": {"tool": "auto"}},
        },
        local_overrides={},
    )
    artifact = {
        "engine": "antigravity",
        "resolved_tool": "antigravity",
        "codex_enabled": True,
        "antigravity_enabled": True,
        "model": "gemini-3.1-pro-high",
        "allowlist_warning": False,
    }
    artifact_path = tmp_path / "antigravity-route-decision.json"
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")

    fixture.assert_route(tmp_path, "antigravity", Path("antigravity-route-decision.json"))

    artifact_path.write_text(json.dumps({**artifact, "engine": "codex"}), encoding="utf-8")
    with pytest.raises(AssertionError, match="materialized cli-tools config"):
        fixture.assert_route(tmp_path, "antigravity", Path("antigravity-route-decision.json"))


def test_cli_skill_route_oracle_resolves_auto_to_codex_when_antigravity_disabled_for_researcher(
    tmp_path: Path,
) -> None:
    """`agents.researcher.tool: auto` with antigravity disabled must fall through to the
    next enabled alias (codex) instead of collapsing straight to claude-direct."""
    _stub_hook_common()
    fixture = _cli_skill_route_oracle_fixture()
    _write_routing_config_files(
        tmp_path,
        base={
            "codex": {
                "enabled": True,
                "model": "codex-model-x",
                "flags": "--full-auto",
                "sandbox": {"analysis": "read-only"},
            },
            "antigravity": {"enabled": False},
            "agents": {"researcher": {"tool": "auto"}},
        },
        local_overrides={},
    )
    artifact = {
        "engine": "codex",
        "resolved_tool": "codex",
        "codex_enabled": True,
        "antigravity_enabled": False,
        "model": "codex-model-x",
        "sandbox": "read-only",
        "flags": "--full-auto",
    }
    artifact_path = tmp_path / "antigravity-route-decision.json"
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")

    fixture.assert_route(tmp_path, "antigravity", Path("antigravity-route-decision.json"))


def test_cli_skill_route_oracle_resolves_auto_to_claude_direct_when_no_cli_enabled(
    tmp_path: Path,
) -> None:
    _stub_hook_common()
    fixture = _cli_skill_route_oracle_fixture()
    _write_routing_config_files(
        tmp_path,
        base={
            "codex": {"enabled": False},
            "antigravity": {"enabled": False},
            "agents": {"researcher": {"tool": "auto"}},
        },
        local_overrides={},
    )
    artifact = {
        "engine": "claude-direct",
        "resolved_tool": "claude-direct",
        "codex_enabled": False,
        "antigravity_enabled": False,
    }
    artifact_path = tmp_path / "antigravity-route-decision.json"
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")

    fixture.assert_route(tmp_path, "antigravity", Path("antigravity-route-decision.json"))


def test_cli_skill_route_oracle_matches_antigravity_allowlist_warning(tmp_path: Path) -> None:
    _stub_hook_common()
    fixture = _cli_skill_route_oracle_fixture()
    _write_routing_config_files(
        tmp_path,
        base={
            "codex": {"enabled": True},
            "antigravity": {
                "enabled": True,
                "model": "gemini-3.1-pro-high",
                "model_allowlist": ["gemini-3.1-pro-high"],
            },
            "agents": {"researcher": {"tool": "antigravity"}},
        },
        local_overrides={"antigravity": {"model": "gemini-9.9-unlisted"}},
    )
    artifact = {
        "engine": "antigravity",
        "resolved_tool": "antigravity",
        "codex_enabled": True,
        "antigravity_enabled": True,
        "model": "gemini-9.9-unlisted",
        "allowlist_warning": True,
    }
    artifact_path = tmp_path / "antigravity-route-decision.json"
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")

    fixture.assert_route(tmp_path, "antigravity", Path("antigravity-route-decision.json"))

    artifact_path.write_text(json.dumps({**artifact, "allowlist_warning": False}), encoding="utf-8")
    with pytest.raises(AssertionError, match="materialized cli-tools config"):
        fixture.assert_route(tmp_path, "antigravity", Path("antigravity-route-decision.json"))


def test_cli_skill_route_oracle_matches_antigravity_within_allowlist(tmp_path: Path) -> None:
    _stub_hook_common()
    fixture = _cli_skill_route_oracle_fixture()
    _write_routing_config_files(
        tmp_path,
        base={
            "codex": {"enabled": True},
            "antigravity": {
                "enabled": True,
                "model": "gemini-3.1-pro-high",
                "model_allowlist": ["gemini-3.1-pro-high"],
            },
            "agents": {"researcher": {"tool": "antigravity"}},
        },
        local_overrides={},
    )
    artifact = {
        "engine": "antigravity",
        "resolved_tool": "antigravity",
        "codex_enabled": True,
        "antigravity_enabled": True,
        "model": "gemini-3.1-pro-high",
        "allowlist_warning": False,
    }
    artifact_path = tmp_path / "antigravity-route-decision.json"
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")

    fixture.assert_route(tmp_path, "antigravity", Path("antigravity-route-decision.json"))


def _issue_fix_decision_oracle_fixture():
    return load_module(
        "assert_issue_fix_decision_fixture",
        "packages/meta-harness/scenarios/fixtures/assert-issue-fix-decision.py",
    )


def _real_issue_fixture_bytes(scenario_filename: str) -> bytes:
    """Extract the exact `.meta-harness/gh-issue-fixture.json` bytes a real issue-fix scenario's
    `setup:` step writes, by actually executing that `setup:` script in an isolated tmp dir.

    Ties these oracle tests to the real scenario definitions (and therefore to the sha256 table
    `assert-issue-fix-decision.py` hardcodes) instead of a hand-transcribed copy that could
    silently drift from either (PR #266 review round 3, point 4).
    """
    scenario_path = PACKAGE_DIR / "scenarios" / "skill" / "issue-fix" / scenario_filename
    doc = yaml.safe_load(scenario_path.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        for command in doc["setup"]:
            subprocess.run(command, shell=True, cwd=tmp_dir, capture_output=True)
        return (tmp_dir / ".meta-harness" / "gh-issue-fixture.json").read_bytes()


def test_issue_fix_gh_fixture_is_packaged() -> None:
    fixture = PACKAGE_DIR / "scenarios" / "fixtures" / "fake-gh-issue-view.py"

    assert fixture.is_file()
    assert "issue view" in fixture.read_text(encoding="utf-8")


def test_issue_fix_decision_oracle_matches_bug_label_policy(tmp_path: Path) -> None:
    fixture = _issue_fix_decision_oracle_fixture()
    (tmp_path / "gh-issue-fixture.json").write_bytes(
        _real_issue_fixture_bytes("fix-greet-none-bug.yaml")
    )
    (tmp_path / "issue-fix-decision.json").write_text(
        json.dumps(
            {
                "branch": "fix/issue-301-greet-none-guard",
                "commit_message": "fix: greet() が None を安全に処理するよう修正\n\nCloses #301",
                "pr_label": "bug",
            }
        ),
        encoding="utf-8",
    )

    fixture.assert_decision(
        tmp_path, Path("gh-issue-fixture.json"), Path("issue-fix-decision.json")
    )


def test_issue_fix_decision_oracle_matches_feature_label_policy(tmp_path: Path) -> None:
    fixture = _issue_fix_decision_oracle_fixture()
    (tmp_path / "gh-issue-fixture.json").write_bytes(
        _real_issue_fixture_bytes("fix-formal-greeting-feature-holdout.yaml")
    )
    (tmp_path / "issue-fix-decision.json").write_text(
        json.dumps(
            {
                "branch": "feat/issue-305-formal-greeting",
                "commit_message": "feat: build_greeting() に formal モードを追加\n\nCloses #305",
                "pr_label": "enhancement",
            }
        ),
        encoding="utf-8",
    )

    fixture.assert_decision(
        tmp_path, Path("gh-issue-fixture.json"), Path("issue-fix-decision.json")
    )


def test_issue_fix_decision_oracle_rejects_wrong_branch_prefix(tmp_path: Path) -> None:
    fixture = _issue_fix_decision_oracle_fixture()
    (tmp_path / "gh-issue-fixture.json").write_bytes(
        _real_issue_fixture_bytes("fix-greet-none-bug.yaml")
    )
    (tmp_path / "issue-fix-decision.json").write_text(
        json.dumps(
            {
                "branch": "feat/issue-301-greet-none-guard",
                "commit_message": "fix: greet() が None を安全に処理するよう修正\n\nCloses #301",
                "pr_label": "bug",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match="does not follow"):
        fixture.assert_decision(
            tmp_path, Path("gh-issue-fixture.json"), Path("issue-fix-decision.json")
        )


def test_issue_fix_decision_oracle_rejects_missing_closes_reference(tmp_path: Path) -> None:
    fixture = _issue_fix_decision_oracle_fixture()
    (tmp_path / "gh-issue-fixture.json").write_bytes(
        _real_issue_fixture_bytes("fix-greet-none-bug.yaml")
    )
    (tmp_path / "issue-fix-decision.json").write_text(
        json.dumps(
            {
                "branch": "fix/issue-301-greet-none-guard",
                "commit_message": "fix: greet() が None を安全に処理するよう修正",
                "pr_label": "bug",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match="Closes #301"):
        fixture.assert_decision(
            tmp_path, Path("gh-issue-fixture.json"), Path("issue-fix-decision.json")
        )


def test_issue_fix_decision_oracle_rejects_wrong_pr_label(tmp_path: Path) -> None:
    fixture = _issue_fix_decision_oracle_fixture()
    (tmp_path / "gh-issue-fixture.json").write_bytes(
        _real_issue_fixture_bytes("fix-greet-none-bug.yaml")
    )
    (tmp_path / "issue-fix-decision.json").write_text(
        json.dumps(
            {
                "branch": "fix/issue-301-greet-none-guard",
                "commit_message": "fix: greet() が None を安全に処理するよう修正\n\nCloses #301",
                "pr_label": "enhancement",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match="pr_label"):
        fixture.assert_decision(
            tmp_path, Path("gh-issue-fixture.json"), Path("issue-fix-decision.json")
        )


def test_issue_fix_decision_oracle_rejects_empty_branch_slug(tmp_path: Path) -> None:
    fixture = _issue_fix_decision_oracle_fixture()
    (tmp_path / "gh-issue-fixture.json").write_bytes(
        _real_issue_fixture_bytes("fix-greet-none-bug.yaml")
    )
    (tmp_path / "issue-fix-decision.json").write_text(
        json.dumps(
            {
                "branch": "fix/issue-301-",
                "commit_message": "fix: greet() が None を安全に処理するよう修正\n\nCloses #301",
                "pr_label": "bug",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match="empty slug"):
        fixture.assert_decision(
            tmp_path, Path("gh-issue-fixture.json"), Path("issue-fix-decision.json")
        )


def test_issue_fix_decision_oracle_rejects_non_kebab_case_branch_slug(tmp_path: Path) -> None:
    fixture = _issue_fix_decision_oracle_fixture()
    (tmp_path / "gh-issue-fixture.json").write_bytes(
        _real_issue_fixture_bytes("fix-greet-none-bug.yaml")
    )
    (tmp_path / "issue-fix-decision.json").write_text(
        json.dumps(
            {
                "branch": "fix/issue-301-Greet_None_Guard",
                "commit_message": "fix: greet() が None を安全に処理するよう修正\n\nCloses #301",
                "pr_label": "bug",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match="not kebab-case"):
        fixture.assert_decision(
            tmp_path, Path("gh-issue-fixture.json"), Path("issue-fix-decision.json")
        )


def test_issue_fix_decision_oracle_rejects_overlong_branch_slug(tmp_path: Path) -> None:
    fixture = _issue_fix_decision_oracle_fixture()
    (tmp_path / "gh-issue-fixture.json").write_bytes(
        _real_issue_fixture_bytes("fix-greet-none-bug.yaml")
    )
    overlong_slug = "-".join(["greet-none-guard"] * 3)  # well over 30 chars
    (tmp_path / "issue-fix-decision.json").write_text(
        json.dumps(
            {
                "branch": f"fix/issue-301-{overlong_slug}",
                "commit_message": "fix: greet() が None を安全に処理するよう修正\n\nCloses #301",
                "pr_label": "bug",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match="exceeds the 30-char convention"):
        fixture.assert_decision(
            tmp_path, Path("gh-issue-fixture.json"), Path("issue-fix-decision.json")
        )


def test_issue_fix_decision_oracle_rejects_extra_key(tmp_path: Path) -> None:
    fixture = _issue_fix_decision_oracle_fixture()
    (tmp_path / "gh-issue-fixture.json").write_bytes(
        _real_issue_fixture_bytes("fix-greet-none-bug.yaml")
    )
    (tmp_path / "issue-fix-decision.json").write_text(
        json.dumps(
            {
                "branch": "fix/issue-301-greet-none-guard",
                "commit_message": "fix: greet() が None を安全に処理するよう修正\n\nCloses #301",
                "pr_label": "bug",
                "confidence": "high",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match="decision artifact keys"):
        fixture.assert_decision(
            tmp_path, Path("gh-issue-fixture.json"), Path("issue-fix-decision.json")
        )


def test_issue_fix_decision_oracle_rejects_missing_key(tmp_path: Path) -> None:
    fixture = _issue_fix_decision_oracle_fixture()
    (tmp_path / "gh-issue-fixture.json").write_bytes(
        _real_issue_fixture_bytes("fix-greet-none-bug.yaml")
    )
    (tmp_path / "issue-fix-decision.json").write_text(
        json.dumps({"branch": "fix/issue-301-greet-none-guard", "pr_label": "bug"}),
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match="decision artifact keys"):
        fixture.assert_decision(
            tmp_path, Path("gh-issue-fixture.json"), Path("issue-fix-decision.json")
        )


def test_issue_fix_decision_oracle_rejects_tampered_fixture_content(tmp_path: Path) -> None:
    """A candidate rewriting `.meta-harness/gh-issue-fixture.json` to fake a friendlier label
    must not be able to steer the oracle's expected decision (PR #266 review round 3, point 4):
    the byte content no longer matches any known-good sha256, so the oracle fails closed instead
    of trusting the (attacker-controlled) `labels`/`number` fields it would otherwise re-parse."""
    fixture = _issue_fix_decision_oracle_fixture()
    real_bytes = _real_issue_fixture_bytes("fix-greet-none-bug.yaml")
    tampered = real_bytes.replace(b'"bug"', b'"task"')
    assert tampered != real_bytes
    (tmp_path / "gh-issue-fixture.json").write_bytes(tampered)
    (tmp_path / "issue-fix-decision.json").write_text(
        json.dumps(
            {
                "branch": "fix/issue-301-greet-none-guard",
                "commit_message": "fix: greet() が None を安全に処理するよう修正\n\nCloses #301",
                "pr_label": "bug",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match="does not match any known-good fixture"):
        fixture.assert_decision(
            tmp_path, Path("gh-issue-fixture.json"), Path("issue-fix-decision.json")
        )


def test_issue_fix_gh_fixture_rejects_mismatched_issue_number(tmp_path: Path) -> None:
    fixture_script = PACKAGE_DIR / "scenarios" / "fixtures" / "fake-gh-issue-view.py"
    meta_dir = tmp_path / ".meta-harness"
    meta_dir.mkdir()
    # Real (known-good) fixture bytes are required now that the stub itself hash-gates before
    # serving (PR #266 review round 4, point 3); otherwise this would spuriously pass the hash
    # gate's own "does not match" error instead of exercising the intended number-mismatch path.
    (meta_dir / "gh-issue-fixture.json").write_bytes(
        _real_issue_fixture_bytes("fix-greet-none-bug.yaml")
    )

    result = subprocess.run(
        [
            sys.executable,
            str(fixture_script),
            "issue",
            "view",
            "999",
            "--json",
            "number,title,body,labels,assignees",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "does not match" in result.stderr
    assert not (meta_dir / "gh-call-log.jsonl").exists()


def test_issue_fix_gh_fixture_rejects_missing_json_flag(tmp_path: Path) -> None:
    fixture_script = PACKAGE_DIR / "scenarios" / "fixtures" / "fake-gh-issue-view.py"
    meta_dir = tmp_path / ".meta-harness"
    meta_dir.mkdir()
    (meta_dir / "gh-issue-fixture.json").write_text(
        json.dumps({"number": 301, "title": "bug", "labels": [{"name": "bug"}]}),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(fixture_script), "issue", "view", "301"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "--json" in result.stderr


def test_issue_fix_gh_fixture_rejects_partial_json_fields(tmp_path: Path) -> None:
    fixture_script = PACKAGE_DIR / "scenarios" / "fixtures" / "fake-gh-issue-view.py"
    meta_dir = tmp_path / ".meta-harness"
    meta_dir.mkdir()
    (meta_dir / "gh-issue-fixture.json").write_text(
        json.dumps({"number": 301, "title": "bug", "labels": [{"name": "bug"}]}),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(fixture_script), "issue", "view", "301", "--json", "number,title"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "--json field set" in result.stderr


def test_issue_fix_gh_fixture_accepts_matching_request(tmp_path: Path) -> None:
    fixture_script = PACKAGE_DIR / "scenarios" / "fixtures" / "fake-gh-issue-view.py"
    meta_dir = tmp_path / ".meta-harness"
    meta_dir.mkdir()
    fixture_bytes = _real_issue_fixture_bytes("fix-greet-none-bug.yaml")
    payload = json.loads(fixture_bytes)
    (meta_dir / "gh-issue-fixture.json").write_bytes(fixture_bytes)

    result = subprocess.run(
        [
            sys.executable,
            str(fixture_script),
            "issue",
            "view",
            "301",
            "--json",
            "number,title,body,labels,assignees",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout) == payload


def test_issue_fix_gh_fixture_rejects_tampered_fixture_bytes(tmp_path: Path) -> None:
    """PR #266 review round 4, point 3: the stub itself must hash-gate before serving, closing
    the tamper-then-restore window (edit the fixture, let the stub read it, restore it) that a
    check performed only by the *oracle* after the run could not observe."""
    fixture_script = PACKAGE_DIR / "scenarios" / "fixtures" / "fake-gh-issue-view.py"
    meta_dir = tmp_path / ".meta-harness"
    meta_dir.mkdir()
    real_bytes = _real_issue_fixture_bytes("fix-greet-none-bug.yaml")
    tampered = real_bytes.replace(b'"bug"', b'"task"')
    assert tampered != real_bytes
    (meta_dir / "gh-issue-fixture.json").write_bytes(tampered)

    result = subprocess.run(
        [
            sys.executable,
            str(fixture_script),
            "issue",
            "view",
            "301",
            "--json",
            "number,title,body,labels,assignees",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "does not match any known-good fixture" in result.stderr
    assert not (meta_dir / "gh-call-log.jsonl").exists()


def test_issue_fix_gh_fixture_rejects_extra_argv_flag(tmp_path: Path) -> None:
    """PR #266 review round 5, point 2: an appended `--repo other/repo` (or any other extra
    argument) must fail closed instead of being silently ignored."""
    fixture_script = PACKAGE_DIR / "scenarios" / "fixtures" / "fake-gh-issue-view.py"
    meta_dir = tmp_path / ".meta-harness"
    meta_dir.mkdir()
    (meta_dir / "gh-issue-fixture.json").write_bytes(
        _real_issue_fixture_bytes("fix-greet-none-bug.yaml")
    )

    result = subprocess.run(
        [
            sys.executable,
            str(fixture_script),
            "issue",
            "view",
            "301",
            "--json",
            "number,title,body,labels,assignees",
            "--repo",
            "other/repo",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "does not exactly match" in result.stderr
    assert not (meta_dir / "gh-call-log.jsonl").exists()


def test_issue_fix_gh_fixture_rejects_duplicate_json_flag(tmp_path: Path) -> None:
    fixture_script = PACKAGE_DIR / "scenarios" / "fixtures" / "fake-gh-issue-view.py"
    meta_dir = tmp_path / ".meta-harness"
    meta_dir.mkdir()
    (meta_dir / "gh-issue-fixture.json").write_bytes(
        _real_issue_fixture_bytes("fix-greet-none-bug.yaml")
    )

    result = subprocess.run(
        [
            sys.executable,
            str(fixture_script),
            "issue",
            "view",
            "301",
            "--json",
            "number,title,body,labels,assignees",
            "--json",
            "number",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "does not exactly match" in result.stderr
    assert not (meta_dir / "gh-call-log.jsonl").exists()


def test_issue_fix_gh_fixture_appends_call_log_entry_on_success(tmp_path: Path) -> None:
    fixture_script = PACKAGE_DIR / "scenarios" / "fixtures" / "fake-gh-issue-view.py"
    meta_dir = tmp_path / ".meta-harness"
    meta_dir.mkdir()
    fixture_bytes = _real_issue_fixture_bytes("fix-greet-none-bug.yaml")
    (meta_dir / "gh-issue-fixture.json").write_bytes(fixture_bytes)
    expected_sha256 = hashlib.sha256(fixture_bytes).hexdigest()

    for _ in range(2):
        result = subprocess.run(
            [
                sys.executable,
                str(fixture_script),
                "issue",
                "view",
                "301",
                "--json",
                "number,title,body,labels,assignees",
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0

    log_lines = (meta_dir / "gh-call-log.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(log_lines) == 2
    for line in log_lines:
        entry = json.loads(line)
        assert entry["requested_number"] == "301"
        assert entry["requested_json_fields"] == [
            "assignees",
            "body",
            "labels",
            "number",
            "title",
        ]
        assert entry["served_sha256"] == expected_sha256
        assert isinstance(entry["timestamp"], str) and entry["timestamp"]


def test_gh_fixture_known_hashes_match_oracle_known_hashes() -> None:
    """`fake-gh-issue-view.py`'s `_KNOWN_FIXTURE_HASHES` and `assert-issue-fix-decision.py`'s
    `_KNOWN_ISSUE_FIXTURES` keys must stay in sync (PR #266 review round 4, points 2/3)."""
    stub = load_module(
        "fake_gh_issue_view_fixture",
        "packages/meta-harness/scenarios/fixtures/fake-gh-issue-view.py",
    )
    oracle = _issue_fix_decision_oracle_fixture()

    assert set(stub._KNOWN_FIXTURE_HASHES) == set(oracle._KNOWN_ISSUE_FIXTURES.keys())


_EXPECTED_GH_CALL_LOG_JSON_FIELDS = ["assignees", "body", "labels", "number", "title"]


def _write_gh_call_log(
    tmp_path: Path, entries: list[dict], *, log_name: str = "gh-call-log.jsonl"
) -> None:
    (tmp_path / log_name).write_text(
        "\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8"
    )


def test_gh_call_log_oracle_accepts_correct_entry(tmp_path: Path) -> None:
    fixture = _issue_fix_decision_oracle_fixture()
    fixture_bytes = _real_issue_fixture_bytes("fix-greet-none-bug.yaml")
    (tmp_path / "gh-issue-fixture.json").write_bytes(fixture_bytes)
    served_sha256 = hashlib.sha256(fixture_bytes).hexdigest()
    _write_gh_call_log(
        tmp_path,
        [
            {
                "requested_number": "301",
                "requested_json_fields": _EXPECTED_GH_CALL_LOG_JSON_FIELDS,
                "served_sha256": served_sha256,
                "timestamp": "2026-07-18T00:00:00+00:00",
            }
        ],
    )

    fixture.assert_gh_call_logged(
        tmp_path, Path("gh-issue-fixture.json"), Path("gh-call-log.jsonl")
    )


def test_gh_call_log_oracle_rejects_missing_log(tmp_path: Path) -> None:
    fixture = _issue_fix_decision_oracle_fixture()
    (tmp_path / "gh-issue-fixture.json").write_bytes(
        _real_issue_fixture_bytes("fix-greet-none-bug.yaml")
    )

    with pytest.raises(AssertionError, match="missing regular gh call log"):
        fixture.assert_gh_call_logged(
            tmp_path, Path("gh-issue-fixture.json"), Path("gh-call-log.jsonl")
        )


def test_gh_call_log_oracle_rejects_wrong_requested_number(tmp_path: Path) -> None:
    fixture = _issue_fix_decision_oracle_fixture()
    fixture_bytes = _real_issue_fixture_bytes("fix-greet-none-bug.yaml")
    (tmp_path / "gh-issue-fixture.json").write_bytes(fixture_bytes)
    served_sha256 = hashlib.sha256(fixture_bytes).hexdigest()
    _write_gh_call_log(
        tmp_path,
        [
            {
                "requested_number": "999",
                "requested_json_fields": _EXPECTED_GH_CALL_LOG_JSON_FIELDS,
                "served_sha256": served_sha256,
                "timestamp": "2026-07-18T00:00:00+00:00",
            }
        ],
    )

    with pytest.raises(AssertionError, match="no gh call log entry"):
        fixture.assert_gh_call_logged(
            tmp_path, Path("gh-issue-fixture.json"), Path("gh-call-log.jsonl")
        )


def test_gh_call_log_oracle_rejects_wrong_served_hash(tmp_path: Path) -> None:
    fixture = _issue_fix_decision_oracle_fixture()
    (tmp_path / "gh-issue-fixture.json").write_bytes(
        _real_issue_fixture_bytes("fix-greet-none-bug.yaml")
    )
    _write_gh_call_log(
        tmp_path,
        [
            {
                "requested_number": "301",
                "requested_json_fields": _EXPECTED_GH_CALL_LOG_JSON_FIELDS,
                "served_sha256": "0" * 64,
                "timestamp": "2026-07-18T00:00:00+00:00",
            }
        ],
    )

    with pytest.raises(AssertionError, match="no gh call log entry"):
        fixture.assert_gh_call_logged(
            tmp_path, Path("gh-issue-fixture.json"), Path("gh-call-log.jsonl")
        )


def test_gh_call_log_oracle_rejects_wrong_json_fields(tmp_path: Path) -> None:
    fixture = _issue_fix_decision_oracle_fixture()
    fixture_bytes = _real_issue_fixture_bytes("fix-greet-none-bug.yaml")
    (tmp_path / "gh-issue-fixture.json").write_bytes(fixture_bytes)
    served_sha256 = hashlib.sha256(fixture_bytes).hexdigest()
    _write_gh_call_log(
        tmp_path,
        [
            {
                "requested_number": "301",
                "requested_json_fields": ["number", "title"],
                "served_sha256": served_sha256,
                "timestamp": "2026-07-18T00:00:00+00:00",
            }
        ],
    )

    with pytest.raises(AssertionError, match="no gh call log entry"):
        fixture.assert_gh_call_logged(
            tmp_path, Path("gh-issue-fixture.json"), Path("gh-call-log.jsonl")
        )


def _task_state_outcome_fixture():
    return load_module(
        "assert_task_state_outcome_fixture",
        "packages/meta-harness/scenarios/fixtures/assert-task-state-outcome.py",
    )


# The exact `.claude/Plans.md` content every task-state scenario's `setup:` step writes -- must
# stay byte-identical to `_CANONICAL_PLANS_FIXTURE` in assert-task-state-outcome.py and to the
# `setup:` heredocs in scenarios/skill/task-state/*.yaml (all three are cross-checked by
# `test_task_state_canonical_fixture_matches_real_scenario_setup` below).
_CANONICAL_PLANS_TEXT = (
    "---\n"
    "codd:\n"
    '  node_id: "plan:meta-harness-eval-fixture"\n'
    "  kind: plan\n"
    "  status: active\n"
    "  depends_on:\n"
    '    - id: "design:meta-harness"\n'
    "      relation: implements\n"
    "---\n"
    "\n"
    "# Plans\n"
    "\n"
    "## Project: meta-harness-eval-fixture\n"
    "\n"
    "### Phase 2: 実装 `cc:WIP`\n"
    "\n"
    "#### API\n"
    "\n"
    "- `cc:done` ユーザー認証API\n"
    "- `cc:WIP` 商品一覧API\n"
    "- `cc:TODO` 注文API\n"
    "\n"
    "---\n"
    "\n"
    "## Decisions\n"
    "\n"
    "- 2026-01-01: 初期設計方針を確定\n"
    "\n"
    "## Notes\n"
    "\n"
    "- 評価用フィクスチャ\n"
)


def _real_plans_seed_bytes(scenario_filename: str) -> bytes:
    """Extract the exact `.claude/Plans.md` bytes a real task-state scenario's `setup:` step
    writes, by actually executing that `setup:` script in an isolated tmp dir (same technique as
    `_real_issue_fixture_bytes`, for the same reason: no hand-transcribed copy that could drift)."""
    scenario_path = PACKAGE_DIR / "scenarios" / "skill" / "task-state" / scenario_filename
    doc = yaml.safe_load(scenario_path.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        for command in doc["setup"]:
            subprocess.run(command, shell=True, cwd=tmp_dir, capture_output=True)
        return (tmp_dir / ".claude" / "Plans.md").read_bytes()


def test_task_state_canonical_fixture_matches_real_scenario_setup() -> None:
    for scenario_filename in (
        "mark-task-done.yaml",
        "record-architecture-decision-holdout.yaml",
        "add-phase-with-agreed-ac.yaml",
        "add-phase-direct-call-confirms-ac-holdout.yaml",
    ):
        assert _real_plans_seed_bytes(scenario_filename) == _CANONICAL_PLANS_TEXT.encode("utf-8")


def test_task_state_mark_done_oracle_passes_on_correct_edit(tmp_path: Path) -> None:
    fixture = _task_state_outcome_fixture()
    plans_path = tmp_path / "Plans.md"
    plans_path.write_text(
        _CANONICAL_PLANS_TEXT.replace("`cc:WIP` 商品一覧API", "`cc:done` 商品一覧API"),
        encoding="utf-8",
    )

    fixture.assert_mark_task_done(plans_path, target_task="商品一覧API", target_status="done")


def test_task_state_mark_done_oracle_rejects_deleted_heading(tmp_path: Path) -> None:
    """A deleted `## Notes` heading shifts every subsequent line, which the whole-document line
    count check catches immediately (PR #266 review round 4, point 1)."""
    fixture = _task_state_outcome_fixture()
    text = _CANONICAL_PLANS_TEXT.replace("`cc:WIP` 商品一覧API", "`cc:done` 商品一覧API").replace(
        "## Notes\n\n", ""
    )
    plans_path = tmp_path / "Plans.md"
    plans_path.write_text(text, encoding="utf-8")

    with pytest.raises(AssertionError, match="same line count as the seeded fixture"):
        fixture.assert_mark_task_done(plans_path, target_task="商品一覧API", target_status="done")


def test_task_state_mark_done_oracle_rejects_extra_blank_line(tmp_path: Path) -> None:
    """An extraneous blank line anywhere in the document (not just inside a checked section)
    must also fail -- this was not reliably caught by the previous section-scoped checks."""
    fixture = _task_state_outcome_fixture()
    text = _CANONICAL_PLANS_TEXT.replace("`cc:WIP` 商品一覧API", "`cc:done` 商品一覧API").replace(
        "# Plans\n\n", "# Plans\n\n\n"
    )
    plans_path = tmp_path / "Plans.md"
    plans_path.write_text(text, encoding="utf-8")

    with pytest.raises(AssertionError, match="same line count as the seeded fixture"):
        fixture.assert_mark_task_done(plans_path, target_task="商品一覧API", target_status="done")


def test_task_state_mark_done_oracle_rejects_unrelated_task_edit(tmp_path: Path) -> None:
    fixture = _task_state_outcome_fixture()
    text = _CANONICAL_PLANS_TEXT.replace("`cc:WIP` 商品一覧API", "`cc:done` 商品一覧API").replace(
        "`cc:TODO` 注文API", "`cc:done` 注文API"
    )
    plans_path = tmp_path / "Plans.md"
    plans_path.write_text(text, encoding="utf-8")

    with pytest.raises(AssertionError, match="differ from the seeded fixture at exactly"):
        fixture.assert_mark_task_done(plans_path, target_task="商品一覧API", target_status="done")


def test_task_state_mark_done_oracle_rejects_modified_frontmatter(tmp_path: Path) -> None:
    fixture = _task_state_outcome_fixture()
    text = _CANONICAL_PLANS_TEXT.replace("`cc:WIP` 商品一覧API", "`cc:done` 商品一覧API").replace(
        "status: active", "status: draft"
    )
    plans_path = tmp_path / "Plans.md"
    plans_path.write_text(text, encoding="utf-8")

    with pytest.raises(AssertionError, match="differ from the seeded fixture at exactly"):
        fixture.assert_mark_task_done(plans_path, target_task="商品一覧API", target_status="done")


def test_task_state_mark_done_oracle_rejects_reordered_task_lines(tmp_path: Path) -> None:
    fixture = _task_state_outcome_fixture()
    text = _CANONICAL_PLANS_TEXT.replace("`cc:WIP` 商品一覧API", "`cc:done` 商品一覧API").replace(
        "- `cc:done` ユーザー認証API\n- `cc:done` 商品一覧API\n",
        "- `cc:done` 商品一覧API\n- `cc:done` ユーザー認証API\n",
    )
    plans_path = tmp_path / "Plans.md"
    plans_path.write_text(text, encoding="utf-8")

    with pytest.raises(AssertionError, match="differ from the seeded fixture at exactly"):
        fixture.assert_mark_task_done(plans_path, target_task="商品一覧API", target_status="done")


def test_task_state_record_decision_oracle_accepts_yesterday_today_and_tomorrow(
    tmp_path: Path,
) -> None:
    fixture = _task_state_outcome_fixture()
    for offset in (-1, 0, 1):
        date_str = (datetime.date.today() + datetime.timedelta(days=offset)).isoformat()
        text = _CANONICAL_PLANS_TEXT.replace(
            "- 2026-01-01: 初期設計方針を確定\n\n## Notes",
            f"- 2026-01-01: 初期設計方針を確定\n"
            f"- {date_str}: GraphQL を採用（理由: フロントエンドの柔軟性）\n\n## Notes",
        )
        plans_path = tmp_path / f"Plans-{offset}.md"
        plans_path.write_text(text, encoding="utf-8")

        fixture.assert_decision_recorded(
            plans_path, expected_decision="GraphQL を採用（理由: フロントエンドの柔軟性）"
        )


def test_task_state_record_decision_oracle_rejects_stale_date(tmp_path: Path) -> None:
    fixture = _task_state_outcome_fixture()
    text = _CANONICAL_PLANS_TEXT.replace(
        "- 2026-01-01: 初期設計方針を確定\n\n## Notes",
        "- 2026-01-01: 初期設計方針を確定\n"
        "- 2020-01-01: GraphQL を採用（理由: フロントエンドの柔軟性）\n\n## Notes",
    )
    plans_path = tmp_path / "Plans.md"
    plans_path.write_text(text, encoding="utf-8")

    with pytest.raises(AssertionError, match="not dated within"):
        fixture.assert_decision_recorded(
            plans_path, expected_decision="GraphQL を採用（理由: フロントエンドの柔軟性）"
        )


def test_task_state_record_decision_oracle_rejects_negated_decision_text(tmp_path: Path) -> None:
    """PR #266 review round 5, point 1: a substring-containment check would let a negated
    decision ("GraphQL は採用しない（理由: ...）") slip through as long as it happened to
    contain the same keywords. The oracle must require an exact match instead."""
    fixture = _task_state_outcome_fixture()
    today = datetime.date.today().isoformat()
    text = _CANONICAL_PLANS_TEXT.replace(
        "- 2026-01-01: 初期設計方針を確定\n\n## Notes",
        f"- 2026-01-01: 初期設計方針を確定\n"
        f"- {today}: GraphQL は採用しない（理由: フロントエンドの柔軟性はあるが学習コストが高い）\n\n## Notes",
    )
    plans_path = tmp_path / "Plans.md"
    plans_path.write_text(text, encoding="utf-8")

    with pytest.raises(AssertionError, match="does not exactly match the expected decision"):
        fixture.assert_decision_recorded(
            plans_path, expected_decision="GraphQL を採用（理由: フロントエンドの柔軟性）"
        )


def test_task_state_record_decision_oracle_rejects_partially_matching_decision_text(
    tmp_path: Path,
) -> None:
    """A decision line containing all the expected keywords but with extra/different trailing
    text must also fail -- not just a full negation."""
    fixture = _task_state_outcome_fixture()
    today = datetime.date.today().isoformat()
    text = _CANONICAL_PLANS_TEXT.replace(
        "- 2026-01-01: 初期設計方針を確定\n\n## Notes",
        f"- 2026-01-01: 初期設計方針を確定\n"
        f"- {today}: GraphQL を採用（理由: フロントエンドの柔軟性、ただし移行コストは要検討）\n\n## Notes",
    )
    plans_path = tmp_path / "Plans.md"
    plans_path.write_text(text, encoding="utf-8")

    with pytest.raises(AssertionError, match="does not exactly match the expected decision"):
        fixture.assert_decision_recorded(
            plans_path, expected_decision="GraphQL を採用（理由: フロントエンドの柔軟性）"
        )


def test_task_state_record_decision_oracle_rejects_entry_written_in_notes_section(
    tmp_path: Path,
) -> None:
    fixture = _task_state_outcome_fixture()
    today = datetime.date.today().isoformat()
    text = _CANONICAL_PLANS_TEXT.replace(
        "- 評価用フィクスチャ\n",
        f"- 評価用フィクスチャ\n- {today}: GraphQL を採用（理由: フロントエンドの柔軟性）\n",
    )
    plans_path = tmp_path / "Plans.md"
    plans_path.write_text(text, encoding="utf-8")

    with pytest.raises(AssertionError, match="content after the new Decisions entry"):
        fixture.assert_decision_recorded(
            plans_path, expected_decision="GraphQL を採用（理由: フロントエンドの柔軟性）"
        )


def test_task_state_record_decision_oracle_rejects_extraneous_decisions_entry(
    tmp_path: Path,
) -> None:
    fixture = _task_state_outcome_fixture()
    today = datetime.date.today().isoformat()
    text = _CANONICAL_PLANS_TEXT.replace(
        "- 2026-01-01: 初期設計方針を確定\n\n## Notes",
        f"- 2026-01-01: 初期設計方針を確定\n"
        f"- {today}: GraphQL を採用（理由: フロントエンドの柔軟性）\n"
        f"- {today}: 想定外の 2 件目の判断\n\n## Notes",
    )
    plans_path = tmp_path / "Plans.md"
    plans_path.write_text(text, encoding="utf-8")

    with pytest.raises(AssertionError, match="differ from the seeded fixture by exactly one"):
        fixture.assert_decision_recorded(
            plans_path, expected_decision="GraphQL を採用（理由: フロントエンドの柔軟性）"
        )


def test_task_state_record_decision_oracle_rejects_deleted_task(tmp_path: Path) -> None:
    fixture = _task_state_outcome_fixture()
    today = datetime.date.today().isoformat()
    text = _CANONICAL_PLANS_TEXT.replace(
        "- 2026-01-01: 初期設計方針を確定\n\n## Notes",
        f"- 2026-01-01: 初期設計方針を確定\n"
        f"- {today}: GraphQL を採用（理由: フロントエンドの柔軟性）\n\n## Notes",
    ).replace("- `cc:TODO` 注文API\n", "")
    plans_path = tmp_path / "Plans.md"
    plans_path.write_text(text, encoding="utf-8")

    with pytest.raises(AssertionError, match="differ from the seeded fixture by exactly one"):
        fixture.assert_decision_recorded(
            plans_path, expected_decision="GraphQL を採用（理由: フロントエンドの柔軟性）"
        )


def test_task_state_record_decision_oracle_rejects_reordered_task_lines(tmp_path: Path) -> None:
    fixture = _task_state_outcome_fixture()
    today = datetime.date.today().isoformat()
    text = _CANONICAL_PLANS_TEXT.replace(
        "- 2026-01-01: 初期設計方針を確定\n\n## Notes",
        f"- 2026-01-01: 初期設計方針を確定\n"
        f"- {today}: GraphQL を採用（理由: フロントエンドの柔軟性）\n\n## Notes",
    ).replace(
        "- `cc:done` ユーザー認証API\n- `cc:WIP` 商品一覧API\n",
        "- `cc:WIP` 商品一覧API\n- `cc:done` ユーザー認証API\n",
    )
    plans_path = tmp_path / "Plans.md"
    plans_path.write_text(text, encoding="utf-8")

    with pytest.raises(AssertionError, match="content before the new Decisions entry"):
        fixture.assert_decision_recorded(
            plans_path, expected_decision="GraphQL を採用（理由: フロントエンドの柔軟性）"
        )


def test_task_state_record_decision_oracle_rejects_modified_frontmatter(tmp_path: Path) -> None:
    fixture = _task_state_outcome_fixture()
    today = datetime.date.today().isoformat()
    text = _CANONICAL_PLANS_TEXT.replace(
        "- 2026-01-01: 初期設計方針を確定\n\n## Notes",
        f"- 2026-01-01: 初期設計方針を確定\n"
        f"- {today}: GraphQL を採用（理由: フロントエンドの柔軟性）\n\n## Notes",
    ).replace("status: active", "status: draft")
    plans_path = tmp_path / "Plans.md"
    plans_path.write_text(text, encoding="utf-8")

    with pytest.raises(AssertionError, match="content before the new Decisions entry"):
        fixture.assert_decision_recorded(
            plans_path, expected_decision="GraphQL を採用（理由: フロントエンドの柔軟性）"
        )


_ADD_PHASE_WITH_AC_BLOCK = (
    "### Phase 3: リリース準備 `cc:TODO`\n"
    "\n"
    "#### Acceptance Criteria\n"
    "\n"
    "- [ ] 主要エンドポイントが 200ms 以内に応答する — verify: `pytest tests/perf/test_latency.py`\n"
    "- [ ] リリースノートの記載内容が十分にユーザーへ伝わる — judge: 変更点・影響範囲・移行手順が明記されている\n"
    "\n"
    "#### Tasks\n"
    "\n"
    "- `cc:TODO` デプロイ手順書作成\n"
    "- `cc:TODO` リリースノート作成\n"
    "\n"
)

_ADD_PHASE_NO_AC_BLOCK = (
    "### Phase 3: リリース準備 `cc:TODO`\n"
    "\n"
    "#### Tasks\n"
    "\n"
    "- `cc:TODO` デプロイ手順書作成\n"
    "- `cc:TODO` リリースノート作成\n"
    "\n"
)


def _insert_before_separator(block: str) -> str:
    """Insert `block` right before the canonical fixture's project separator (the `---`
    immediately preceding `## Decisions`), matching where `add-phase` is expected to write."""
    return _CANONICAL_PLANS_TEXT.replace("\n---\n\n## Decisions", f"\n{block}---\n\n## Decisions")


def test_task_state_add_phase_with_ac_oracle_passes_on_correct_insertion(tmp_path: Path) -> None:
    fixture = _task_state_outcome_fixture()
    plans_path = tmp_path / "Plans.md"
    plans_path.write_text(_insert_before_separator(_ADD_PHASE_WITH_AC_BLOCK), encoding="utf-8")

    fixture.assert_add_phase_with_ac(
        plans_path,
        phase_name="Phase 3: リリース準備",
        tasks=["デプロイ手順書作成", "リリースノート作成"],
        verify_text="主要エンドポイントが 200ms 以内に応答する",
        verify_command="pytest tests/perf/test_latency.py",
        judge_text="リリースノートの記載内容が十分にユーザーへ伝わる",
        judge_criteria="変更点・影響範囲・移行手順が明記されている",
    )


def test_task_state_add_phase_with_ac_oracle_rejects_missing_ac_section(tmp_path: Path) -> None:
    """A direct add-phase call that silently dropped the agreed Acceptance Criteria must fail."""
    fixture = _task_state_outcome_fixture()
    plans_path = tmp_path / "Plans.md"
    plans_path.write_text(_insert_before_separator(_ADD_PHASE_NO_AC_BLOCK), encoding="utf-8")

    with pytest.raises(AssertionError, match="expected exactly one matching canonical line"):
        fixture.assert_add_phase_with_ac(
            plans_path,
            phase_name="Phase 3: リリース準備",
            tasks=["デプロイ手順書作成", "リリースノート作成"],
            verify_text="主要エンドポイントが 200ms 以内に応答する",
            verify_command="pytest tests/perf/test_latency.py",
            judge_text="リリースノートの記載内容が十分にユーザーへ伝わる",
            judge_criteria="変更点・影響範囲・移行手順が明記されている",
        )


def test_task_state_add_phase_with_ac_oracle_rejects_mismatched_verify_command(
    tmp_path: Path,
) -> None:
    fixture = _task_state_outcome_fixture()
    tampered_block = _ADD_PHASE_WITH_AC_BLOCK.replace(
        "pytest tests/perf/test_latency.py", "pytest tests/perf/test_other.py"
    )
    plans_path = tmp_path / "Plans.md"
    plans_path.write_text(_insert_before_separator(tampered_block), encoding="utf-8")

    with pytest.raises(AssertionError, match="verify Acceptance Criteria item does not match"):
        fixture.assert_add_phase_with_ac(
            plans_path,
            phase_name="Phase 3: リリース準備",
            tasks=["デプロイ手順書作成", "リリースノート作成"],
            verify_text="主要エンドポイントが 200ms 以内に応答する",
            verify_command="pytest tests/perf/test_latency.py",
            judge_text="リリースノートの記載内容が十分にユーザーへ伝わる",
            judge_criteria="変更点・影響範囲・移行手順が明記されている",
        )


def test_task_state_add_phase_with_ac_oracle_rejects_collateral_edit_to_existing_phase(
    tmp_path: Path,
) -> None:
    fixture = _task_state_outcome_fixture()
    text = _insert_before_separator(_ADD_PHASE_WITH_AC_BLOCK).replace(
        "`cc:WIP` 商品一覧API", "`cc:done` 商品一覧API"
    )
    plans_path = tmp_path / "Plans.md"
    plans_path.write_text(text, encoding="utf-8")

    with pytest.raises(AssertionError, match="content before the new phase's insertion point"):
        fixture.assert_add_phase_with_ac(
            plans_path,
            phase_name="Phase 3: リリース準備",
            tasks=["デプロイ手順書作成", "リリースノート作成"],
            verify_text="主要エンドポイントが 200ms 以内に応答する",
            verify_command="pytest tests/perf/test_latency.py",
            judge_text="リリースノートの記載内容が十分にユーザーへ伝わる",
            judge_criteria="変更点・影響範囲・移行手順が明記されている",
        )


def test_task_state_add_phase_no_ac_oracle_passes_on_correct_insertion(tmp_path: Path) -> None:
    fixture = _task_state_outcome_fixture()
    plans_path = tmp_path / "Plans.md"
    plans_path.write_text(_insert_before_separator(_ADD_PHASE_NO_AC_BLOCK), encoding="utf-8")

    fixture.assert_add_phase_no_ac(
        plans_path,
        phase_name="Phase 3: リリース準備",
        tasks=["デプロイ手順書作成", "リリースノート作成"],
    )


def test_task_state_add_phase_no_ac_oracle_rejects_fabricated_ac_section(tmp_path: Path) -> None:
    """A direct add-phase call must not invent an Acceptance Criteria section on its own."""
    fixture = _task_state_outcome_fixture()
    plans_path = tmp_path / "Plans.md"
    plans_path.write_text(_insert_before_separator(_ADD_PHASE_WITH_AC_BLOCK), encoding="utf-8")

    with pytest.raises(AssertionError, match="must not add an"):
        fixture.assert_add_phase_no_ac(
            plans_path,
            phase_name="Phase 3: リリース準備",
            tasks=["デプロイ手順書作成", "リリースノート作成"],
        )


def test_task_state_add_phase_no_ac_oracle_rejects_deleted_task(tmp_path: Path) -> None:
    fixture = _task_state_outcome_fixture()
    tampered_block = _ADD_PHASE_NO_AC_BLOCK.replace("- `cc:TODO` リリースノート作成\n", "")
    plans_path = tmp_path / "Plans.md"
    plans_path.write_text(_insert_before_separator(tampered_block), encoding="utf-8")

    with pytest.raises(AssertionError, match="new phase tasks do not match"):
        fixture.assert_add_phase_no_ac(
            plans_path,
            phase_name="Phase 3: リリース準備",
            tasks=["デプロイ手順書作成", "リリースノート作成"],
        )


def _init_git_repo_with_tracked_file(repo_dir: Path, relative_path: str, content: str) -> None:
    tracked_path = repo_dir / relative_path
    tracked_path.parent.mkdir(parents=True, exist_ok=True)
    tracked_path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo_dir, check=True)
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "baseline"], cwd=repo_dir, check=True)


def test_every_bypass_permissions_scenario_has_a_collateral_scope_critical_check() -> None:
    """PR #273 final review (Medium, security+spec reviewers independently flagged this):
    `permission_mode: bypassPermissions` unlocks the entire `.claude/` tree for a scenario, so
    every scenario that opts into it must carry a `collateral-scope` critical check as a
    compensating control (ADR-20260714-038 "bypassPermissions 例外" addendum). This scans all
    registered scenario yaml files rather than hardcoding the current 6 bypass scenarios, so a
    future scenario adding bypass without the paired oracle fails this test immediately."""
    scenario_paths = sorted((PACKAGE_DIR / "scenarios").rglob("*.yaml"))
    assert scenario_paths, "expected to find at least one scenario yaml file"

    bypass_scenarios_without_collateral_scope = []
    bypass_scenario_count = 0
    for path in scenario_paths:
        scenario = yaml.safe_load(path.read_text(encoding="utf-8"))
        if scenario.get("permission_mode") != "bypassPermissions":
            continue
        bypass_scenario_count += 1
        critical_commands = [item.get("command", "") for item in scenario.get("critical", [])]
        if not any("collateral-scope" in command for command in critical_commands):
            bypass_scenarios_without_collateral_scope.append(path.relative_to(PACKAGE_DIR))

    # Sanity check: this test must actually exercise at least one bypass scenario, or the
    # assertion below would vacuously pass even if the enforcement logic were broken.
    assert bypass_scenario_count >= 1, (
        "expected at least one bypassPermissions scenario to exist (task-state/handoff suites)"
    )
    assert not bypass_scenarios_without_collateral_scope, (
        "bypassPermissions scenarios missing a collateral-scope critical check: "
        f"{bypass_scenarios_without_collateral_scope}"
    )


class TestCollateralScopeOracle:
    """Issue #261 PR6 bot review follow-up: `bypassPermissions` unlocks the entire `.claude/`
    tree for task-state scenarios, so this oracle guards against collateral damage outside the
    single expected target file."""

    @pytest.fixture(autouse=True)
    def _isolate_git_config(self, monkeypatch: pytest.MonkeyPatch, tmp_path_factory) -> None:
        # Match the production oracle container's git isolation (`scenario_docker_profile.
        # build_oracle_command`, which also sets `HOME` to a fresh tmpfs dir): without also
        # overriding `HOME`, a developer machine's own default `~/.config/git/ignore` excludes
        # file (e.g. a global `**/.claude/settings.local.json` rule) resolves via `$HOME` and
        # can silently hide files this test writes from `git status`, causing false negatives.
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("collateral-scope-home")))

    def test_passes_when_only_allowed_file_changed(self, tmp_path: Path) -> None:
        fixture = _task_state_outcome_fixture()
        _init_git_repo_with_tracked_file(tmp_path, ".claude/Plans.md", "before\n")
        (tmp_path / ".claude" / "Plans.md").write_text("after\n", encoding="utf-8")

        fixture.assert_tracked_changes_limited_to({".claude/Plans.md"}, cwd=tmp_path)

    def test_passes_with_no_changes_at_all(self, tmp_path: Path) -> None:
        fixture = _task_state_outcome_fixture()
        _init_git_repo_with_tracked_file(tmp_path, ".claude/Plans.md", "unchanged\n")

        fixture.assert_tracked_changes_limited_to({".claude/Plans.md"}, cwd=tmp_path)

    def test_rejects_change_to_unrelated_tracked_file(self, tmp_path: Path) -> None:
        """A tracked file elsewhere under `.claude/` (e.g. `settings.json`) unlocked by
        `bypassPermissions` must fail the run even though `Plans.md` itself is untouched."""
        fixture = _task_state_outcome_fixture()
        _init_git_repo_with_tracked_file(tmp_path, ".claude/Plans.md", "unchanged\n")
        settings_path = tmp_path / ".claude" / "settings.json"
        settings_path.write_text('{"hooks": {}}\n', encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)

        with pytest.raises(AssertionError, match="tracked files changed outside the allowed scope"):
            fixture.assert_tracked_changes_limited_to({".claude/Plans.md"}, cwd=tmp_path)

    def test_gitignored_hook_side_effects_do_not_trip_the_check(self, tmp_path: Path) -> None:
        """PR #273 bot review round 3: the untracked scan is now unconditional (no more
        `allowed_new_prefixes`-empty early return), so this must be proven via an actual
        `.gitignore` mirroring the real repo -- `git status` never reports ignored paths as
        `??` regardless of `--untracked-files=all`, so hook-generated session state
        (`.claude/context/...`) and `__pycache__` stay invisible to this check exactly as in
        production (verified against the repo's real `.gitignore`, see the calling scenario
        yaml's comment for the citation)."""
        fixture = _task_state_outcome_fixture()
        _init_git_repo_with_tracked_file(tmp_path, ".claude/Plans.md", "unchanged\n")
        (tmp_path / ".gitignore").write_text(".claude/context/\n__pycache__/\n", encoding="utf-8")
        subprocess.run(["git", "add", ".gitignore"], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add gitignore"], cwd=tmp_path, check=True)

        context_dir = tmp_path / ".claude" / "context" / "session"
        context_dir.mkdir(parents=True)
        (context_dir / "entry.json").write_text("{}\n", encoding="utf-8")
        pycache_dir = tmp_path / "packages" / "meta-harness" / "__pycache__"
        pycache_dir.mkdir(parents=True)
        (pycache_dir / "module.cpython-314.pyc").write_bytes(b"\x00")

        fixture.assert_tracked_changes_limited_to({".claude/Plans.md"}, cwd=tmp_path)

    def test_default_rejects_new_untracked_file_without_allow_list(self, tmp_path: Path) -> None:
        """PR #273 bot review round 3 (Codex P2): a bypass-mode candidate creating an
        un-gitignored file (e.g. `.claude/settings.local.json`, a real permission-escalation
        vector) must be caught even when the caller passes no `allowed_new_prefixes` at all --
        the previous early-return skip made this a silent pass."""
        fixture = _task_state_outcome_fixture()
        _init_git_repo_with_tracked_file(tmp_path, ".claude/Plans.md", "unchanged\n")
        (tmp_path / ".claude" / "settings.local.json").write_text("{}\n", encoding="utf-8")

        with pytest.raises(AssertionError, match="new untracked files outside the allowed scope"):
            fixture.assert_tracked_changes_limited_to({".claude/Plans.md"}, cwd=tmp_path)

    def test_rejects_deleted_tracked_file(self, tmp_path: Path) -> None:
        fixture = _task_state_outcome_fixture()
        _init_git_repo_with_tracked_file(tmp_path, ".claude/Plans.md", "unchanged\n")
        (tmp_path / ".claude" / "extra.md").write_text("tracked\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add extra"], cwd=tmp_path, check=True)
        (tmp_path / ".claude" / "extra.md").unlink()

        with pytest.raises(AssertionError, match="tracked files changed outside the allowed scope"):
            fixture.assert_tracked_changes_limited_to({".claude/Plans.md"}, cwd=tmp_path)

    def test_allowed_new_prefix_permits_new_file_under_it(self, tmp_path: Path) -> None:
        """`create-handoff` case: a brand-new `.claude/handoffs/{timestamp}.md` must be
        permitted when its prefix is explicitly allow-listed."""
        fixture = _task_state_outcome_fixture()
        _init_git_repo_with_tracked_file(tmp_path, ".claude/Plans.md", "unchanged\n")
        handoffs_dir = tmp_path / ".claude" / "handoffs"
        handoffs_dir.mkdir()
        (handoffs_dir / "20260719-000000.md").write_text("# Task Handoff\n", encoding="utf-8")

        fixture.assert_tracked_changes_limited_to(
            {".claude/Plans.md"}, allowed_new_prefixes=(".claude/handoffs/",), cwd=tmp_path
        )

    def test_allowed_new_prefix_rejects_new_file_elsewhere(self, tmp_path: Path) -> None:
        fixture = _task_state_outcome_fixture()
        _init_git_repo_with_tracked_file(tmp_path, ".claude/Plans.md", "unchanged\n")
        (tmp_path / ".claude" / "settings.local.json").write_text("{}\n", encoding="utf-8")

        with pytest.raises(AssertionError, match="new untracked files outside the allowed scope"):
            fixture.assert_tracked_changes_limited_to(
                {".claude/Plans.md"}, allowed_new_prefixes=(".claude/handoffs/",), cwd=tmp_path
            )

    def test_space_containing_path_is_parsed_correctly(self, tmp_path: Path) -> None:
        """PR #273 bot review round 3 (CodeRabbit): plain `git status --porcelain` /
        `git diff --name-only` quote paths with unusual characters (including spaces), which
        broke the previous newline+slice parsing. `-z` must correctly detect both a legitimate
        allowed new file with a space in its name, and an unrelated disallowed one."""
        fixture = _task_state_outcome_fixture()
        _init_git_repo_with_tracked_file(tmp_path, ".claude/Plans.md", "unchanged\n")
        handoffs_dir = tmp_path / ".claude" / "handoffs"
        handoffs_dir.mkdir()
        (handoffs_dir / "2026 07 19 000000.md").write_text("# Task Handoff\n", encoding="utf-8")

        # Allowed: the space-containing new file lives under the allowed prefix.
        fixture.assert_tracked_changes_limited_to(
            {".claude/Plans.md"}, allowed_new_prefixes=(".claude/handoffs/",), cwd=tmp_path
        )

        # Disallowed: a second space-containing file outside any allowed scope must still be
        # caught (proves the parser did not desync after the first entry).
        (tmp_path / ".claude" / "extra notes.md").write_text("x\n", encoding="utf-8")
        with pytest.raises(AssertionError, match=r"extra notes\.md"):
            fixture.assert_tracked_changes_limited_to(
                {".claude/Plans.md"}, allowed_new_prefixes=(".claude/handoffs/",), cwd=tmp_path
            )

    def test_staged_new_file_under_allowed_prefix_is_permitted(self, tmp_path: Path) -> None:
        """PR #273 bot review round 4 (Codex P2): `git diff --name-status` reports a
        candidate-staged brand-new file (`git add`-ed but not committed) as status `A`. Such a
        file must be permitted when it falls under `allowed_new_prefixes`, the same as an
        unstaged/untracked one -- staging it should not turn a legitimate new handoff file into
        a failure."""
        fixture = _task_state_outcome_fixture()
        _init_git_repo_with_tracked_file(tmp_path, ".claude/Plans.md", "unchanged\n")
        handoffs_dir = tmp_path / ".claude" / "handoffs"
        handoffs_dir.mkdir()
        (handoffs_dir / "20260719-000000.md").write_text("# Task Handoff\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", ".claude/handoffs/20260719-000000.md"], cwd=tmp_path, check=True
        )

        fixture.assert_tracked_changes_limited_to(
            {".claude/Plans.md"}, allowed_new_prefixes=(".claude/handoffs/",), cwd=tmp_path
        )

    def test_staged_new_file_outside_allowed_prefix_is_rejected(self, tmp_path: Path) -> None:
        """The staged-addition allowance must still respect `allowed_new_prefixes` scoping --
        staging a file elsewhere (e.g. `.claude/settings.local.json`) must not be laundered
        into a pass just by running `git add` on it."""
        fixture = _task_state_outcome_fixture()
        _init_git_repo_with_tracked_file(tmp_path, ".claude/Plans.md", "unchanged\n")
        settings_path = tmp_path / ".claude" / "settings.local.json"
        settings_path.write_text("{}\n", encoding="utf-8")
        subprocess.run(["git", "add", ".claude/settings.local.json"], cwd=tmp_path, check=True)

        with pytest.raises(AssertionError, match="tracked files changed outside the allowed scope"):
            fixture.assert_tracked_changes_limited_to(
                {".claude/Plans.md"}, allowed_new_prefixes=(".claude/handoffs/",), cwd=tmp_path
            )

    def test_staged_modification_of_existing_tracked_file_is_still_rejected(
        self, tmp_path: Path
    ) -> None:
        """A staged *modification* to an already-tracked file (status `M`, not `A`) must never
        be excused by `allowed_new_prefixes`, even if its path happens to start with an allowed
        prefix -- only a brand-new addition qualifies."""
        fixture = _task_state_outcome_fixture()
        _init_git_repo_with_tracked_file(tmp_path, ".claude/Plans.md", "unchanged\n")
        handoffs_dir = tmp_path / ".claude" / "handoffs"
        handoffs_dir.mkdir()
        existing = handoffs_dir / "already-tracked.md"
        existing.write_text("original\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", ".claude/handoffs/already-tracked.md"], cwd=tmp_path, check=True
        )
        subprocess.run(["git", "commit", "-q", "-m", "seed handoff file"], cwd=tmp_path, check=True)
        existing.write_text("tampered\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", ".claude/handoffs/already-tracked.md"], cwd=tmp_path, check=True
        )

        with pytest.raises(AssertionError, match="tracked files changed outside the allowed scope"):
            fixture.assert_tracked_changes_limited_to(
                {".claude/Plans.md"}, allowed_new_prefixes=(".claude/handoffs/",), cwd=tmp_path
            )

    def test_symlink_under_allowed_prefix_is_rejected(self, tmp_path: Path) -> None:
        """PR #273 final review (security High): `ln -s <existing file>
        .claude/handoffs/x.md` puts a path-legitimate symlink under an allowed prefix. A
        path-only check would pass it, but a downstream content oracle following the link
        could read whatever pre-existing file it points at. The symlink must be rejected
        regardless of its path."""
        fixture = _task_state_outcome_fixture()
        _init_git_repo_with_tracked_file(tmp_path, ".claude/Plans.md", "unchanged\n")
        target = tmp_path / "sandbox" / "existing-with-keyword.md"
        target.parent.mkdir(parents=True)
        target.write_text("Current Task State\nNext Up\npytest\n", encoding="utf-8")
        handoffs_dir = tmp_path / ".claude" / "handoffs"
        handoffs_dir.mkdir()
        (handoffs_dir / "20260719-000000.md").symlink_to(target)

        with pytest.raises(AssertionError, match="new untracked files outside the allowed scope"):
            fixture.assert_tracked_changes_limited_to(
                {".claude/Plans.md"}, allowed_new_prefixes=(".claude/handoffs/",), cwd=tmp_path
            )

    def test_regular_file_under_allowed_prefix_still_passes(self, tmp_path: Path) -> None:
        """Sanity check alongside the symlink rejection test: a normal (non-symlink) new file
        under an allowed prefix must keep passing -- the symlink check must not become an
        overly broad false positive."""
        fixture = _task_state_outcome_fixture()
        _init_git_repo_with_tracked_file(tmp_path, ".claude/Plans.md", "unchanged\n")
        handoffs_dir = tmp_path / ".claude" / "handoffs"
        handoffs_dir.mkdir()
        (handoffs_dir / "20260719-000000.md").write_text("# Task Handoff\n", encoding="utf-8")

        fixture.assert_tracked_changes_limited_to(
            {".claude/Plans.md"}, allowed_new_prefixes=(".claude/handoffs/",), cwd=tmp_path
        )

    def test_rename_of_existing_tracked_file_is_rejected_by_both_paths(
        self, tmp_path: Path
    ) -> None:
        """PR #273 bot review round 4: a detected rename (`git diff --name-status` status
        `R###`) reports two paths in one -z record (old, then new). Both must be checked
        against `allowed_paths` -- a rename is never treated as a fresh `A` addition, so
        `allowed_new_prefixes` must not excuse the new path even if it happens to fall under
        one, and the vacated old path must also be flagged."""
        fixture = _task_state_outcome_fixture()
        _init_git_repo_with_tracked_file(tmp_path, ".claude/Plans.md", "unchanged\n")
        source = tmp_path / "sandbox" / "oldname.md"
        source.parent.mkdir(parents=True)
        source.write_text("line1\nline2\nline3\nline4\nline5\n", encoding="utf-8")
        subprocess.run(["git", "add", "sandbox/oldname.md"], cwd=tmp_path, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "seed renameable file"], cwd=tmp_path, check=True
        )
        (tmp_path / ".claude" / "handoffs").mkdir()
        subprocess.run(
            ["git", "mv", "sandbox/oldname.md", ".claude/handoffs/renamed.md"],
            cwd=tmp_path,
            check=True,
        )

        with pytest.raises(AssertionError, match="tracked files changed outside the allowed scope"):
            fixture.assert_tracked_changes_limited_to(
                {".claude/Plans.md"}, allowed_new_prefixes=(".claude/handoffs/",), cwd=tmp_path
            )

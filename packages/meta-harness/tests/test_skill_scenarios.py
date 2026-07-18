"""実 skill scenario suite の所有権・train/holdout・実行 envelope テスト。"""

from __future__ import annotations

import datetime
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
    for target in (
        "skill:handoff",
        "skill:issue-create",
        "skill:codex-system",
        "skill:antigravity-system",
        "skill:issue-fix",
        "skill:task-state",
    ):
        paths = ev.validate_target_suite(PACKAGE_DIR, SCHEMA_DIR, target)
        scenarios = [ev.load_scenario(path, SCHEMA_DIR) for path in paths]

        assert len(scenarios) == 2
        assert sum(not scenario["holdout"] for scenario in scenarios) == 1
        assert sum(scenario["holdout"] for scenario in scenarios) == 1
        assert {scenario["target"] for scenario in scenarios} == {target}


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
    (meta_dir / "gh-issue-fixture.json").write_text(
        json.dumps({"number": 301, "title": "bug", "labels": [{"name": "bug"}]}),
        encoding="utf-8",
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
    payload = {
        "number": 301,
        "title": "bug",
        "body": "body",
        "labels": [{"name": "bug"}],
        "assignees": [],
    }
    (meta_dir / "gh-issue-fixture.json").write_text(json.dumps(payload), encoding="utf-8")

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


def _task_state_outcome_fixture():
    return load_module(
        "assert_task_state_outcome_fixture",
        "packages/meta-harness/scenarios/fixtures/assert-task-state-outcome.py",
    )


_DEFAULT_TASKS: list[tuple[str, str]] = [
    ("done", "ユーザー認証API"),
    ("WIP", "商品一覧API"),
    ("TODO", "注文API"),
]


def _plans_text(
    *,
    tasks: list[tuple[str, str]] | None = None,
    decisions: list[str] | None = None,
    notes: list[str] | None = None,
    include_notes_section: bool = True,
    codd_status: str = "active",
) -> str:
    """Build a Plans.md variant. `tasks`/`decisions`/`notes` default to the exact seeded fixture
    content (see `assert-task-state-outcome.py`'s `_CANONICAL_PLANS_FIXTURE`); tests override one
    dimension at a time to simulate a specific tampering scenario."""
    tasks = tasks if tasks is not None else list(_DEFAULT_TASKS)
    decisions = decisions if decisions is not None else ["2026-01-01: 初期設計方針を確定"]
    notes = notes if notes is not None else ["評価用フィクスチャ"]
    lines = [
        "---",
        "codd:",
        '  node_id: "plan:meta-harness-eval-fixture"',
        "  kind: plan",
        f"  status: {codd_status}",
        "  depends_on:",
        '    - id: "design:meta-harness"',
        "      relation: implements",
        "---",
        "",
        "# Plans",
        "",
        "## Project: meta-harness-eval-fixture",
        "",
        "### Phase 2: 実装 `cc:WIP`",
        "",
        "#### API",
        "",
        *[f"- `cc:{status}` {name}" for status, name in tasks],
        "",
        "---",
        "",
        "## Decisions",
        "",
        *[f"- {entry}" for entry in decisions],
        "",
    ]
    if include_notes_section:
        lines += ["## Notes", "", *[f"- {entry}" for entry in notes], ""]
    return "\n".join(lines)


def test_task_state_mark_done_oracle_passes_on_correct_edit(tmp_path: Path) -> None:
    fixture = _task_state_outcome_fixture()
    expected_tasks = [("done", "ユーザー認証API"), ("done", "商品一覧API"), ("TODO", "注文API")]
    plans_path = tmp_path / "Plans.md"
    plans_path.write_text(_plans_text(tasks=expected_tasks), encoding="utf-8")

    fixture.assert_mark_task_done(plans_path, expected_tasks=expected_tasks)


def test_task_state_mark_done_oracle_rejects_deleted_notes_section(tmp_path: Path) -> None:
    fixture = _task_state_outcome_fixture()
    expected_tasks = [("done", "ユーザー認証API"), ("done", "商品一覧API"), ("TODO", "注文API")]
    plans_path = tmp_path / "Plans.md"
    plans_path.write_text(
        _plans_text(tasks=expected_tasks, include_notes_section=False), encoding="utf-8"
    )

    with pytest.raises(AssertionError, match="missing '## Notes' section"):
        fixture.assert_mark_task_done(plans_path, expected_tasks=expected_tasks)


def test_task_state_mark_done_oracle_rejects_deleted_decisions_entry(tmp_path: Path) -> None:
    fixture = _task_state_outcome_fixture()
    expected_tasks = [("done", "ユーザー認証API"), ("done", "商品一覧API"), ("TODO", "注文API")]
    plans_path = tmp_path / "Plans.md"
    plans_path.write_text(_plans_text(tasks=expected_tasks, decisions=[]), encoding="utf-8")

    with pytest.raises(AssertionError, match="Decisions section changed"):
        fixture.assert_mark_task_done(plans_path, expected_tasks=expected_tasks)


def test_task_state_mark_done_oracle_rejects_extraneous_decisions_entry(tmp_path: Path) -> None:
    """PR #266 review round 3, point 5: an *added* extra entry must fail too, not just a
    deletion -- `mark-task-done` mode must never touch Decisions at all."""
    fixture = _task_state_outcome_fixture()
    expected_tasks = [("done", "ユーザー認証API"), ("done", "商品一覧API"), ("TODO", "注文API")]
    plans_path = tmp_path / "Plans.md"
    plans_path.write_text(
        _plans_text(
            tasks=expected_tasks,
            decisions=["2026-01-01: 初期設計方針を確定", "2026-02-01: 想定外の追加判断"],
        ),
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match="Decisions section changed"):
        fixture.assert_mark_task_done(plans_path, expected_tasks=expected_tasks)


def test_task_state_mark_done_oracle_rejects_modified_frontmatter(tmp_path: Path) -> None:
    fixture = _task_state_outcome_fixture()
    expected_tasks = [("done", "ユーザー認証API"), ("done", "商品一覧API"), ("TODO", "注文API")]
    plans_path = tmp_path / "Plans.md"
    plans_path.write_text(_plans_text(tasks=expected_tasks, codd_status="draft"), encoding="utf-8")

    with pytest.raises(AssertionError, match="CODD frontmatter block was modified"):
        fixture.assert_mark_task_done(plans_path, expected_tasks=expected_tasks)


def test_task_state_mark_done_oracle_rejects_duplicated_task_line(tmp_path: Path) -> None:
    fixture = _task_state_outcome_fixture()
    expected_tasks = [("done", "ユーザー認証API"), ("done", "商品一覧API"), ("TODO", "注文API")]
    actual_tasks = [
        ("done", "ユーザー認証API"),
        ("done", "商品一覧API"),
        ("done", "商品一覧API"),
        ("TODO", "注文API"),
    ]
    plans_path = tmp_path / "Plans.md"
    plans_path.write_text(_plans_text(tasks=actual_tasks), encoding="utf-8")

    with pytest.raises(AssertionError, match="task lines do not exactly match"):
        fixture.assert_mark_task_done(plans_path, expected_tasks=expected_tasks)


def test_task_state_mark_done_oracle_rejects_reordered_task_lines(tmp_path: Path) -> None:
    """PR #266 review round 3, point 3: reordering (with no duplication/omission) must also
    fail -- a sorted-set comparison would have missed this."""
    fixture = _task_state_outcome_fixture()
    expected_tasks = [("done", "ユーザー認証API"), ("done", "商品一覧API"), ("TODO", "注文API")]
    reordered_tasks = [("done", "商品一覧API"), ("done", "ユーザー認証API"), ("TODO", "注文API")]
    plans_path = tmp_path / "Plans.md"
    plans_path.write_text(_plans_text(tasks=reordered_tasks), encoding="utf-8")

    with pytest.raises(AssertionError, match="task lines do not exactly match"):
        fixture.assert_mark_task_done(plans_path, expected_tasks=expected_tasks)


def test_task_state_record_decision_oracle_accepts_yesterday_today_and_tomorrow(
    tmp_path: Path,
) -> None:
    fixture = _task_state_outcome_fixture()
    for offset in (-1, 0, 1):
        date_str = (datetime.date.today() + datetime.timedelta(days=offset)).isoformat()
        plans_path = tmp_path / f"Plans-{offset}.md"
        plans_path.write_text(
            _plans_text(
                decisions=[
                    "2026-01-01: 初期設計方針を確定",
                    f"{date_str}: GraphQL を採用（理由: フロントエンドの柔軟性）",
                ]
            ),
            encoding="utf-8",
        )

        fixture.assert_decision_recorded(
            plans_path,
            decision_substrings=["GraphQL", "フロントエンドの柔軟性"],
            expected_tasks=_DEFAULT_TASKS,
        )


def test_task_state_record_decision_oracle_rejects_stale_date(tmp_path: Path) -> None:
    fixture = _task_state_outcome_fixture()
    plans_path = tmp_path / "Plans.md"
    plans_path.write_text(
        _plans_text(
            decisions=[
                "2026-01-01: 初期設計方針を確定",
                "2020-01-01: GraphQL を採用（理由: フロントエンドの柔軟性）",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match="new Decisions entry is not dated within"):
        fixture.assert_decision_recorded(
            plans_path,
            decision_substrings=["GraphQL", "フロントエンドの柔軟性"],
            expected_tasks=_DEFAULT_TASKS,
        )


def test_task_state_record_decision_oracle_rejects_entry_written_in_notes_section(
    tmp_path: Path,
) -> None:
    fixture = _task_state_outcome_fixture()
    today = datetime.date.today().isoformat()
    plans_path = tmp_path / "Plans.md"
    plans_path.write_text(
        _plans_text(
            decisions=["2026-01-01: 初期設計方針を確定"],
            notes=[
                "評価用フィクスチャ",
                f"{today}: GraphQL を採用（理由: フロントエンドの柔軟性）",
            ],
        ),
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match="exactly the seeded entries plus exactly one"):
        fixture.assert_decision_recorded(
            plans_path,
            decision_substrings=["GraphQL", "フロントエンドの柔軟性"],
            expected_tasks=_DEFAULT_TASKS,
        )


def test_task_state_record_decision_oracle_rejects_extraneous_decisions_entry(
    tmp_path: Path,
) -> None:
    """PR #266 review round 3, point 5: two new entries (not exactly one) must fail."""
    fixture = _task_state_outcome_fixture()
    today = datetime.date.today().isoformat()
    plans_path = tmp_path / "Plans.md"
    plans_path.write_text(
        _plans_text(
            decisions=[
                "2026-01-01: 初期設計方針を確定",
                f"{today}: GraphQL を採用（理由: フロントエンドの柔軟性）",
                f"{today}: 想定外の 2 件目の判断",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match="exactly the seeded entries plus exactly one"):
        fixture.assert_decision_recorded(
            plans_path,
            decision_substrings=["GraphQL", "フロントエンドの柔軟性"],
            expected_tasks=_DEFAULT_TASKS,
        )


def test_task_state_record_decision_oracle_rejects_deleted_task(tmp_path: Path) -> None:
    fixture = _task_state_outcome_fixture()
    today = datetime.date.today().isoformat()
    plans_path = tmp_path / "Plans.md"
    plans_path.write_text(
        _plans_text(
            tasks=[("done", "ユーザー認証API"), ("WIP", "商品一覧API")],
            decisions=[
                "2026-01-01: 初期設計方針を確定",
                f"{today}: GraphQL を採用（理由: フロントエンドの柔軟性）",
            ],
        ),
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match="task lines do not exactly match"):
        fixture.assert_decision_recorded(
            plans_path,
            decision_substrings=["GraphQL", "フロントエンドの柔軟性"],
            expected_tasks=_DEFAULT_TASKS,
        )


def test_task_state_record_decision_oracle_rejects_duplicated_task_line(tmp_path: Path) -> None:
    fixture = _task_state_outcome_fixture()
    today = datetime.date.today().isoformat()
    plans_path = tmp_path / "Plans.md"
    plans_path.write_text(
        _plans_text(
            tasks=[
                ("done", "ユーザー認証API"),
                ("WIP", "商品一覧API"),
                ("WIP", "商品一覧API"),
                ("TODO", "注文API"),
            ],
            decisions=[
                "2026-01-01: 初期設計方針を確定",
                f"{today}: GraphQL を採用（理由: フロントエンドの柔軟性）",
            ],
        ),
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match="task lines do not exactly match"):
        fixture.assert_decision_recorded(
            plans_path,
            decision_substrings=["GraphQL", "フロントエンドの柔軟性"],
            expected_tasks=_DEFAULT_TASKS,
        )


def test_task_state_record_decision_oracle_rejects_reordered_task_lines(tmp_path: Path) -> None:
    """PR #266 review round 3, point 3: reordering must fail in decision mode too."""
    fixture = _task_state_outcome_fixture()
    today = datetime.date.today().isoformat()
    plans_path = tmp_path / "Plans.md"
    plans_path.write_text(
        _plans_text(
            tasks=[("WIP", "商品一覧API"), ("done", "ユーザー認証API"), ("TODO", "注文API")],
            decisions=[
                "2026-01-01: 初期設計方針を確定",
                f"{today}: GraphQL を採用（理由: フロントエンドの柔軟性）",
            ],
        ),
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match="task lines do not exactly match"):
        fixture.assert_decision_recorded(
            plans_path,
            decision_substrings=["GraphQL", "フロントエンドの柔軟性"],
            expected_tasks=_DEFAULT_TASKS,
        )


def test_task_state_record_decision_oracle_rejects_modified_frontmatter(tmp_path: Path) -> None:
    fixture = _task_state_outcome_fixture()
    today = datetime.date.today().isoformat()
    plans_path = tmp_path / "Plans.md"
    plans_path.write_text(
        _plans_text(
            decisions=[
                "2026-01-01: 初期設計方針を確定",
                f"{today}: GraphQL を採用（理由: フロントエンドの柔軟性）",
            ],
            codd_status="draft",
        ),
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match="CODD frontmatter block was modified"):
        fixture.assert_decision_recorded(
            plans_path,
            decision_substrings=["GraphQL", "フロントエンドの柔軟性"],
            expected_tasks=_DEFAULT_TASKS,
        )

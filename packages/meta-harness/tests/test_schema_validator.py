"""手書き JSON Schema 検証器のテスト（Sec7「schema 検証」, EV-24）。

Phase 1a スコープの 8 スキーマ（`proposal.schema.json` は Phase 2 の専用テストで検証）のうち、
代表として `candidate.manifest.schema.json` / `ledger.event.schema.json`
（`$defs` + `oneOf` 経由） / `overlay.schema.json` / `result.schema.json`
（他ファイル参照 `$ref` 経由） / `frontier.schema.json` / `scenario.schema.json` /
`run.metadata.schema.json` / `config_patch.schema.json` を検証する。
"""

from __future__ import annotations

from pathlib import Path

from tests.module_loader import load_module

mh = load_module(
    "meta_harness_common_schema",
    "packages/meta-harness/lib/meta_harness_common.py",
)

SCHEMA_DIR = Path(__file__).resolve().parents[3] / "packages" / "meta-harness" / "schemas"


def _load(name: str) -> dict:
    return mh.load_schema(SCHEMA_DIR, name)


def _target_patterns(value: object) -> list[str]:
    if isinstance(value, dict):
        patterns = [
            pattern
            for key, pattern in value.items()
            if key == "pattern"
            and isinstance(pattern, str)
            and pattern.startswith("^(claude-harness|skill:")
        ]
        return patterns + [item for child in value.values() for item in _target_patterns(child)]
    if isinstance(value, list):
        return [item for child in value for item in _target_patterns(child)]
    return []


def test_target_patterns_stay_in_sync_with_runtime_validator() -> None:
    schema_names = (
        "candidate.manifest.schema.json",
        "frontier.schema.json",
        "ledger.event.schema.json",
        "run.metadata.schema.json",
        "scenario.schema.json",
    )
    patterns = [pattern for name in schema_names for pattern in _target_patterns(_load(name))]
    non_routing_pattern = "^(claude-harness|skill:[a-z0-9-]+)$"

    assert len(patterns) == 15
    assert set(patterns) == {mh.TARGET_PATTERN.pattern, non_routing_pattern}
    assert patterns.count(non_routing_pattern) == 6


class TestCandidateManifestSchema:
    _VALID = {
        "schema_version": "1.0",
        "cand_id": "cand-20260101-000000-my-slug",
        "parent_id": None,
        "generation": 0,
        "created_at": "2026-01-01T00:00:00+09:00",
        "created_by": "human",
        "target": "claude-harness",
        "source_commit": "a" * 40,
        "config_hash": "b" * 64,
        "model_versions": {},
        "overlay_files": ["facets/foo/SKILL.md"],
        "description": "desc",
    }

    def test_valid_instance_has_zero_errors(self) -> None:
        schema = _load("candidate.manifest.schema.json")
        assert mh.validate_against_schema(self._VALID, schema, SCHEMA_DIR) == []

    def test_missing_required_key_is_reported(self) -> None:
        schema = _load("candidate.manifest.schema.json")
        instance = {k: v for k, v in self._VALID.items() if k != "cand_id"}
        errors = mh.validate_against_schema(instance, schema, SCHEMA_DIR)
        assert any("cand_id" in e for e in errors)

    def test_enum_violation_is_reported(self) -> None:
        schema = _load("candidate.manifest.schema.json")
        instance = {**self._VALID, "created_by": "robot"}
        errors = mh.validate_against_schema(instance, schema, SCHEMA_DIR)
        assert any("not in enum" in e for e in errors)

    def test_pattern_violation_is_reported(self) -> None:
        schema = _load("candidate.manifest.schema.json")
        instance = {**self._VALID, "cand_id": "not-a-valid-cand-id"}
        errors = mh.validate_against_schema(instance, schema, SCHEMA_DIR)
        assert any("does not match pattern" in e for e in errors)

    def test_additional_property_is_reported(self) -> None:
        schema = _load("candidate.manifest.schema.json")
        instance = {**self._VALID, "unexpected_field": "x"}
        errors = mh.validate_against_schema(instance, schema, SCHEMA_DIR)
        assert any("additionalProperties" in e for e in errors)


class TestLedgerEventSchemaOneOf:
    _VALID_EVALUATION_COMPLETED = {
        "event": "evaluation_completed",
        "ts": "2026-07-16T00:00:00+09:00",
        "schema_version": "1.0",
        "evaluation_id": "eval-20260716-000000-abcdef12",
        "cand_id": "cand-routing-config",
        "target": "routing-config",
        "holdout": False,
        "own_run_ids": [],
        "own_suite_hash": "a" * 64,
        "evaluator_hash": "b" * 64,
        "own_critical_pass": True,
        "regression_results": [],
        "verdict": "pass",
        "unverified_impacts": [],
        "evaluation_base_commit": "c" * 40,
        "routing_config_base_hash": "d" * 64,
        "impacted_targets": [],
        "impact_input_hash": "e" * 64,
        "regression_cost_usd": 0.0,
    }

    def test_candidate_registered_matches_exactly_one_branch(self) -> None:
        schema = _load("ledger.event.schema.json")
        instance = {
            "event": "candidate_registered",
            "ts": "2026-01-01T00:00:00+09:00",
            "schema_version": "1.0",
            "cand_id": "cand-x",
            "parent_id": None,
            "generation": 0,
            "target": "claude-harness",
            "created_by": "human",
        }
        assert mh.validate_against_schema(instance, schema, SCHEMA_DIR) == []

    def test_candidate_registered_proposal_tokens_used_is_allowed(self) -> None:
        schema = _load("ledger.event.schema.json")
        instance = {
            "event": "candidate_registered",
            "ts": "2026-01-01T00:00:00+09:00",
            "schema_version": "1.0",
            "cand_id": "cand-x",
            "parent_id": None,
            "generation": 0,
            "target": "claude-harness",
            "created_by": "proposer",
            "proposal": {
                "theme": "tighten example",
                "based_on_runs": ["run-1"],
                "cost_usd": 0.0,
                "tokens_used": 123,
            },
        }
        assert mh.validate_against_schema(instance, schema, SCHEMA_DIR) == []

    def test_routing_config_evaluation_requires_base_hash(self) -> None:
        schema = _load("ledger.event.schema.json")
        evaluation_schema = schema["$defs"]["evaluation_completed"]
        instance = {
            key: value
            for key, value in self._VALID_EVALUATION_COMPLETED.items()
            if key != "routing_config_base_hash"
        }

        assert mh.validate_against_schema(instance, evaluation_schema, SCHEMA_DIR)

    def test_routing_config_evaluation_with_base_hash_is_valid(self) -> None:
        schema = _load("ledger.event.schema.json")["$defs"]["evaluation_completed"]

        assert (
            mh.validate_against_schema(self._VALID_EVALUATION_COMPLETED, schema, SCHEMA_DIR) == []
        )

    def test_non_routing_evaluation_allows_missing_base_hash(self) -> None:
        schema = _load("ledger.event.schema.json")["$defs"]["evaluation_completed"]
        instance = {
            key: value
            for key, value in self._VALID_EVALUATION_COMPLETED.items()
            if key != "routing_config_base_hash"
        }
        instance["target"] = "claude-harness"

        assert mh.validate_against_schema(instance, schema, SCHEMA_DIR) == []

    def test_evaluation_allows_optional_budget_latched_suites(self) -> None:
        schema = _load("ledger.event.schema.json")["$defs"]["evaluation_completed"]
        instance = {
            **self._VALID_EVALUATION_COMPLETED,
            "budget_latched_suites": ["claude-harness", "skill:issue-create"],
        }

        assert mh.validate_against_schema(instance, schema, SCHEMA_DIR) == []

    def test_rejected_and_cooldown_loop_iterations_forbid_cand_id(self) -> None:
        schema = _load("ledger.event.schema.json")["$defs"]["loop_iteration"]
        for outcome in ("proposal_rejected", "cooldown_wait"):
            instance = {
                "event": "loop_iteration",
                "ts": "2026-07-18T00:00:00+09:00",
                "schema_version": "1.0",
                "loop_id": "loop-20260718-routing",
                "iteration": 1,
                "outcome": outcome,
                "quality_best_before": 80.0,
                "quality_best_after": 80.0,
                "iteration_cost_usd": 0.0,
            }

            assert mh.validate_against_schema(instance, schema, SCHEMA_DIR) == []
            assert mh.validate_against_schema({**instance, "cand_id": "cand-x"}, schema, SCHEMA_DIR)

    def test_candidate_loop_iteration_with_cand_id_is_valid(self) -> None:
        schema = _load("ledger.event.schema.json")["$defs"]["loop_iteration"]
        instance = {
            "event": "loop_iteration",
            "ts": "2026-07-18T00:00:00+09:00",
            "schema_version": "1.0",
            "loop_id": "loop-20260718-routing",
            "iteration": 1,
            "cand_id": "cand-x",
            "outcome": "candidate",
            "quality_best_before": 80.0,
            "quality_best_after": 81.0,
            "iteration_cost_usd": 0.1,
        }

        assert mh.validate_against_schema(instance, schema, SCHEMA_DIR) == []

    def test_ambiguous_or_no_match_event_is_reported(self) -> None:
        schema = _load("ledger.event.schema.json")
        instance = {"event": "not_a_real_event"}
        errors = mh.validate_against_schema(instance, schema, SCHEMA_DIR)
        assert any("no oneOf branch matched" in e for e in errors)

    def test_status_changed_missing_required_key_is_caught(self) -> None:
        # 回帰テスト: status_changed def は "type"/"required"/"additionalProperties" と
        # 入れ子の "oneOf"（from/to の遷移許容）を併せ持つ。以前の実装は "oneOf" があると
        # 他キーワードの検証を完全にスキップするバグがあり、必須キー欠落や
        # additionalProperties 違反を検出できなかった（本テストスイート作成時に発見・修正）。
        schema = _load("ledger.event.schema.json")
        instance = {"cand_id": "c1", "from": "candidate", "to": "evaluated"}
        errors = mh.validate_against_schema(instance, schema["$defs"]["status_changed"], SCHEMA_DIR)
        assert any("missing required key 'event'" in e for e in errors)

    def test_status_changed_additional_property_is_caught(self) -> None:
        schema = _load("ledger.event.schema.json")
        instance = {
            "event": "status_changed",
            "ts": "2026-01-01T00:00:00+09:00",
            "schema_version": "1.0",
            "cand_id": "c1",
            "from": "candidate",
            "to": "evaluated",
            "reason": "r",
            "unexpected": "nope",
        }
        errors = mh.validate_against_schema(instance, schema["$defs"]["status_changed"], SCHEMA_DIR)
        assert any("additionalProperties" in e for e in errors)

    def test_status_changed_invalid_transition_is_caught(self) -> None:
        schema = _load("ledger.event.schema.json")
        instance = {
            "event": "status_changed",
            "ts": "2026-01-01T00:00:00+09:00",
            "schema_version": "1.0",
            "cand_id": "c1",
            "from": "promoted",
            "to": "candidate",
            "reason": "r",
        }
        errors = mh.validate_against_schema(instance, schema["$defs"]["status_changed"], SCHEMA_DIR)
        assert any("no oneOf branch matched" in e for e in errors)

    def test_status_changed_valid_transition_has_zero_errors(self) -> None:
        schema = _load("ledger.event.schema.json")
        instance = {
            "event": "status_changed",
            "ts": "2026-01-01T00:00:00+09:00",
            "schema_version": "1.0",
            "cand_id": "c1",
            "from": "evaluated",
            "to": "promoted",
            "reason": "confirmed",
        }
        errors = mh.validate_against_schema(instance, schema["$defs"]["status_changed"], SCHEMA_DIR)
        assert errors == []

    def test_promotion_released_promoted_reason_has_zero_errors(self) -> None:
        schema = _load("ledger.event.schema.json")
        instance = {
            "event": "promotion_released",
            "ts": "2026-07-09T00:00:00+09:00",
            "schema_version": "1.0",
            "cand_id": "cand-20260709-010000-promote-abcd",
            "reason": "promoted",
        }
        errors = mh.validate_against_schema(
            instance, schema["$defs"]["promotion_released"], SCHEMA_DIR
        )
        assert errors == []


class TestOverlaySchema:
    def test_valid_instance_has_zero_errors(self) -> None:
        schema = _load("overlay.schema.json")
        instance = {"schema_version": "1.0", "files": ["facets/foo/SKILL.md"]}
        assert mh.validate_against_schema(instance, schema, SCHEMA_DIR) == []

    def test_pattern_violation_on_files_item_is_reported(self) -> None:
        schema = _load("overlay.schema.json")
        instance = {"schema_version": "1.0", "files": ["not-under-facets/x.txt"]}
        errors = mh.validate_against_schema(instance, schema, SCHEMA_DIR)
        assert any("does not match pattern" in e for e in errors)

    def test_missing_required_key_is_reported(self) -> None:
        schema = _load("overlay.schema.json")
        errors = mh.validate_against_schema({"schema_version": "1.0"}, schema, SCHEMA_DIR)
        assert any("files" in e for e in errors)


class TestResultSchema:
    _VALID = {
        "schema_version": "1.0",
        "run_id": "run-20260101-000000-slug-scn-a1-abcd1234",
        "cand_id": "cand-x",
        "scenario_id": "scenario-1",
        "verdict": "pass",
        "critical": [],
        "critical_pass_rate": 1.0,
        "checks": [],
        "self_report": None,
        "penalty": 0,
        "quality_score": 100.0,
        "cost": {
            "input_tokens": 1,
            "output_tokens": 1,
            "total_tokens": 2,
            "tool_uses": 0,
            "duration_ms": 1,
            "total_cost_usd": 0.0,
            "num_turns": 1,
        },
        "attempt": 1,
        "attempts_total": 1,
        "claude_version": "2.1.201",
        "errors": [],
    }

    def test_valid_instance_has_zero_errors(self) -> None:
        schema = _load("result.schema.json")
        assert mh.validate_against_schema(self._VALID, schema, SCHEMA_DIR) == []

    def test_cross_file_ref_to_ledger_cost_def_is_enforced(self) -> None:
        schema = _load("result.schema.json")
        instance = {**self._VALID, "cost": {"input_tokens": 1}}  # cost def の required 欠落
        errors = mh.validate_against_schema(instance, schema, SCHEMA_DIR)
        assert any("missing required key" in e for e in errors)

    def test_verdict_enum_violation_is_reported(self) -> None:
        schema = _load("result.schema.json")
        instance = {**self._VALID, "verdict": "maybe"}
        errors = mh.validate_against_schema(instance, schema, SCHEMA_DIR)
        assert any("not in enum" in e for e in errors)

    def test_optional_budget_latched_true_is_valid(self) -> None:
        schema = _load("result.schema.json")
        instance = {**self._VALID, "verdict": "error", "budget_latched": True}

        assert mh.validate_against_schema(instance, schema, SCHEMA_DIR) == []

    # EV-24: claude_version は result.schema.json の required に含まれる
    def test_claude_version_is_a_required_field(self) -> None:
        schema = _load("result.schema.json")
        assert "claude_version" in schema["required"]


class TestFrontierSchema:
    def test_valid_instance_has_zero_errors(self) -> None:
        schema = _load("frontier.schema.json")
        instance = {
            "schema_version": "1.0",
            "target": "claude-harness",
            "generated_at": "2026-01-01T00:00:00+09:00",
            "ledger_line_count": 0,
            "suite_hash": "0" * 64,
            "evaluator_hash": "0" * 64,
            "cost_axis": "total_tokens",
            "points": [],
            "frontier": [],
            "dominated": [],
        }
        assert mh.validate_against_schema(instance, schema, SCHEMA_DIR) == []

    def test_points_item_additional_property_is_reported(self) -> None:
        schema = _load("frontier.schema.json")
        instance = {
            "schema_version": "1.0",
            "target": "claude-harness",
            "generated_at": "2026-01-01T00:00:00+09:00",
            "ledger_line_count": 0,
            "suite_hash": "0" * 64,
            "evaluator_hash": "0" * 64,
            "cost_axis": "total_tokens",
            "points": [
                {
                    "cand_id": "c1",
                    "quality_mean": 1.0,
                    "quality_var": 0.0,
                    "quality_min": 1.0,
                    "cost_mean": 1.0,
                    "runs": 1,
                    "eligible": True,
                }
            ],
            "frontier": [],
            "dominated": [],
        }
        errors = mh.validate_against_schema(instance, schema, SCHEMA_DIR)
        assert any("additionalProperties" in e for e in errors)


class TestScenarioSchema:
    _VALID = {
        "schema_version": "1.0",
        "id": "scenario-1",
        "target": "claude-harness",
        "description": "d",
        "prompt": "do the thing",
        "critical": [
            {"id": "c1", "text": "must pass", "oracle": "command_exit", "command": "true"}
        ],
    }

    def test_valid_instance_has_zero_errors(self) -> None:
        schema = _load("scenario.schema.json")
        assert mh.validate_against_schema(self._VALID, schema, SCHEMA_DIR) == []

    def test_min_items_violation_on_critical_is_reported(self) -> None:
        schema = _load("scenario.schema.json")
        instance = {**self._VALID, "critical": []}
        errors = mh.validate_against_schema(instance, schema, SCHEMA_DIR)
        assert any(">= 1" in e for e in errors)

    def test_id_pattern_violation_is_reported(self) -> None:
        schema = _load("scenario.schema.json")
        instance = {**self._VALID, "id": "Not Valid ID"}
        errors = mh.validate_against_schema(instance, schema, SCHEMA_DIR)
        assert any("does not match pattern" in e for e in errors)

    def test_safe_path_prepend_is_accepted(self) -> None:
        schema = _load("scenario.schema.json")
        instance = {**self._VALID, "path_prepend": ["bin", "tools/local-bin"]}
        assert mh.validate_against_schema(instance, schema, SCHEMA_DIR) == []

    def test_path_prepend_traversal_is_rejected(self) -> None:
        schema = _load("scenario.schema.json")
        instance = {**self._VALID, "path_prepend": ["../bin"]}
        errors = mh.validate_against_schema(instance, schema, SCHEMA_DIR)
        assert any("does not match pattern" in error for error in errors)

    def test_permission_mode_accept_edits_is_accepted(self) -> None:
        schema = _load("scenario.schema.json")
        instance = {**self._VALID, "permission_mode": "acceptEdits"}
        assert mh.validate_against_schema(instance, schema, SCHEMA_DIR) == []

    def test_permission_mode_bypass_permissions_is_accepted(self) -> None:
        # Issue #261 PR6 bot レビュー対応: `.claude/` protected path への非対話書込みを
        # 個別シナリオだけに限定して opt-in させるための override（詳細は evaluator.py
        # `_effective_scenario_execution` のコメント参照）。
        schema = _load("scenario.schema.json")
        instance = {**self._VALID, "permission_mode": "bypassPermissions"}
        assert mh.validate_against_schema(instance, schema, SCHEMA_DIR) == []

    def test_permission_mode_invalid_value_is_rejected(self) -> None:
        schema = _load("scenario.schema.json")
        instance = {**self._VALID, "permission_mode": "plan"}
        errors = mh.validate_against_schema(instance, schema, SCHEMA_DIR)
        assert any("permission_mode" in error for error in errors)


class TestRunMetadataSchema:
    _VALID = {
        "schema_version": "1.0",
        "run_id": "run-20260101-000000-slug-scn-a1-abcd1234",
        "cand_id": "cand-x",
        "scenario_id": "scenario-1",
        "suite_id": "claude-harness",
        "suite_hash": "a" * 64,
        "scenario_hash": "b" * 64,
        "evaluator_hash": "c" * 64,
        "target": "claude-harness",
        "holdout": False,
        "project_root": "/tmp/project",
        "ai_orchestra_dir": "/tmp/ai-orchestra",
        "source_commit": "a" * 40,
        "config_hash": "b" * 64,
        "model": None,
        "claude_version": "2.1.201",
        "cli_capabilities": {},
        "allowed_tools": ["Read"],
        "allowed_tools_source": "global",
        "model_tools": ["Read"],
        "max_output_tokens": 4096,
        "max_output_tokens_source": "global",
        "path_prepend": [],
        "permission_mode": "acceptEdits",
        "permission_mode_source": "global",
        "started_at": "2026-01-01T00:00:00+09:00",
        "attempt": 1,
        "attempts_total": 1,
    }

    def test_valid_instance_has_zero_errors(self) -> None:
        schema = _load("run.metadata.schema.json")
        assert mh.validate_against_schema(self._VALID, schema, SCHEMA_DIR) == []

    def test_legacy_instance_without_permission_mode_is_still_valid(self) -> None:
        """PR #273 bot review round 4: `permission_mode` / `permission_mode_source` were added
        to `required` without a `schema_version` bump, which would have rejected pre-existing
        run metadata (written before these fields existed) on re-validation. They must stay
        optional so schema_version 1.0 legacy runs keep validating; the evaluator write side
        (not this schema) is responsible for always populating them going forward (see
        `test_run_metadata_records_permission_mode_and_source` in
        test_evaluator_headless_run.py)."""
        schema = _load("run.metadata.schema.json")
        legacy_instance = {
            key: value
            for key, value in self._VALID.items()
            if key not in ("permission_mode", "permission_mode_source")
        }

        assert mh.validate_against_schema(legacy_instance, schema, SCHEMA_DIR) == []

    def test_permission_mode_accepts_non_scenario_documented_modes(self) -> None:
        """PR #273 bot review round 6: `_effective_scenario_execution` passes the global
        `evaluate.permission_mode` config value through to metadata verbatim whenever no
        scenario-level override is present, so this schema (a factual record of what was
        actually run) must accept every documented Claude Code `--permission-mode` value, not
        just the two meta-harness currently lets an individual *scenario* opt into
        (`scenario.schema.json`'s `permission_mode` stays restricted to `acceptEdits`/
        `bypassPermissions` -- that is a separate, intentionally narrower policy choice)."""
        schema = _load("run.metadata.schema.json")
        instance = {**self._VALID, "permission_mode": "dontAsk"}

        assert mh.validate_against_schema(instance, schema, SCHEMA_DIR) == []

    def test_routing_config_requires_base_hash(self) -> None:
        schema = _load("run.metadata.schema.json")
        instance = {
            **self._VALID,
            "target": "routing-config",
            "suite_id": "routing-config",
        }

        assert mh.validate_against_schema(instance, schema, SCHEMA_DIR)

    def test_routing_config_with_base_hash_is_valid(self) -> None:
        schema = _load("run.metadata.schema.json")
        instance = {
            **self._VALID,
            "target": "routing-config",
            "suite_id": "routing-config",
            "routing_config_base_hash": "d" * 64,
        }

        assert mh.validate_against_schema(instance, schema, SCHEMA_DIR) == []

    def test_non_routing_target_allows_missing_base_hash(self) -> None:
        schema = _load("run.metadata.schema.json")

        assert mh.validate_against_schema(self._VALID, schema, SCHEMA_DIR) == []

    def test_missing_required_key_is_reported(self) -> None:
        schema = _load("run.metadata.schema.json")
        instance = {k: v for k, v in self._VALID.items() if k != "claude_version"}
        errors = mh.validate_against_schema(instance, schema, SCHEMA_DIR)
        assert any("claude_version" in e for e in errors)

    def test_isolation_subfields_are_required_when_isolation_is_present(self) -> None:
        schema = _load("run.metadata.schema.json")
        instance = {**self._VALID, "isolation": {}}

        errors = mh.validate_against_schema(instance, schema, SCHEMA_DIR)

        assert any("backend" in e for e in errors)
        assert any("platform_profile_input_sha256" in e for e in errors)

    def test_docker_broker_metrics_are_strictly_validated(self) -> None:
        schema = _load("run.metadata.schema.json")
        metrics = {
            "request_count": 1,
            "rejected_count": 0,
            "upstream_request_bytes": 128,
            "usage": {
                "input_tokens": 10,
                "output_tokens": 2,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "total_tokens": 12,
            },
            "estimated_cost_usd": 0.001,
            "budget_exceeded": False,
            "anomaly": False,
            "anomaly_reasons": [],
        }
        isolation = {
            "backend": "docker",
            "image": "scenario:test",
            "image_id": "sha256:" + "1" * 64,
            "broker_image": "broker:test",
            "broker_image_id": "sha256:" + "2" * 64,
            "broker_settings_sha256": "3" * 64,
            "scenario_context_sha256": "4" * 64,
            "broker_context_sha256": "5" * 64,
            "scenario_base_image": "node:test@sha256:" + "6" * 64,
            "broker_base_image": "broker:test@sha256:" + "7" * 64,
            "platform_profile_input_sha256": "8" * 64,
            "resources": {
                "pids_limit": 128,
                "memory": "2g",
                "cpus": 2.0,
                "workspace_size": "512m",
                "workspace_max_files": 10000,
                "max_lifetime_sec": 660,
            },
            "git": {"mode": "isolated-snapshot", "source_commit": "a" * 40},
            "broker": {"metrics": metrics},
        }

        assert (
            mh.validate_against_schema({**self._VALID, "isolation": isolation}, schema, SCHEMA_DIR)
            == []
        )
        isolation_with_budget_rejection = {
            **isolation,
            "broker": {"metrics": {**metrics, "budget_rejected_count": 1}},
        }
        assert (
            mh.validate_against_schema(
                {**self._VALID, "isolation": isolation_with_budget_rejection}, schema, SCHEMA_DIR
            )
            == []
        )
        invalid_memory = {
            **isolation,
            "resources": {**isolation["resources"], "memory": "unbounded"},
        }
        memory_errors = mh.validate_against_schema(
            {**self._VALID, "isolation": invalid_memory}, schema, SCHEMA_DIR
        )
        assert memory_errors
        errors = mh.validate_against_schema(
            {**self._VALID, "isolation": {**isolation, "broker": {"metrics": {}}}},
            schema,
            SCHEMA_DIR,
        )
        assert errors


class TestConfigPatchSchema:
    def test_valid_instance_has_zero_errors(self) -> None:
        schema = _load("config_patch.schema.json")
        instance = [{"file": "x.yaml", "key_path": "a.b", "value": 1}]
        assert mh.validate_against_schema(instance, schema, SCHEMA_DIR) == []

    def test_missing_required_key_in_item_is_reported(self) -> None:
        schema = _load("config_patch.schema.json")
        instance = [{"file": "x.yaml", "key_path": "a.b"}]
        errors = mh.validate_against_schema(instance, schema, SCHEMA_DIR)
        assert any("value" in e for e in errors)

    def test_additional_property_in_item_is_reported(self) -> None:
        schema = _load("config_patch.schema.json")
        instance = [{"file": "x.yaml", "key_path": "a.b", "value": 1, "extra": True}]
        errors = mh.validate_against_schema(instance, schema, SCHEMA_DIR)
        assert any("additionalProperties" in e for e in errors)

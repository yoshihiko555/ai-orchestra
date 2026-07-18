"""ヘッドレス実行（`claude -p`）の結果判定テスト（Sec2-2, Sec2-5）。

PR #168 レビュー指摘（Codex P1 x2）に対応:
- `claude -p` が非ゼロ終了・is_error=true（budget 打ち切り含む）・result イベント欠落の
  場合、oracle 判定結果に関わらず run 段階のエラーとして扱われること（成果物が残っていても
  pass にしない）。
- シナリオ実行が候補ハーネス（worktree）を評価対象にするよう、`AI_ORCHESTRA_DIR` を
  worktree_dir に明示設定した env で `claude -p` を起動すること。

`claude` は一切呼ばない。runner は完全にフェイクに差し替える。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.module_loader import load_module

ev = load_module(
    "meta_harness_evaluator_headless_run",
    "packages/meta-harness/lib/evaluator.py",
)


def _completed(returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode)


def _write_result_event(events_path: Path, event: dict) -> None:
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _write_events(events_path: Path, events: list[dict]) -> None:
    events_path.write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )


def _install_isolation_launch(monkeypatch, tmp_path: Path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("{}\n", encoding="utf-8")
    launch = ev.siso.ScenarioIsolationLaunch(
        executable="/usr/bin/srt",
        settings_path=settings_path,
        settings={},
        env={"AI_ORCHESTRA_DIR": str(tmp_path / "worktree"), "PATH": "/usr/bin"},
        metadata={
            "backend": "srt",
            "srt_version": "1.0.0",
            "settings_sha256": "a" * 64,
            "platform_profile_input_sha256": "b" * 64,
        },
    )
    monkeypatch.setattr(ev.siso, "resolve_scenario_isolation", lambda **_kwargs: launch)
    monkeypatch.setattr(ev.siso, "cleanup_scenario_isolation", lambda _launch: None)
    monkeypatch.setattr(ev.siso, "execution_boundary_available", lambda _config: True)
    return launch


class TestCheckHeadlessRunOutcome:
    """`_check_headless_run_outcome` 単体のテスト（Codex P1: 非ゼロ終了が pass 化するバグ）。"""

    def test_success_case_does_not_raise(self, tmp_path: Path) -> None:
        events_path = tmp_path / "events.jsonl"
        _write_result_event(
            events_path, {"type": "result", "subtype": "success", "is_error": False}
        )
        ev._check_headless_run_outcome(_completed(0), events_path)  # 例外が出なければ OK

    def test_is_error_true_forces_error(self, tmp_path: Path) -> None:
        events_path = tmp_path / "events.jsonl"
        _write_result_event(
            events_path, {"type": "result", "subtype": "error_during_execution", "is_error": True}
        )
        try:
            ev._check_headless_run_outcome(_completed(1), events_path)
        except ev.EvaluatorStageError as exc:
            assert exc.stage == "run"
            assert exc.error_type == "run_error"
        else:
            raise AssertionError("is_error=true should raise EvaluatorStageError")

    def test_budget_exceeded_subtype_forces_error_with_budget_exceeded_type(
        self, tmp_path: Path
    ) -> None:
        """budget 打ち切りで成果物ファイルが残っていても pass にしない（Sec2-5）。"""
        events_path = tmp_path / "events.jsonl"
        _write_result_event(
            events_path, {"type": "result", "subtype": "error_max_budget_usd", "is_error": True}
        )
        try:
            ev._check_headless_run_outcome(_completed(1), events_path)
        except ev.EvaluatorStageError as exc:
            assert exc.stage == "run"
            assert exc.error_type == "budget_exceeded"
        else:
            raise AssertionError("error_max_budget_usd should raise EvaluatorStageError")

    def test_missing_result_event_forces_error(self, tmp_path: Path) -> None:
        events_path = tmp_path / "events.jsonl"
        events_path.write_text("", encoding="utf-8")
        try:
            ev._check_headless_run_outcome(_completed(0), events_path)
        except ev.EvaluatorStageError as exc:
            assert exc.stage == "run"
            assert exc.error_type == "run_error"
        else:
            raise AssertionError("missing result event should raise EvaluatorStageError")

    # EV-18: extract_cost() retains a best-effort ZERO_COST fallback, but the independent
    # headless outcome guard must prevent that fallback from becoming a passing frontier run.
    def test_missing_result_zero_cost_fallback_cannot_be_frontier_eligible(
        self, tmp_path: Path
    ) -> None:
        events_path = tmp_path / "events.jsonl"
        events_path.write_text("", encoding="utf-8")

        cost = ev.extract_cost(events_path)
        assert cost == ev.ZERO_COST
        with pytest.raises(ev.EvaluatorStageError, match="no result event"):
            ev._check_headless_run_outcome(_completed(0), events_path)

        critical = [{"id": "behavior", "passed": True, "oracle": "artifact_exists"}]
        verdict = ev._determine_verdict(hard_failure=True, critical_checks=critical)
        assert verdict == "error"
        point = ev.mh._summarize_candidate_runs(
            "cand-zero-cost",
            [
                {
                    "run_id": "run-zero-cost",
                    "scenario_id": "behavioral",
                    "holdout": False,
                    "verdict": verdict,
                    "quality_score": 100.0,
                    "cost": cost,
                }
            ],
            "total_cost_usd",
            frozenset({"behavioral"}),
        )

        assert point["cost_mean"] == 0.0
        assert point["eligible"] is False
        frontier, dominated = ev.mh.compute_pareto_frontier(
            [candidate for candidate in [point] if candidate["eligible"]],
            target="routing-config",
        )
        assert frontier == []
        assert dominated == []

    def test_nonzero_exit_with_success_subtype_still_forces_error(self, tmp_path: Path) -> None:
        """result イベントは success を報告していても、プロセス自体が非ゼロ終了なら error。"""
        events_path = tmp_path / "events.jsonl"
        _write_result_event(
            events_path, {"type": "result", "subtype": "success", "is_error": False}
        )
        try:
            ev._check_headless_run_outcome(_completed(2), events_path)
        except ev.EvaluatorStageError as exc:
            assert exc.stage == "run"
            assert exc.error_type == "run_error"
        else:
            raise AssertionError("nonzero exit code should raise EvaluatorStageError")


class TestScenarioExecutionEnvelope:
    def test_explicit_empty_allowed_tools_does_not_fall_back_to_global(self) -> None:
        execution = ev._effective_scenario_execution(
            {
                "target": "skill:handoff",
                "allowed_tools": [],
                "budget": {"max_output_tokens": 1024},
            },
            {
                "evaluate": {"allowed_tools": ["Read", "Bash(python3 *)"]},
                "scenario_run": {"max_output_tokens_default": 4096},
            },
        )

        assert execution == {
            "allowed_tools": [],
            "allowed_tools_source": "scenario",
            "model_tools": ["Skill"],
            "max_output_tokens": 1024,
            "max_output_tokens_source": "scenario",
            "path_prepend": [],
        }

    def test_headless_command_contains_minimal_model_tools_and_output_limit(
        self, tmp_path: Path
    ) -> None:
        command = ev._build_headless_command(
            {
                "target": "skill:handoff",
                "prompt": "/handoff test",
                "allowed_tools": ["Bash(python3 *)", "Write"],
                "budget": {"max_output_tokens": 1024},
            },
            {},
            tmp_path / "instruction.md",
        )

        assert command[:3] == [
            "/usr/bin/env",
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS=1024",
            "claude",
        ]
        assert command[command.index("--allowedTools") + 1] == "Bash(python3 *) Write"
        assert command[command.index("--tools") + 1] == "Bash Write Skill"

    def test_headless_command_prepends_safe_workspace_path(self, tmp_path: Path) -> None:
        command = ev._build_headless_command(
            {
                "target": "skill:issue-create",
                "prompt": "/issue-create task test",
                "allowed_tools": ["Bash(gh *)"],
                "path_prepend": ["bin"],
            },
            {},
            tmp_path / "instruction.md",
        )

        assert command[:4] == [
            "/usr/bin/env",
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS=4096",
            "PATH=/workspace/bin:/runtime/bin:/usr/local/bin:/usr/bin:/bin",
            "claude",
        ]

    def test_unsafe_path_prepend_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="safe relative paths"):
            ev._effective_scenario_execution(
                {"target": "skill:issue-create", "path_prepend": ["../bin"]},
                {},
            )

    def test_explicit_null_output_tokens_default_falls_back_to_4096(self) -> None:
        """`scenario_run.max_output_tokens_default: null` must not raise TypeError."""
        execution = ev._effective_scenario_execution(
            {"target": "skill:handoff"},
            {"scenario_run": {"max_output_tokens_default": None}},
        )

        assert execution["max_output_tokens"] == 4096
        assert execution["max_output_tokens_source"] == "global"

    def test_execution_snapshot_treats_null_output_tokens_default_as_4096(self) -> None:
        # Issue #261 PR2 review round 3: both models must be pinned (and present in
        # model_allowlist) or evaluator_execution_snapshot() now fails closed before
        # this null-output-tokens-fallback concern can even be exercised.
        snapshot = ev.evaluator_execution_snapshot(
            {
                "scenario_run": {"max_output_tokens_default": None},
                "evaluate": {
                    "model": "claude-sonnet-5",
                    "isolation": {"broker": {"model_allowlist": ["claude-sonnet-5"]}},
                },
                "judge": {"model": "claude-sonnet-5"},
            }
        )

        assert snapshot["max_output_tokens_default"] == 4096

    def test_evaluator_hash_changes_when_execution_fallback_changes(self) -> None:
        first = ev.compute_evaluator_hash(
            {}, {"allowed_tools": ["Read"], "max_output_tokens_default": 4096}
        )
        second = ev.compute_evaluator_hash(
            {}, {"allowed_tools": ["Read"], "max_output_tokens_default": 2048}
        )

        assert first != second

    def test_execution_snapshot_includes_cost_comparability_scope(self) -> None:
        """Issue #261 PR2: judge model/effort, broker pricing, broker model allowlist and the
        global scenario budget default must be part of the evaluator hash scope, since a
        config-only change to any of these breaks cost/quality comparability across runs."""
        config = {
            "judge": {"tool": "codex", "model": "claude-sonnet-5", "effort": "high"},
            "evaluate": {
                "model": "claude-sonnet-5",
                "isolation": {
                    "broker": {
                        "pricing_upper_bound_usd_per_million": {"input": 3.0, "output": 15.0},
                        "model_allowlist": ["claude-sonnet-5"],
                    }
                },
            },
            "scenario_run": {"max_budget_usd_default": 3.0},
        }

        snapshot = ev.evaluator_execution_snapshot(config)

        assert snapshot["judge_tool"] == "codex"
        assert snapshot["judge_model"] == "claude-sonnet-5"
        assert snapshot["judge_effort"] == "high"
        assert snapshot["broker_pricing_upper_bound_usd_per_million"] == {
            "input": 3.0,
            "output": 15.0,
        }
        assert snapshot["broker_model_allowlist"] == ["claude-sonnet-5"]
        assert snapshot["scenario_run_max_budget_usd_default"] == 3.0

    def test_execution_snapshot_fails_closed_when_repinned_model_mismatches_allowlist(
        self,
    ) -> None:
        """Issue #261 PR2 review round 2: computing the evaluator hash for a config
        whose pinned judge.model/evaluate.model is missing from the configured
        broker model_allowlist must fail closed with an actionable error rather than
        silently produce a hash for a broker configuration that would itself refuse
        to start (or, worse, previously auto-admitted the pricier model)."""
        config = {
            "judge": {"model": "claude-sonnet-5", "effort": "high"},
            "evaluate": {
                "model": "claude-sonnet-5",
                "isolation": {
                    "broker": {
                        "pricing_upper_bound_usd_per_million": {"input": 3.0},
                        "model_allowlist": ["claude-opus-4-8"],
                    }
                },
            },
            "scenario_run": {"max_budget_usd_default": 3.0},
        }

        with pytest.raises(ev.siso.docker.profile.DockerProfileError) as excinfo:
            ev.evaluator_execution_snapshot(config)

        message = str(excinfo.value)
        assert "claude-sonnet-5" in message
        assert "evaluate.isolation.broker.model_allowlist" in message
        assert "pricing_upper_bound_usd_per_million" in message

    @pytest.mark.parametrize(
        "override",
        [
            # judge.tool changes the scoring path (claude-bare vs codex) with no other
            # config change, so it alone must stale prior evaluator_hash-scoped runs
            # (CodeRabbit High, PR #265).
            {"judge": {"tool": "codex"}},
            # judge.model repin must also extend model_allowlist, or the fail-closed
            # guard (Issue #261 PR2 review round 2) would reject the config outright
            # before a hash could even be computed -- see the dedicated fail-closed
            # test below for that behavior.
            {
                "judge": {"model": "claude-opus-4-8"},
                "evaluate": {
                    "isolation": {
                        "broker": {"model_allowlist": ["claude-sonnet-5", "claude-opus-4-8"]}
                    }
                },
            },
            {
                "evaluate": {
                    "isolation": {
                        "broker": {"pricing_upper_bound_usd_per_million": {"input": 15.0}}
                    }
                }
            },
            {"scenario_run": {"max_budget_usd_default": 54.0}},
        ],
        ids=[
            "judge_tool",
            "judge_model",
            "broker_pricing",
            "scenario_run_budget",
        ],
    )
    def test_evaluator_hash_changes_when_cost_comparability_scope_changes(
        self, override: dict
    ) -> None:
        base_config: dict = {
            "judge": {"model": "claude-sonnet-5", "effort": "high"},
            "evaluate": {
                "model": "claude-sonnet-5",
                "isolation": {
                    "broker": {
                        "pricing_upper_bound_usd_per_million": {"input": 3.0},
                        "model_allowlist": ["claude-sonnet-5"],
                    }
                },
            },
            "scenario_run": {"max_budget_usd_default": 3.0},
        }
        changed_config = json.loads(json.dumps(base_config))
        for key, value in override.items():
            changed_config[key] = _deep_merge(changed_config.get(key, {}), value)

        before = ev.compute_configured_evaluator_hash(base_config)
        after = ev.compute_configured_evaluator_hash(changed_config)

        assert before != after

    def test_evaluator_hash_unaffected_by_unpinned_menu_surplus_entries(self) -> None:
        """Issue #261 PR2 review round 3: effective_broker_model_allowlist wires only
        the pinned evaluate.model/judge.model pair to the broker, never the full
        configured model_allowlist "menu" (surplus entries lack a pricing
        calibration and must not be admitted). Adding an unrelated, unpinned entry
        to the menu is therefore a config-comparability no-op and must NOT stale
        prior evaluator_hash-scoped runs."""
        base_config: dict = {
            "judge": {"model": "claude-sonnet-5", "effort": "high"},
            "evaluate": {
                "model": "claude-sonnet-5",
                "isolation": {
                    "broker": {
                        "pricing_upper_bound_usd_per_million": {"input": 3.0},
                        "model_allowlist": ["claude-sonnet-5"],
                    }
                },
            },
            "scenario_run": {"max_budget_usd_default": 3.0},
        }
        menu_expanded_config = json.loads(json.dumps(base_config))
        menu_expanded_config["evaluate"]["isolation"]["broker"]["model_allowlist"] = [
            "claude-sonnet-5",
            "claude-opus-4-8-experimental",
        ]

        before = ev.compute_configured_evaluator_hash(base_config)
        after = ev.compute_configured_evaluator_hash(menu_expanded_config)

        assert before == after


class TestSkillActivationEvidence:
    def test_registered_slash_command_with_tool_use_passes(self, tmp_path: Path) -> None:
        events_path = tmp_path / "events.jsonl"
        _write_events(
            events_path,
            [
                {"type": "system", "subtype": "init", "slash_commands": ["handoff"]},
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "tool_use", "name": "Bash", "input": {"command": "true"}}
                        ]
                    },
                },
            ],
        )

        ev._verify_headless_skill_activation(
            {"target": "skill:handoff", "prompt": "/handoff test"},
            events_path,
        )

    def test_registration_is_accumulated_across_multiple_init_events(self, tmp_path: Path) -> None:
        events_path = tmp_path / "events.jsonl"
        _write_events(
            events_path,
            [
                {"type": "system", "subtype": "init", "slash_commands": ["handoff"]},
                {"type": "system", "subtype": "init", "slash_commands": []},
                {"type": "assistant", "message": {"content": [{"type": "tool_use"}]}},
            ],
        )

        ev._verify_headless_skill_activation(
            {"target": "skill:handoff", "prompt": "/handoff test"}, events_path
        )

    def test_missing_registered_slash_command_fails_closed(self, tmp_path: Path) -> None:
        events_path = tmp_path / "events.jsonl"
        _write_events(
            events_path,
            [
                {"type": "system", "subtype": "init", "slash_commands": []},
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "tool_use", "name": "Bash"}]},
                },
            ],
        )

        with pytest.raises(ev.EvaluatorStageError, match="was not registered"):
            ev._verify_headless_skill_activation(
                {"target": "skill:handoff", "prompt": "/handoff test"},
                events_path,
            )

    def test_registered_slash_without_tool_use_fails_closed(self, tmp_path: Path) -> None:
        events_path = tmp_path / "events.jsonl"
        _write_events(
            events_path,
            [{"type": "system", "subtype": "init", "slash_commands": ["handoff"]}],
        )

        with pytest.raises(ev.EvaluatorStageError, match="produced no tool use"):
            ev._verify_headless_skill_activation(
                {"target": "skill:handoff", "prompt": "/handoff test"},
                events_path,
            )


class TestRunHeadlessScenarioEnvironment:
    """Codex P1: シナリオ実行が親環境の AI_ORCHESTRA_DIR を継承する問題。"""

    def test_ai_orchestra_dir_env_points_to_worktree_dir(self, tmp_path: Path, monkeypatch) -> None:
        launch = _install_isolation_launch(monkeypatch, tmp_path)
        worktree_dir = tmp_path / "worktree"
        worktree_dir.mkdir()
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        instruction_path = tmp_path / "self-report-instruction.md"
        instruction_path.write_text("irrelevant", encoding="utf-8")

        captured_env: dict[str, str] = {}
        captured_command: list[str] = []

        def fake_runner(cmd, **kwargs):
            captured_command.extend(cmd)
            captured_env.update(kwargs.get("env") or {})
            kwargs["stdout"].write(
                (
                    json.dumps({"type": "result", "subtype": "success", "is_error": False}) + "\n"
                ).encode()
            )
            return _completed(0)

        monkeypatch.setattr(ev.sproc, "run_bounded_process_tree", fake_runner)

        scenario = {"id": "s1", "prompt": "irrelevant"}
        ev.run_headless_scenario(
            scenario,
            {},
            worktree_dir,
            staging_dir,
            instruction_path,
            main_root=tmp_path,
            source_commit="a" * 40,
            runner=fake_runner,
        )

        assert captured_env.get("AI_ORCHESTRA_DIR") == str(worktree_dir)
        assert captured_command[:3] == [
            launch.executable,
            "--settings",
            str(launch.settings_path),
        ]
        assert "--setting-sources" in captured_command
        assert "project,local" in captured_command
        assert "--no-chrome" in captured_command
        metadata = json.loads((staging_dir / "isolation.json").read_text(encoding="utf-8"))
        assert metadata == launch.metadata

    def test_effective_scenario_timeout_and_budget_reach_broker_config(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        launch = _install_isolation_launch(monkeypatch, tmp_path)
        captured: dict = {}

        def resolve(**kwargs):
            captured.update(kwargs)
            return launch

        monkeypatch.setattr(ev.siso, "resolve_scenario_isolation", resolve)
        worktree = tmp_path / "worktree"
        staging = tmp_path / "staging"
        instruction = tmp_path / "instruction.md"
        worktree.mkdir()
        staging.mkdir()
        instruction.write_text("irrelevant")

        def fake_runner(_cmd, **kwargs):
            kwargs["stdout"].write(b'{"type":"result","subtype":"success","is_error":false}\n')
            return _completed()

        monkeypatch.setattr(ev.sproc, "run_bounded_process_tree", fake_runner)
        ev.run_headless_scenario(
            {
                "id": "s1",
                "prompt": "irrelevant",
                "timeout_ms": 900000,
                "budget": {"max_budget_usd": 1.25},
            },
            {"evaluate": {"timeout_ms_default": 300000}},
            worktree,
            staging,
            instruction,
            main_root=tmp_path,
            source_commit="a" * 40,
            runner=fake_runner,
        )

        assert captured["config"]["evaluate"]["timeout_ms_default"] == 900000
        assert captured["config"]["scenario_run"]["max_budget_usd_default"] == 1.25

    def test_raises_when_result_event_indicates_budget_exceeded(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        launch = _install_isolation_launch(monkeypatch, tmp_path)
        worktree_dir = tmp_path / "worktree"
        worktree_dir.mkdir()
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        instruction_path = tmp_path / "self-report-instruction.md"
        instruction_path.write_text("irrelevant", encoding="utf-8")
        lifecycle_events: list[str] = []

        def refresh_metadata(refreshed_launch):
            assert refreshed_launch is launch
            lifecycle_events.append("refresh")
            return {
                **launch.metadata,
                "broker": {
                    "metrics": {
                        "budget_exceeded": True,
                        "anomaly": True,
                    }
                },
            }

        def cleanup(cleaned_launch):
            assert cleaned_launch is launch
            persisted = json.loads((staging_dir / "isolation.json").read_text())
            assert persisted["broker"]["metrics"]["budget_exceeded"] is True
            lifecycle_events.append("cleanup")

        monkeypatch.setattr(ev.siso, "refresh_isolation_metadata", refresh_metadata)
        monkeypatch.setattr(ev.siso, "cleanup_scenario_isolation", cleanup)

        def fake_runner(cmd, **kwargs):
            kwargs["stdout"].write(
                (
                    json.dumps(
                        {"type": "result", "subtype": "error_max_budget_usd", "is_error": True}
                    )
                    + "\n"
                ).encode()
            )
            return _completed(1)

        monkeypatch.setattr(ev.sproc, "run_bounded_process_tree", fake_runner)

        scenario = {"id": "s1", "prompt": "irrelevant"}
        try:
            ev.run_headless_scenario(
                scenario,
                {},
                worktree_dir,
                staging_dir,
                instruction_path,
                main_root=tmp_path,
                source_commit="a" * 40,
                runner=fake_runner,
            )
        except ev.EvaluatorStageError as exc:
            assert exc.error_type == "budget_exceeded"
        else:
            raise AssertionError(
                "budget-exceeded result event should raise EvaluatorStageError, not return"
                " a HeadlessRunResult that lets oracle checks decide pass/fail"
            )
        assert lifecycle_events == ["refresh", "cleanup"]

    def test_isolation_failure_is_fail_closed(self, tmp_path: Path, monkeypatch) -> None:
        worktree_dir = tmp_path / "worktree"
        worktree_dir.mkdir()
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        instruction_path = tmp_path / "instruction.md"
        instruction_path.write_text("irrelevant", encoding="utf-8")
        monkeypatch.setattr(
            ev.siso,
            "resolve_scenario_isolation",
            lambda **_kwargs: (_ for _ in ()).throw(
                ev.siso.ScenarioIsolationError("canary failed")
            ),
        )
        monkeypatch.setattr(ev.siso, "execution_boundary_available", lambda _config: True)

        with pytest.raises(ev.EvaluatorStageError, match="isolation unavailable") as exc_info:
            ev.run_headless_scenario(
                {"id": "s1", "prompt": "irrelevant"},
                {},
                worktree_dir,
                staging_dir,
                instruction_path,
                main_root=tmp_path,
                source_commit="a" * 40,
                runner=lambda *_args, **_kwargs: pytest.fail("runner must not be called"),
            )

        assert exc_info.value.error_type == "run_error"

    def test_timeout_always_cleans_isolation(self, tmp_path: Path, monkeypatch) -> None:
        launch = _install_isolation_launch(monkeypatch, tmp_path)
        cleaned = []
        monkeypatch.setattr(ev.siso, "cleanup_scenario_isolation", cleaned.append)
        worktree_dir = tmp_path / "worktree"
        worktree_dir.mkdir()
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        instruction_path = tmp_path / "instruction.md"
        instruction_path.write_text("irrelevant", encoding="utf-8")

        def timeout_runner(cmd, **_kwargs):
            raise subprocess.TimeoutExpired(cmd, 1)

        monkeypatch.setattr(ev.sproc, "run_bounded_process_tree", timeout_runner)

        with pytest.raises(ev.EvaluatorStageError) as exc_info:
            ev.run_headless_scenario(
                {"id": "s1", "prompt": "irrelevant", "timeout_ms": 1000},
                {},
                worktree_dir,
                staging_dir,
                instruction_path,
                main_root=tmp_path,
                source_commit="a" * 40,
                runner=timeout_runner,
            )

        assert exc_info.value.error_type == "timeout"
        assert cleaned == [launch]

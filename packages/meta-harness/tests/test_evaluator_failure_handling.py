"""失敗処理のテスト（EV-13, Sec2-5）。

どの段階でエラーが発生しても verdict=error の result.json + ledger 追記を必ず行うこと。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.module_loader import load_module

ev = load_module(
    "meta_harness_evaluator_failure_handling",
    "packages/meta-harness/lib/evaluator.py",
)
mh = load_module(
    "meta_harness_common_failure_handling",
    "packages/meta-harness/lib/meta_harness_common.py",
)

_SCHEMA_DIR = Path("packages/meta-harness/schemas").resolve()
_PACKAGE_DIR = Path("packages/meta-harness").resolve()
_SCENARIO_PATH = Path(
    "packages/meta-harness/scenarios/claude-harness/summarize-readme.yaml"
).resolve()


def _run_attempt(tmp_path: Path, manifest_source_commit: str) -> tuple[dict, Path]:
    scenario = {
        "id": "summarize-readme",
        "prompt": "irrelevant",
        "setup": [],
        "critical": [{"id": "c1", "text": "n/a", "oracle": "artifact_exists", "path": "x.md"}],
        "checks": [],
        "holdout": False,
    }
    manifest = {"source_commit": manifest_source_commit, "config_hash": "b" * 64}
    cli_capabilities = {"claude_version": "2.1.202", "ok": True}
    result = ev.run_single_attempt(
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
    return result, tmp_path


class TestWorktreeCreationFailureIsRecordedAsError:
    def test_invalid_source_commit_yields_error_verdict_and_ledger_entry(
        self, tmp_path: Path
    ) -> None:
        result, main_root = _run_attempt(tmp_path, manifest_source_commit="0" * 40)

        assert result["verdict"] == "error"
        assert result["errors"]
        assert result["errors"][0]["stage"] == "worktree_create"
        assert result["errors"][0]["type"] == "worktree_error"

        run_dir = mh.runs_dir(main_root, mh.DEFAULTS) / result["run_id"]
        assert run_dir.is_dir()
        on_disk_result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        assert on_disk_result["verdict"] == "error"

        events = mh.read_ledger_events(main_root, mh.DEFAULTS)
        run_completed = [e for e in events if e.get("event") == "run_completed"]
        assert run_completed
        assert run_completed[-1]["verdict"] == "error"
        assert run_completed[-1]["run_id"] == result["run_id"]

    def test_metadata_still_written_even_on_failure(self, tmp_path: Path) -> None:
        result, main_root = _run_attempt(tmp_path, manifest_source_commit="0" * 40)
        run_dir = mh.runs_dir(main_root, mh.DEFAULTS) / result["run_id"]
        metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
        assert metadata["run_id"] == result["run_id"]
        assert metadata["finished_at"] is not None


def _git_head(git_project: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=git_project, capture_output=True, text=True, check=True
    ).stdout.strip()


class TestEachStageFailureForcesErrorVerdict:
    def test_overlay_stage_failure_forces_error(self, git_project: Path, monkeypatch) -> None:
        """CodeRabbit 指摘（PR #168）: worktree 作成を実際に成功させ、overlay ステージの
        エラー分類（`overlay_apply`/`overlay_error`）まで到達させて検証する（従来は無効な
        commit で worktree_create が先に失敗し、overlay ステージに一度も到達しなかった）。"""

        def failing_apply_overlay(overlay_dir, config, worktree_dir, schema_dir, **_kwargs):
            raise ev.EvaluatorStageError("overlay_apply", "overlay_error", "forced")

        monkeypatch.setattr(ev, "apply_overlay", failing_apply_overlay)
        result, _main_root = _run_attempt(
            git_project, manifest_source_commit=_git_head(git_project)
        )

        assert result["verdict"] == "error"
        assert result["errors"]
        assert result["errors"][0]["stage"] == "overlay_apply"
        assert result["errors"][0]["type"] == "overlay_error"

    def test_generic_exception_in_lifecycle_is_caught_and_forces_error(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        def raising_lifecycle(**kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(ev, "_run_attempt_lifecycle", raising_lifecycle)
        try:
            result, _main_root = _run_attempt(tmp_path, manifest_source_commit="0" * 40)
            assert result["verdict"] == "error"
        except RuntimeError:
            raise AssertionError(
                "run_single_attempt must not propagate exceptions from the lifecycle"
                " (Sec2-5 requires verdict=error to always be recorded)"
            ) from None


class TestNoCriticalChecksIsRecordedAsError:
    def test_zero_critical_checks_yields_error_verdict(self, tmp_path: Path, monkeypatch) -> None:
        def fake_lifecycle(**kwargs):
            return [], [], False, []  # critical checks が空

        monkeypatch.setattr(ev, "_run_attempt_lifecycle", fake_lifecycle)
        result, main_root = _run_attempt(tmp_path, manifest_source_commit="0" * 40)
        assert result["verdict"] == "error"
        events = mh.read_ledger_events(main_root, mh.DEFAULTS)
        run_completed = [e for e in events if e.get("event") == "run_completed"]
        assert run_completed[-1]["verdict"] == "error"


class TestBrokerMetricsForceRunError:
    @pytest.mark.parametrize(
        ("metrics", "expected_type"),
        [
            ({"budget_exceeded": True, "anomaly": False, "anomaly_reasons": []}, "budget_exceeded"),
            (
                {
                    "budget_exceeded": False,
                    "anomaly": True,
                    "anomaly_reasons": ["invalid upstream response"],
                },
                "run_error",
            ),
        ],
    )
    def test_refreshed_broker_violation_is_a_hard_failure(
        self, tmp_path: Path, monkeypatch, metrics: dict, expected_type: str
    ) -> None:
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        staging = tmp_path / "staging"
        staging.mkdir()
        launch = ev.siso.ScenarioIsolationLaunch(
            executable="docker",
            settings_path=None,
            settings={},
            env={},
            metadata={"backend": "docker"},
            backend="docker",
        )
        monkeypatch.setattr(ev, "worktree_root", lambda *_args: tmp_path / "worktrees")
        monkeypatch.setattr(ev, "create_worktree", lambda *_args, **_kwargs: worktree)
        monkeypatch.setattr(ev, "apply_overlay", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(ev, "build_facet_and_context", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(ev, "run_setup_commands", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(
            ev,
            "run_headless_scenario",
            lambda *_args, **_kwargs: ev.HeadlessRunResult(
                events_path=staging / "events.jsonl",
                progress_path=staging / "progress.log",
                timed_out=False,
                isolation_launch=launch,
            ),
        )
        refresh_count = 0

        def run_oracle(check, *_args, **_kwargs):
            assert refresh_count == 1
            return {
                "id": check["id"],
                "passed": True,
                "oracle": check["oracle"],
                "detail": "ok",
            }

        def refresh_isolation_metadata(_launch):
            nonlocal refresh_count
            refresh_count += 1
            return {"backend": "docker", "broker": {"metrics": metrics}}

        monkeypatch.setattr(ev, "run_oracle", run_oracle)
        monkeypatch.setattr(ev.siso, "refresh_isolation_metadata", refresh_isolation_metadata)
        monkeypatch.setattr(ev.siso, "cleanup_scenario_isolation", lambda _launch: None)
        monkeypatch.setattr(ev, "remove_worktree", lambda *_args, **_kwargs: None)

        checks, _, hard_failure, errors = ev._run_attempt_lifecycle(
            main_root=tmp_path,
            config=mh.DEFAULTS,
            schema_dir=_SCHEMA_DIR,
            package_dir=_PACKAGE_DIR,
            cand_dir=tmp_path / "candidate",
            manifest={"source_commit": "a" * 40},
            scenario={
                "critical": [{"id": "c1", "text": "n/a", "oracle": "artifact_exists", "path": "x"}],
                "checks": [],
            },
            run_id="run-test",
            staging_dir=staging,
            runner=subprocess.run,
        )

        assert checks[0]["passed"] is True
        assert refresh_count == 2
        assert hard_failure is True
        assert errors[-1]["stage"] == "broker"
        assert errors[-1]["type"] == expected_type


class TestJudgeErrorForcesRunVerdictError:
    """Codex 指摘（evaluator.py:746）: judge backend の error は fail-closed（Sec3-3）に従い、
    check の fail や silent pass ではなく run 全体の verdict=error に伝播しなければならない。"""

    def _run_with_rubric_judge(
        self, git_project: Path, monkeypatch, *, judge_check_is_critical: bool
    ) -> dict:
        def noop_overlay(overlay_dir, config, worktree_dir, schema_dir, **_kwargs):
            return None

        def noop_build(worktree_dir, **_kwargs):
            return None

        def noop_setup(scenario, worktree_dir, **_kwargs):
            return None

        def noop_headless_run(
            scenario,
            config,
            worktree_dir,
            staging_dir,
            instruction,
            *,
            main_root=None,
            source_commit=None,
            runner=None,
        ):
            events_path = staging_dir / "events.jsonl"
            staging_dir.mkdir(parents=True, exist_ok=True)
            events_path.write_text(
                json.dumps({"type": "result", "subtype": "success", "is_error": False}) + "\n",
                encoding="utf-8",
            )
            return ev.HeadlessRunResult(
                events_path=events_path,
                progress_path=staging_dir / "progress.log",
                timed_out=False,
            )

        def erroring_judge(
            rubric,
            worktree_dir,
            config,
            schema_dir,
            *,
            isolation_launch=None,
            runner=None,
        ):
            return ev.JudgeVerdict(False, "judge unavailable: forced for test", "codex", error=True)

        monkeypatch.setattr(ev, "apply_overlay", noop_overlay)
        monkeypatch.setattr(ev, "build_facet_and_context", noop_build)
        monkeypatch.setattr(ev, "run_setup_commands", noop_setup)
        monkeypatch.setattr(ev, "run_headless_scenario", noop_headless_run)
        monkeypatch.setattr(ev, "run_rubric_judge", erroring_judge)

        judge_check = {"id": "j1", "text": "n/a", "oracle": "rubric_judge", "rubric": "check it"}
        # README.md は git_project フィクスチャの初期コミットに実在するため、
        # 非 judge の critical check は本来ここでは pass する（judge error 以外の
        # 理由で verdict=error にならないようにするための対照条件）。
        passing_critical = {
            "id": "c-readme",
            "text": "n/a",
            "oracle": "artifact_exists",
            "path": "README.md",
        }
        scenario = {
            "id": "summarize-readme",
            "prompt": "irrelevant",
            "setup": [],
            "critical": [judge_check] if judge_check_is_critical else [passing_critical],
            "checks": [] if judge_check_is_critical else [judge_check],
            "holdout": False,
        }
        manifest = {"source_commit": _git_head(git_project), "config_hash": "b" * 64}
        cli_capabilities = {"claude_version": "2.1.202", "ok": True}
        return ev.run_single_attempt(
            main_root=git_project,
            config=mh.DEFAULTS,
            schema_dir=_SCHEMA_DIR,
            package_dir=_PACKAGE_DIR,
            project_dir=git_project,
            cand_id="cand-20260707-120000-slug-ab12",
            cand_dir=git_project / "cand",
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

    def test_critical_rubric_judge_error_forces_verdict_error(
        self, git_project: Path, monkeypatch
    ) -> None:
        result = self._run_with_rubric_judge(git_project, monkeypatch, judge_check_is_critical=True)
        assert result["verdict"] == "error"
        assert any(e["type"] == "oracle_error" for e in result["errors"])

    def test_non_critical_rubric_judge_error_forces_verdict_error(
        self, git_project: Path, monkeypatch
    ) -> None:
        """非 critical の judge check がエラーでも、他の critical check が pass しているという
        理由だけで run 全体が pass 扱いにならないこと（judge が一度も実行されていないため）。"""
        result = self._run_with_rubric_judge(
            git_project, monkeypatch, judge_check_is_critical=False
        )
        assert result["verdict"] == "error"
        assert any(e["type"] == "oracle_error" for e in result["errors"])


class TestLedgerAppendLockConflictIsDiagnosable:
    """Codex 指摘（evaluator.py:1372）: `cmd_evaluate` の `except mh.LockAcquisitionError`
    は ledger 追記中の lock 競合を既に exit code 3 に正規化する（実測確認済み、`with
    mh.evaluate_lock(...):` の内側で送出された例外は外側の try/except まで正常に伝播する）。
    本テストは診断性向上の確認: 成果物が ledger 未記載のまま残る run_dir のパスが、
    再送出される例外メッセージに含まれること。"""

    def test_lock_acquisition_error_message_includes_run_dir(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        def fake_lifecycle(**kwargs):
            checks = [{"id": "c1", "passed": True, "oracle": "artifact_exists", "detail": "ok"}]
            return checks, [], False, []

        monkeypatch.setattr(ev, "_run_attempt_lifecycle", fake_lifecycle)

        def failing_append(main_root, config, event):
            # `evaluator.py` 内の `except mh.LockAcquisitionError` は `ev` 自身が import した
            # `meta_harness_common` インスタンス（`ev.mh`）のクラスでなければ捕捉できない
            # （module_loader はテストファイルの `mh` とは別インスタンスとしてロードするため）。
            raise ev.mh.LockAcquisitionError("store.lock is held by another process")

        monkeypatch.setattr(ev, "append_run_completed_event", failing_append)

        try:
            _run_attempt(tmp_path, manifest_source_commit="0" * 40)
        except ev.mh.LockAcquisitionError as exc:
            run_dirs = list(mh.runs_dir(tmp_path, mh.DEFAULTS).glob("run-*"))
            assert run_dirs
            assert str(run_dirs[0]) in str(exc)
            assert "not recorded in ledger.jsonl" in str(exc)
        else:
            raise AssertionError(
                "LockAcquisitionError must propagate so the CLI can normalize it to exit 3"
            )


def test_judge_claude_bare_nonzero_exit_reports_both_stderr_and_stdout(monkeypatch) -> None:
    """Issue #354: claude --bare の非ゼロ終了時、従来は stderr のみをメッセージへ採用して
    stdout を破棄していた。`--output-format json` はエラー診断を stdout の JSON へ書くことが
    あるため（実測では stderr が空文字で真因が artifacts から追跡不能だった）、エラー
    メッセージに stderr と stdout の両方の抜粋が含まれることを固定する。"""
    monkeypatch.setattr(ev, "_has_bare_auth", lambda: True)

    def fake_runner(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd,
            returncode=1,
            stdout='{"type":"error","message":"model not allowed by broker"}',
            stderr="",
        )

    verdict = ev._judge_via_claude_bare(
        "irrelevant prompt",
        {},
        max_output_tokens=1024,
        isolation_launch=None,
        runner=fake_runner,
    )

    assert verdict.error is True
    assert "claude --bare exited 1" in verdict.reason
    assert "model not allowed by broker" in verdict.reason  # stdout 抜粋が残ること
    assert "stderr=" in verdict.reason and "stdout=" in verdict.reason


_JUDGE_PASS_STDOUT = '{"passed": true, "reason": "ok"}'
_JUDGE_FAIL_STDOUT = '{"passed": false, "reason": "rubric not satisfied"}'


def _make_flaky_runner(outcomes: list[subprocess.CompletedProcess]):
    """呼び出しごとに outcomes を順に返す runner。呼び出し回数を記録する。"""
    calls: list[list[str]] = []

    def runner(cmd, **kwargs):
        calls.append(list(cmd))
        return outcomes[len(calls) - 1]

    return runner, calls


def test_judge_unavailable_retries_once_and_recovers(monkeypatch, tmp_path) -> None:
    """Issue #354: judge が一過性のインフラ要因（使い捨てコンテナ連続起動の終盤で
    claude --bare が exit 1）で実行できなかった場合、同一 backend で 1 回だけリトライして
    回復すること。数十秒後の同一コマンドが成功する実測に基づく堅牢化。"""
    monkeypatch.setattr(ev, "_has_bare_auth", lambda: True)
    sleeps: list[float] = []
    monkeypatch.setattr(ev.time, "sleep", sleeps.append)
    runner, calls = _make_flaky_runner(
        [
            subprocess.CompletedProcess([], returncode=1, stdout="", stderr=""),
            subprocess.CompletedProcess([], returncode=0, stdout=_JUDGE_PASS_STDOUT, stderr=""),
        ]
    )

    verdict = ev.run_rubric_judge(
        "irrelevant rubric", tmp_path, mh.DEFAULTS, _SCHEMA_DIR, runner=runner
    )

    assert verdict.error is False
    assert verdict.passed is True
    assert len(calls) == 2
    assert sleeps == [ev.JUDGE_UNAVAILABLE_RETRY_DELAY_SECONDS]


def test_judge_unavailable_after_retry_stays_error_with_both_reasons(monkeypatch, tmp_path) -> None:
    """リトライ後も失敗した場合は fail-closed（verdict=error）を維持し、初回・再試行の
    両方の失敗理由がメッセージに残ること（別 backend へ降格しないこと）。"""
    monkeypatch.setattr(ev, "_has_bare_auth", lambda: True)
    monkeypatch.setattr(ev.time, "sleep", lambda _s: None)
    runner, calls = _make_flaky_runner(
        [
            subprocess.CompletedProcess([], returncode=1, stdout="boot failure A", stderr=""),
            subprocess.CompletedProcess([], returncode=1, stdout="boot failure B", stderr=""),
        ]
    )

    verdict = ev.run_rubric_judge(
        "irrelevant rubric", tmp_path, mh.DEFAULTS, _SCHEMA_DIR, runner=runner
    )

    assert verdict.error is True
    assert verdict.backend == "claude-bare"
    assert len(calls) == 2
    assert "after retry" in verdict.reason
    assert "boot failure A" in verdict.reason and "boot failure B" in verdict.reason


def test_judge_retry_worst_case_is_reflected_in_container_lifetime() -> None:
    """Issue #354: リトライ導入で judge 1 check の最悪所要時間は
    JUDGE_TIMEOUT_SECONDS×2 + retry delay へ増えた。broker/コンテナの max lifetime が
    この増分（JUDGE_RETRY_EXTRA_LIFETIME_SECONDS、手動同期の定数）を織り込んでいることを
    突合し、リトライがコンテナ寿命切れで確実に失敗する経路（レビュー指摘）を塞ぐ。"""
    sdp = load_module(
        "meta_harness_sdp_failure_handling",
        "packages/meta-harness/lib/scenario_docker_profile.py",
    )
    retry_extra = ev.JUDGE_TIMEOUT_SECONDS + ev.JUDGE_UNAVAILABLE_RETRY_DELAY_SECONDS
    assert sdp.JUDGE_RETRY_EXTRA_LIFETIME_SECONDS >= retry_extra
    assert (
        sdp.broker_max_lifetime_seconds(mh.DEFAULTS)
        >= sdp.container_max_lifetime_seconds(mh.DEFAULTS) + retry_extra
    )


def test_judge_rubric_fail_is_not_retried(monkeypatch, tmp_path) -> None:
    """rubric の fail 判定（passed=false）は judge 実行自体の失敗ではないためリトライしない
    こと（リトライは判定セマンティクスを変えない、の固定）。"""
    monkeypatch.setattr(ev, "_has_bare_auth", lambda: True)
    monkeypatch.setattr(
        ev.time,
        "sleep",
        lambda _s: (_ for _ in ()).throw(AssertionError("sleep must not be called")),
    )
    runner, calls = _make_flaky_runner(
        [subprocess.CompletedProcess([], returncode=0, stdout=_JUDGE_FAIL_STDOUT, stderr="")]
    )

    verdict = ev.run_rubric_judge(
        "irrelevant rubric", tmp_path, mh.DEFAULTS, _SCHEMA_DIR, runner=runner
    )

    assert verdict.error is False
    assert verdict.passed is False
    assert len(calls) == 1

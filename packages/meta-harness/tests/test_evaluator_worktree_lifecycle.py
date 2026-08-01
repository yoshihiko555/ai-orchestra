"""worktree ライフサイクルのテスト（EV-14, EV-31, Sec2-1）。

`git` は実プロセス（既定 runner=subprocess.run）で操作する（claude/codex は使わない）。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tests.module_loader import load_module

ev = load_module(
    "meta_harness_evaluator_worktree_lifecycle",
    "packages/meta-harness/lib/evaluator.py",
)
hook_common = load_module(
    "hook_common_routing_config_materialization",
    "packages/core/hooks/hook_common.py",
)
_SCHEMA_DIR = Path("packages/meta-harness/schemas").resolve()


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


class TestCreateWorktreeSuccess:
    def test_creates_detached_worktree_at_source_commit(self, git_project: Path) -> None:
        head = _git("rev-parse", "HEAD", cwd=git_project).stdout.strip()
        root = git_project / ".worktrees" / "meta"
        worktree_dir = ev.create_worktree(git_project, root, "run-test-0001", head)
        try:
            assert worktree_dir.is_dir()
            assert (worktree_dir / "README.md").is_file()
            worktree_head = _git("rev-parse", "HEAD", cwd=worktree_dir).stdout.strip()
            assert worktree_head == head
        finally:
            ev.remove_worktree(git_project, worktree_dir)


class TestCreateWorktreeFailure:
    def test_invalid_source_commit_raises_stage_error(self, git_project: Path) -> None:
        root = git_project / ".worktrees" / "meta"
        try:
            ev.create_worktree(git_project, root, "run-test-badcommit", "0" * 40)
        except ev.EvaluatorStageError as exc:
            assert exc.stage == "worktree_create"
            assert exc.error_type == "worktree_error"
        else:
            raise AssertionError("invalid source_commit should raise EvaluatorStageError")

    def test_no_worktree_directory_left_behind_after_failure(self, git_project: Path) -> None:
        root = git_project / ".worktrees" / "meta"
        try:
            ev.create_worktree(git_project, root, "run-test-badcommit2", "0" * 40)
        except ev.EvaluatorStageError:
            pass
        assert not (root / "wt-run-test-badcommit2").exists()


class TestRemoveWorktreeIsBestEffort:
    def test_remove_worktree_does_not_raise_when_directory_missing(self, git_project: Path) -> None:
        root = git_project / ".worktrees" / "meta"
        # 一度も作成していないパスを渡しても例外を送出しないこと（best-effort）。
        ev.remove_worktree(git_project, root / "wt-never-created")

    def test_remove_worktree_actually_removes_the_worktree(self, git_project: Path) -> None:
        head = _git("rev-parse", "HEAD", cwd=git_project).stdout.strip()
        root = git_project / ".worktrees" / "meta"
        worktree_dir = ev.create_worktree(git_project, root, "run-test-remove", head)
        assert worktree_dir.is_dir()

        ev.remove_worktree(git_project, worktree_dir)

        assert not worktree_dir.exists()
        listing = _git("worktree", "list", cwd=git_project).stdout
        assert str(worktree_dir) not in listing


class TestEnsureBridgeArtifactIgnored:
    """PR #326 レビュー round 4 (Codex P1): 既存候補を再評価する worktree は候補登録時点の古い
    source_commit から checkout されるため、bridge artifact 専用の `.gitignore` 除外行が
    存在しない可能性がある。source_commit の年代に依存させず、worktree 作成直後にこの1行だけを
    実行時に追記する（`git_project` の `.gitignore` は `.claude/meta-harness/` のみで、
    このレビュー対応前の履歴的な source_commit を模している）。"""

    def test_appends_single_file_ignore_line_when_missing(self, git_project: Path) -> None:
        root = git_project / ".worktrees" / "meta"
        head = _git("rev-parse", "HEAD", cwd=git_project).stdout.strip()
        worktree_dir = ev.create_worktree(git_project, root, "run-bridge-ignore", head)
        try:
            gitignore_path = worktree_dir / ".gitignore"
            before_lines = gitignore_path.read_text(encoding="utf-8").splitlines()
            assert "meta-harness-oracle" not in "\n".join(before_lines)

            ev._ensure_bridge_artifact_ignored(worktree_dir)

            after_lines = gitignore_path.read_text(encoding="utf-8").splitlines()
            expected_line = f"/{ev.CANDIDATE_FINAL_REPORT_RELATIVE_PATH.as_posix()}"
            assert expected_line in after_lines
            # ディレクトリ全体ではなく単一ファイルだけを ignore 対象にする（候補が
            # bypassPermissions で同じディレクトリ配下に書いた他のファイルは
            # collateral-scope の untracked-file 検査で引き続き捕捉されるべきなので）。
            assert ".claude/meta-harness-oracle/" not in after_lines
        finally:
            ev.remove_worktree(git_project, worktree_dir)

    def test_is_idempotent_across_repeated_calls(self, git_project: Path) -> None:
        root = git_project / ".worktrees" / "meta"
        head = _git("rev-parse", "HEAD", cwd=git_project).stdout.strip()
        worktree_dir = ev.create_worktree(git_project, root, "run-bridge-ignore-idem", head)
        try:
            ev._ensure_bridge_artifact_ignored(worktree_dir)
            ev._ensure_bridge_artifact_ignored(worktree_dir)

            lines = (worktree_dir / ".gitignore").read_text(encoding="utf-8").splitlines()
            expected_line = f"/{ev.CANDIDATE_FINAL_REPORT_RELATIVE_PATH.as_posix()}"
            assert lines.count(expected_line) == 1
        finally:
            ev.remove_worktree(git_project, worktree_dir)

    def test_rejects_symlinked_gitignore(self, tmp_path: Path) -> None:
        worktree_dir = tmp_path / "worktree"
        worktree_dir.mkdir()
        outside = tmp_path / "outside.gitignore"
        outside.write_text("", encoding="utf-8")
        (worktree_dir / ".gitignore").symlink_to(outside)

        with pytest.raises(ev.EvaluatorStageError):
            ev._ensure_bridge_artifact_ignored(worktree_dir)


class TestMaterializeCurrentOracleFixtures:
    """PR #326 レビュー round 4 (Codex P1) + Issue #340 Codex 設計レビュー: 既存候補を再評価する
    worktree の `scenarios/fixtures/` は候補登録時点の古い source_commit のものになり、新しい
    サブコマンド（例: add-phase-with-ac）を持たず argparse の exit 2 で落ちる。attempt 開始時に
    `_snapshot_trusted_oracle_fixtures` で固定した信頼済み immutable copy の内容で worktree 側を
    上書き materialize する。source 欠落は silent no-op ではなく fail-closed（改ざん対策の復元が
    黙って無効化されるのを防ぐ）。"""

    def _make_package_dir(self, tmp_path: Path) -> Path:
        package_dir = tmp_path / "package"
        (package_dir / "scenarios" / "fixtures").mkdir(parents=True)
        (package_dir / "scenarios" / "fixtures" / "assert-task-state-outcome.py").write_text(
            "# current trusted fixture\n", encoding="utf-8"
        )
        return package_dir

    def test_overwrites_stale_fixture_with_current_content(self, tmp_path: Path) -> None:
        package_dir = self._make_package_dir(tmp_path)

        worktree_dir = tmp_path / "worktree"
        stale_fixture_dir = worktree_dir / "packages" / "meta-harness" / "scenarios" / "fixtures"
        stale_fixture_dir.mkdir(parents=True)
        (stale_fixture_dir / "assert-task-state-outcome.py").write_text(
            "# stale fixture without add-phase-with-ac\n", encoding="utf-8"
        )
        (stale_fixture_dir / "stale-only-file.py").write_text(
            "# should be removed by materialization\n", encoding="utf-8"
        )

        trusted = ev._snapshot_trusted_oracle_fixtures(package_dir, tmp_path / "staging")
        ev._materialize_current_oracle_fixtures(worktree_dir, trusted)

        materialized = stale_fixture_dir / "assert-task-state-outcome.py"
        assert materialized.read_text(encoding="utf-8") == "# current trusted fixture\n"
        assert not (stale_fixture_dir / "stale-only-file.py").exists()

    def test_snapshot_fails_closed_when_trusted_source_missing(self, tmp_path: Path) -> None:
        """Issue #340 Codex 設計レビュー: source 欠落の silent no-op は post-run の改ざん対策
        復元を黙って無効化するため、EvaluatorStageError（verdict=error 経路）で fail-closed
        する。"""
        package_dir = tmp_path / "package-without-fixtures"
        package_dir.mkdir()

        with pytest.raises(ev.EvaluatorStageError) as excinfo:
            ev._snapshot_trusted_oracle_fixtures(package_dir, tmp_path / "staging")
        assert "trusted oracle fixtures source missing" in excinfo.value.message

    def test_materialize_fails_closed_when_trusted_copy_missing(self, tmp_path: Path) -> None:
        worktree_dir = tmp_path / "worktree"
        worktree_dir.mkdir()

        with pytest.raises(ev.EvaluatorStageError) as excinfo:
            ev._materialize_current_oracle_fixtures(worktree_dir, tmp_path / "no-such-copy")
        assert "trusted oracle fixtures copy missing" in excinfo.value.message
        assert not (worktree_dir / "packages").exists()

    def test_snapshot_copy_is_immutable_against_later_source_updates(self, tmp_path: Path) -> None:
        """TOCTOU 対策: snapshot 後に信頼済み source 側が外部更新されても、固定済み copy の
        内容は変化しない。"""
        package_dir = self._make_package_dir(tmp_path)

        trusted = ev._snapshot_trusted_oracle_fixtures(package_dir, tmp_path / "staging")
        (package_dir / "scenarios" / "fixtures" / "assert-task-state-outcome.py").write_text(
            "# updated after snapshot\n", encoding="utf-8"
        )

        frozen = trusted / "assert-task-state-outcome.py"
        assert frozen.read_text(encoding="utf-8") == "# current trusted fixture\n"

    def test_rejects_symlinked_destination(self, tmp_path: Path) -> None:
        package_dir = tmp_path / "package"
        (package_dir / "scenarios" / "fixtures").mkdir(parents=True)
        (package_dir / "scenarios" / "fixtures" / "f.py").write_text("x", encoding="utf-8")

        worktree_dir = tmp_path / "worktree"
        (worktree_dir / "packages" / "meta-harness" / "scenarios").mkdir(parents=True)
        outside = tmp_path / "outside-fixtures"
        outside.mkdir()
        (worktree_dir / "packages" / "meta-harness" / "scenarios" / "fixtures").symlink_to(
            outside, target_is_directory=True
        )

        trusted = ev._snapshot_trusted_oracle_fixtures(package_dir, tmp_path / "staging")
        with pytest.raises(ev.EvaluatorStageError):
            ev._materialize_current_oracle_fixtures(worktree_dir, trusted)


class TestOracleFixtureMaterializationTiming:
    """PR #326 レビュー round 4/5 (Codex P1) + Issue #340: `scenarios/fixtures/` の materialize
    は次の2回行われなければならない。

    1. **snapshot 前**（worktree 作成直後・`run_headless_scenario` より前）: isolated git
       snapshot のベースラインコミットへ現行の信頼済み fixture を含める。これが無いと、
       fixture 改修後に古い source_commit の候補を再評価した際、oracle 直前の再 materialize に
       よる差分が「候補による tracked 変更」として collateral-scope oracle に検出され、
       noop 候補ですら決定論的に fail する（Issue #340）。
    2. **候補実行後・oracle 実行前**: bypassPermissions 下の候補が fixture スクリプトを
       改ざんしても、oracle 判定前に信頼済み内容へ復元する。候補実行前だけに materialize
       すると、outcome/collateral 両チェック（同一スクリプトが両方の subcommand を実装して
       いる）を自分の改変ごと隠して通過できてしまう。
    """

    _TRUSTED_COPY_SENTINEL = Path("/sentinel/trusted-oracle-fixtures")

    def _run_lifecycle_with_tracked_calls(
        self, git_project: Path, monkeypatch
    ) -> tuple[list[str], list[tuple], bool, list[dict]]:
        call_order: list[str] = []
        materialize_args: list[tuple] = []
        package_dir = Path("packages/meta-harness").resolve()

        monkeypatch.setattr(ev, "apply_overlay", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(ev, "build_facet_and_context", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(ev, "run_setup_commands", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(ev.siso, "cleanup_scenario_isolation", lambda *_args, **_kwargs: None)

        def fake_run_headless_scenario(*_args, **_kwargs):
            call_order.append("run_headless_scenario")
            return SimpleNamespace(isolation_launch=object())

        def fake_snapshot(_package_dir, _staging_dir):
            call_order.append("snapshot_trusted_fixtures")
            return self._TRUSTED_COPY_SENTINEL

        def fake_materialize(worktree_dir, trusted_fixtures_dir):
            call_order.append("materialize_fixtures")
            materialize_args.append((worktree_dir, trusted_fixtures_dir))

        monkeypatch.setattr(ev, "run_headless_scenario", fake_run_headless_scenario)
        monkeypatch.setattr(ev, "_snapshot_trusted_oracle_fixtures", fake_snapshot)
        monkeypatch.setattr(ev, "_materialize_current_oracle_fixtures", fake_materialize)

        _checks, _checks_nc, hard_failure, errors = ev._run_attempt_lifecycle(
            main_root=git_project,
            config={"evaluate": {"worktree_root": ".worktrees/meta"}},
            schema_dir=_SCHEMA_DIR,
            package_dir=package_dir,
            cand_dir=git_project,
            manifest={"source_commit": _git("rev-parse", "HEAD", cwd=git_project).stdout.strip()},
            scenario={
                "id": "s-materialize-order",
                "prompt": "irrelevant",
                "setup": [],
                "critical": [],
                "checks": [],
            },
            run_id="run-test-materialize-order",
            staging_dir=git_project / "staging-materialize-order",
            runner=subprocess.run,
        )
        return call_order, materialize_args, hard_failure, errors

    def test_materialize_runs_before_snapshot_and_again_after_candidate(
        self, git_project: Path, monkeypatch
    ) -> None:
        """Issue #340: snapshot 前 materialize → 候補実行 → 再 materialize の順序を検証する。

        1回目（ベースライン整合）は `run_headless_scenario`（内部で isolated git snapshot の
        ベースラインコミットを作成する）より前、2回目（改ざん対策の復元）は候補実行の後で
        なければならない。
        """
        call_order, _materialize_args, hard_failure, errors = (
            self._run_lifecycle_with_tracked_calls(git_project, monkeypatch)
        )

        assert hard_failure is False, errors
        assert call_order == [
            "snapshot_trusted_fixtures",
            "materialize_fixtures",
            "run_headless_scenario",
            "materialize_fixtures",
        ]

    def test_tamper_restore_materialize_still_runs_after_headless_scenario(
        self, git_project: Path, monkeypatch
    ) -> None:
        """改ざん対策（PR #326 round 4/5）の維持: snapshot 前 materialize を追加しても、
        候補実行後・oracle 実行前の再 materialize が引き続き存在すること。両呼び出しとも
        信頼済みハーネスの `package_dir` を source に取ること。"""
        call_order, materialize_args, hard_failure, errors = self._run_lifecycle_with_tracked_calls(
            git_project, monkeypatch
        )

        assert hard_failure is False, errors
        scenario_index = call_order.index("run_headless_scenario")
        assert "materialize_fixtures" in call_order[scenario_index + 1 :]
        # 両 materialize とも attempt 開始時に固定した同一の immutable copy を source に取る
        # （TOCTOU 対策。信頼済み package_dir を都度読まない）。
        assert [args[1] for args in materialize_args] == [
            self._TRUSTED_COPY_SENTINEL,
            self._TRUSTED_COPY_SENTINEL,
        ]


class TestPreSnapshotMaterializeBaselineIntegration:
    """Issue #340 の実 git 統合回帰テスト: 「snapshot 前 materialize → ベースラインコミット →
    noop 候補 → 再 materialize」の後、collateral-scope oracle が見る `git diff HEAD` に
    fixture 差分が現れないこと。旧 source_commit の checkout を「stale fixture が tracked な
    git repo」で模し、ベースラインコミットは `scenario_isolation._prepare_isolated_git` と
    同じ `git add --all` + commit で模す。"""

    _STALE_FIXTURE = "# stale fixture without add-phase-with-ac\n"
    _CURRENT_FIXTURE = "# current trusted fixture\n"

    def _make_trusted_package_dir(self, tmp_path: Path) -> Path:
        package_dir = tmp_path / "package"
        (package_dir / "scenarios" / "fixtures").mkdir(parents=True)
        (package_dir / "scenarios" / "fixtures" / "assert-task-state-outcome.py").write_text(
            self._CURRENT_FIXTURE, encoding="utf-8"
        )
        return package_dir

    def _make_stale_worktree(self, tmp_path: Path) -> tuple[Path, Path]:
        """stale fixture（変更 + 現行に無いファイル）が tracked な git repo を作る。"""
        worktree_dir = tmp_path / "worktree"
        fixture_dir = worktree_dir / "packages" / "meta-harness" / "scenarios" / "fixtures"
        fixture_dir.mkdir(parents=True)
        (fixture_dir / "assert-task-state-outcome.py").write_text(
            self._STALE_FIXTURE, encoding="utf-8"
        )
        (fixture_dir / "removed-in-current.py").write_text(
            "# stale-only file removed in current harness\n", encoding="utf-8"
        )
        _git("init", cwd=worktree_dir)
        _git("config", "user.email", "test@example.com", cwd=worktree_dir)
        _git("config", "user.name", "test", cwd=worktree_dir)
        _git("add", "--all", cwd=worktree_dir)
        _git("commit", "-m", "source_commit checkout (stale fixtures)", cwd=worktree_dir)
        return worktree_dir, fixture_dir

    def _snapshot_baseline(self, worktree_dir: Path) -> None:
        """`_prepare_isolated_git` のベースライン確定（`git add --all` + commit）を模す。"""
        _git("add", "--all", cwd=worktree_dir)
        _git("commit", "--allow-empty", "-m", "scenario baseline", cwd=worktree_dir)

    def _tracked_diff_names(self, worktree_dir: Path) -> str:
        return _git("diff", "--name-status", "HEAD", cwd=worktree_dir).stdout.strip()

    def _freeze_trusted(self, package_dir: Path, tmp_path: Path) -> Path:
        return ev._snapshot_trusted_oracle_fixtures(package_dir, tmp_path / "staging")

    def test_collateral_diff_stays_empty_for_noop_candidate(self, tmp_path: Path) -> None:
        package_dir = self._make_trusted_package_dir(tmp_path)
        worktree_dir, fixture_dir = self._make_stale_worktree(tmp_path)

        trusted = self._freeze_trusted(package_dir, tmp_path)
        ev._materialize_current_oracle_fixtures(worktree_dir, trusted)  # snapshot 前
        self._snapshot_baseline(worktree_dir)
        # noop 候補（何も変更しない）
        ev._materialize_current_oracle_fixtures(worktree_dir, trusted)  # oracle 直前の復元

        assert self._tracked_diff_names(worktree_dir) == ""
        materialized = fixture_dir / "assert-task-state-outcome.py"
        assert materialized.read_text(encoding="utf-8") == self._CURRENT_FIXTURE

    def test_without_pre_snapshot_materialize_diff_reproduces_issue_340(
        self, tmp_path: Path
    ) -> None:
        """counterfactual: snapshot 前 materialize を省くと、oracle 直前の復元自体が
        tracked 差分になり noop 候補でも collateral 検出される（Issue #340 の再現）。"""
        package_dir = self._make_trusted_package_dir(tmp_path)
        worktree_dir, _fixture_dir = self._make_stale_worktree(tmp_path)

        self._snapshot_baseline(worktree_dir)  # 旧 fixture のままベースライン確定
        trusted = self._freeze_trusted(package_dir, tmp_path)
        ev._materialize_current_oracle_fixtures(worktree_dir, trusted)

        diff_names = self._tracked_diff_names(worktree_dir)
        assert "assert-task-state-outcome.py" in diff_names
        assert "removed-in-current.py" in diff_names

    def test_candidate_tamper_is_restored_but_collateral_change_stays_visible(
        self, tmp_path: Path
    ) -> None:
        """候補が fixture を改ざん（変更/追加）しても oracle 判定前に信頼済み内容へ復元されて
        diff に現れず、fixture 外への変更は引き続き tracked 差分として検出されること。"""
        package_dir = self._make_trusted_package_dir(tmp_path)
        worktree_dir, fixture_dir = self._make_stale_worktree(tmp_path)
        tracked_outside = worktree_dir / "README.md"
        tracked_outside.write_text("original\n", encoding="utf-8")
        _git("add", "--all", cwd=worktree_dir)
        _git("commit", "-m", "add tracked file outside fixtures", cwd=worktree_dir)

        trusted = self._freeze_trusted(package_dir, tmp_path)
        ev._materialize_current_oracle_fixtures(worktree_dir, trusted)
        self._snapshot_baseline(worktree_dir)
        # 候補による改ざん（fixture の書き換え + 追加）と fixture 外の正当な collateral 変更
        (fixture_dir / "assert-task-state-outcome.py").write_text(
            "# tampered: always exit 0\n", encoding="utf-8"
        )
        (fixture_dir / "planted-by-candidate.py").write_text("# planted\n", encoding="utf-8")
        tracked_outside.write_text("modified by candidate\n", encoding="utf-8")
        ev._materialize_current_oracle_fixtures(worktree_dir, trusted)

        diff_names = self._tracked_diff_names(worktree_dir)
        assert "assert-task-state-outcome.py" not in diff_names
        assert "planted-by-candidate.py" not in diff_names
        assert "README.md" in diff_names
        materialized = fixture_dir / "assert-task-state-outcome.py"
        assert materialized.read_text(encoding="utf-8") == self._CURRENT_FIXTURE

    def test_trusted_source_update_between_materializes_does_not_leak_into_diff(
        self, tmp_path: Path
    ) -> None:
        """TOCTOU 対策の統合検証: 2 回の materialize の間に信頼済み source（package_dir）が
        外部更新されても、両者は attempt 開始時の immutable copy を参照するため、noop 候補の
        collateral diff は空のまま変わらないこと。"""
        package_dir = self._make_trusted_package_dir(tmp_path)
        worktree_dir, fixture_dir = self._make_stale_worktree(tmp_path)

        trusted = self._freeze_trusted(package_dir, tmp_path)
        ev._materialize_current_oracle_fixtures(worktree_dir, trusted)
        self._snapshot_baseline(worktree_dir)
        # attempt 実行中の外部更新（fixture 改修コミット等）を模す
        (package_dir / "scenarios" / "fixtures" / "assert-task-state-outcome.py").write_text(
            "# updated while attempt is running\n", encoding="utf-8"
        )
        ev._materialize_current_oracle_fixtures(worktree_dir, trusted)

        assert self._tracked_diff_names(worktree_dir) == ""
        materialized = fixture_dir / "assert-task-state-outcome.py"
        assert materialized.read_text(encoding="utf-8") == self._CURRENT_FIXTURE


class TestFinallyRemovalOnLifecycleFailure:
    """EV-14: worktree は評価の成功・失敗に関わらず finally で確実に除去される。"""

    def test_worktree_removed_even_when_build_step_fails(
        self, git_project: Path, monkeypatch
    ) -> None:
        def failing_build(worktree_dir, **_kwargs):
            raise ev.EvaluatorStageError("build", "build_error", "forced failure for test")

        monkeypatch.setattr(ev, "build_facet_and_context", failing_build)

        cand_dir = git_project / "cand"
        (cand_dir / "overlay" / "facets" / "example-facet").mkdir(parents=True)
        (cand_dir / "overlay" / "facets" / "example-facet" / "SKILL.md").write_text(
            "# example\n", encoding="utf-8"
        )

        scenario = {
            "id": "s1",
            "prompt": "irrelevant",
            "setup": [],
            "critical": [],
            "checks": [],
        }
        checks, checks_non_critical, hard_failure, errors = ev._run_attempt_lifecycle(
            main_root=git_project,
            config={"evaluate": {"worktree_root": ".worktrees/meta"}},
            schema_dir=Path("packages/meta-harness/schemas").resolve(),
            package_dir=Path("packages/meta-harness").resolve(),
            cand_dir=cand_dir,
            manifest={"source_commit": _git("rev-parse", "HEAD", cwd=git_project).stdout.strip()},
            scenario=scenario,
            run_id="run-test-finally",
            staging_dir=git_project / "staging",
            runner=subprocess.run,
        )

        assert hard_failure is True
        assert errors[0]["stage"] == "build"
        assert errors[0]["type"] == "build_error"
        assert checks == []
        assert checks_non_critical == []
        # worktree ディレクトリが finally で除去され、残っていないこと
        root = git_project / ".worktrees" / "meta"
        assert not (root / "wt-run-test-finally").exists()

    def test_worktree_removed_even_on_unexpected_exception(
        self, git_project: Path, monkeypatch
    ) -> None:
        def raising_apply_overlay(overlay_dir, config, worktree_dir, schema_dir, **_kwargs):
            raise RuntimeError("totally unexpected error")

        monkeypatch.setattr(ev, "apply_overlay", raising_apply_overlay)

        scenario = {"id": "s2", "prompt": "irrelevant", "setup": [], "critical": [], "checks": []}
        _checks, _checks_nc, hard_failure, errors = ev._run_attempt_lifecycle(
            main_root=git_project,
            config={"evaluate": {"worktree_root": ".worktrees/meta"}},
            schema_dir=Path("packages/meta-harness/schemas").resolve(),
            package_dir=Path("packages/meta-harness").resolve(),
            cand_dir=git_project,
            manifest={"source_commit": _git("rev-parse", "HEAD", cwd=git_project).stdout.strip()},
            scenario=scenario,
            run_id="run-test-unexpected",
            staging_dir=git_project / "staging2",
            runner=subprocess.run,
        )

        assert hard_failure is True
        assert errors[0]["stage"] == "unknown"
        assert errors[0]["type"] == "run_error"
        root = git_project / ".worktrees" / "meta"
        assert not (root / "wt-run-test-unexpected").exists()

    def test_worktree_removed_even_when_isolation_cleanup_fails(
        self, git_project: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(ev, "apply_overlay", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(ev, "build_facet_and_context", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(ev, "run_setup_commands", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(
            ev,
            "run_headless_scenario",
            lambda *_args, **_kwargs: SimpleNamespace(isolation_launch=object()),
        )

        def failing_cleanup(_launch) -> None:
            raise RuntimeError("forced cleanup failure")

        monkeypatch.setattr(ev.siso, "cleanup_scenario_isolation", failing_cleanup)

        _checks, _checks_nc, hard_failure, errors = ev._run_attempt_lifecycle(
            main_root=git_project,
            config={"evaluate": {"worktree_root": ".worktrees/meta"}},
            schema_dir=_SCHEMA_DIR,
            package_dir=Path("packages/meta-harness").resolve(),
            cand_dir=git_project,
            manifest={"source_commit": _git("rev-parse", "HEAD", cwd=git_project).stdout.strip()},
            scenario={
                "id": "s3",
                "prompt": "irrelevant",
                "setup": [],
                "critical": [],
                "checks": [],
            },
            run_id="run-test-cleanup-failure",
            staging_dir=git_project / "staging3",
            runner=subprocess.run,
        )

        assert hard_failure is True
        assert errors == [
            {
                "stage": "isolation_cleanup",
                "type": "cleanup_error",
                "message": "forced cleanup failure",
            }
        ]
        root = git_project / ".worktrees" / "meta"
        assert not (root / "wt-run-test-cleanup-failure").exists()


class TestApplyOverlayReRejectsUnsafeOverlaysAtEvaluateTime:
    """EV-31: overlay 拒否は register 時だけでなく evaluate 時にも再検証される（defense in depth）。

    register 側の検証をすり抜けたケース（register 後の allowlist 変更・レースコンディション等）
    を模し、`apply_overlay` 単体が worktree 適用直前に安全制約を再検証することを確認する。
    """

    _CONFIG: dict = {}  # overlay allowed_prefixes/denied_prefixes は既定値を使う

    def test_rejects_absolute_path_entry(self, git_project: Path, tmp_path: Path) -> None:
        overlay_dir = tmp_path / "overlay"
        (overlay_dir / "facets" / "x").mkdir(parents=True)
        (overlay_dir / "facets" / "x" / "SKILL.md").write_text("ok", encoding="utf-8")
        # validate_overlay は overlay_dir 配下のファイルを rglob するため、絶対パス違反は
        # ファイル名自体では表現できない。ここでは禁止 prefix 違反で同じ経路を検証する。
        (overlay_dir / "packages").mkdir(parents=True, exist_ok=True)
        (overlay_dir / "packages" / "meta-harness").mkdir(parents=True, exist_ok=True)
        (overlay_dir / "packages" / "meta-harness" / "evil.py").write_text("x", encoding="utf-8")

        worktree_dir = git_project  # apply_overlay は検証失敗時 worktree に触れる前に例外を出す
        try:
            ev.apply_overlay(
                overlay_dir,
                self._CONFIG,
                worktree_dir,
                _SCHEMA_DIR,
                target="claude-harness",
            )
        except ev.EvaluatorStageError as exc:
            assert exc.stage == "overlay_apply"
            assert exc.error_type == "overlay_error"
        else:
            raise AssertionError(
                "overlay touching a denied prefix must be rejected at evaluate time"
            )

    def test_rejects_path_outside_allowed_prefixes(self, git_project: Path, tmp_path: Path) -> None:
        overlay_dir = tmp_path / "overlay2"
        (overlay_dir / "not-facets").mkdir(parents=True)
        (overlay_dir / "not-facets" / "file.txt").write_text("x", encoding="utf-8")

        try:
            ev.apply_overlay(
                overlay_dir,
                self._CONFIG,
                git_project,
                _SCHEMA_DIR,
                target="claude-harness",
            )
        except ev.EvaluatorStageError as exc:
            assert exc.stage == "overlay_apply"
        else:
            raise AssertionError(
                "overlay outside allowed_prefixes must be rejected at evaluate time"
            )

    def test_valid_overlay_is_applied_without_error(
        self, git_project: Path, tmp_path: Path
    ) -> None:
        overlay_dir = tmp_path / "overlay3"
        (overlay_dir / "facets" / "example-facet").mkdir(parents=True)
        (overlay_dir / "facets" / "example-facet" / "SKILL.md").write_text(
            "# example\n", encoding="utf-8"
        )

        ev.apply_overlay(
            overlay_dir,
            self._CONFIG,
            git_project,
            _SCHEMA_DIR,
            target="claude-harness",
        )

        assert (git_project / "facets" / "example-facet" / "SKILL.md").read_text(
            encoding="utf-8"
        ) == "# example\n"


class TestRoutingConfigPatchMaterialization:
    @staticmethod
    def _write_patch_overlay(base: Path, patch: list[dict]) -> Path:
        base.mkdir(parents=True)
        (base / ev.mh.CONFIG_PATCH_FILENAME).write_text(
            json.dumps(patch),
            encoding="utf-8",
        )
        return base

    def test_writes_and_deep_merges_worktree_local_yaml(self, tmp_path: Path) -> None:
        overlay_dir = tmp_path / "overlay"
        overlay_dir.mkdir()
        (overlay_dir / "config-patch.json").write_text(
            json.dumps(
                [
                    {
                        "file": "agent-routing/cli-tools.yaml",
                        "key_path": "codex.model",
                        "value": "gpt-5.6-sol",
                    },
                    {
                        "file": "agent-routing/cli-tools.yaml",
                        "key_path": "agents.debugger.tool",
                        "value": "auto",
                    },
                ]
            ),
            encoding="utf-8",
        )
        worktree_dir = tmp_path / "worktree"
        local_path = worktree_dir / ".claude/config/agent-routing/cli-tools.local.yaml"
        local_path.parent.mkdir(parents=True)
        local_path.write_text("codex:\n  enabled: false\n", encoding="utf-8")
        # R3-5: `hook_common.load_cli_tools_config` は `.claude/config/agent-routing/
        # cli-tools.yaml`(base)が無いと `{}` を返す(`find_package_config` の
        # project-local レイヤーで見つからなければ、後段の AI_ORCHESTRA_DIR フォール
        # バック経由で「たまたま」実リポジトリの base config を拾うだけになり、
        # `AI_ORCHESTRA_DIR` 未設定・別プロジェクト実行では `KeyError` になる)。worktree
        # 内に base config 自体も明示的に seed し、リポジトリ状態や env var に依存しない
        # hermetic なテストにする。
        base_config_path = worktree_dir / ".claude/config/agent-routing/cli-tools.yaml"
        base_config_path.write_text(
            Path("packages/agent-routing/config/cli-tools.yaml")
            .resolve()
            .read_text(encoding="utf-8"),
            encoding="utf-8",
        )

        ev.apply_overlay(
            overlay_dir,
            {},
            worktree_dir,
            _SCHEMA_DIR,
            target="routing-config",
            created_by="human",
        )

        assert yaml.safe_load(local_path.read_text(encoding="utf-8")) == {
            "agents": {"debugger": {"tool": "auto"}},
            "codex": {"enabled": False, "model": "gpt-5.6-sol"},
        }
        assert local_path.read_text(encoding="utf-8").startswith("agents:\n")
        merged = hook_common.load_cli_tools_config(str(worktree_dir))
        assert merged["codex"]["enabled"] is False
        assert merged["codex"]["model"] == "gpt-5.6-sol"
        assert "sandbox" in merged["codex"]
        assert merged["agents"]["debugger"]["tool"] == "auto"
        assert not (overlay_dir / ".claude/config/agent-routing/cli-tools.local.yaml").exists()

    def test_missing_created_by_is_rejected_for_routing_config_patch(self, tmp_path: Path) -> None:
        overlay_dir = self._write_patch_overlay(
            tmp_path / "overlay-missing-creator",
            [
                {
                    "file": "agent-routing/cli-tools.yaml",
                    "key_path": "codex.model",
                    "value": "gpt-5.3-codex",
                }
            ],
        )
        worktree_dir = tmp_path / "worktree-missing-creator"
        worktree_dir.mkdir()

        with pytest.raises(ev.EvaluatorStageError, match="created_by='' is not allowed"):
            ev.apply_registered_candidate_overlay(
                main_root=tmp_path,
                config={},
                manifest={"target": "routing-config"},
                overlay_dir=overlay_dir,
                worktree_dir=worktree_dir,
                schema_dir=_SCHEMA_DIR,
            )

        assert not (worktree_dir / ".claude/config/agent-routing/cli-tools.local.yaml").exists()

    def test_preplanted_old_style_tmp_symlink_is_not_followed(self, tmp_path: Path) -> None:
        overlay_dir = self._write_patch_overlay(
            tmp_path / "overlay-symlink-guard",
            [
                {
                    "file": "agent-routing/cli-tools.yaml",
                    "key_path": "codex.model",
                    "value": "gpt-5.6-sol",
                }
            ],
        )
        worktree_dir = tmp_path / "worktree-symlink-guard"
        local_path = worktree_dir / ".claude/config/agent-routing/cli-tools.local.yaml"
        local_path.parent.mkdir(parents=True)
        sentinel = tmp_path / "outside-sentinel.txt"
        sentinel.write_text("unchanged\n", encoding="utf-8")
        old_tmp_path = local_path.with_name(f".{local_path.name}.tmp-{ev.os.getpid()}")
        old_tmp_path.symlink_to(sentinel)

        ev.apply_overlay(
            overlay_dir,
            {},
            worktree_dir,
            _SCHEMA_DIR,
            target="routing-config",
            created_by="human",
        )

        assert sentinel.read_text(encoding="utf-8") == "unchanged\n"
        assert old_tmp_path.is_symlink()
        assert yaml.safe_load(local_path.read_text(encoding="utf-8"))["codex"]["model"] == (
            "gpt-5.6-sol"
        )

    def test_symlinked_applied_patch_parent_directory_is_rejected(self, tmp_path: Path) -> None:
        """R3-2: `O_NOFOLLOW` は一時ファイル自身(最終コンポーネント)しか保護しない。
        `applied-config-patch.json` の親ディレクトリ `.claude/meta-harness` が worktree
        外を指す symlink に差し替えられていても、その外へ書き込んではならない。"""
        overlay_dir = self._write_patch_overlay(
            tmp_path / "overlay-meta-harness-symlink-guard",
            [
                {
                    "file": "agent-routing/cli-tools.yaml",
                    "key_path": "codex.model",
                    "value": "gpt-5.6-sol",
                }
            ],
        )
        worktree_dir = tmp_path / "worktree-meta-harness-symlink-guard"
        (worktree_dir / ".claude").mkdir(parents=True)
        outside_dir = tmp_path / "outside-meta-harness-target"
        outside_dir.mkdir()
        (worktree_dir / ".claude" / "meta-harness").symlink_to(outside_dir)

        with pytest.raises(ev.EvaluatorStageError, match="escapes worktree"):
            ev.apply_overlay(
                overlay_dir,
                {},
                worktree_dir,
                _SCHEMA_DIR,
                target="routing-config",
                created_by="human",
            )

        assert list(outside_dir.iterdir()) == []

    def test_tmp_file_is_removed_when_atomic_replace_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        overlay_dir = self._write_patch_overlay(
            tmp_path / "overlay-replace-failure",
            [
                {
                    "file": "agent-routing/cli-tools.yaml",
                    "key_path": "codex.model",
                    "value": "gpt-5.6-sol",
                }
            ],
        )
        worktree_dir = tmp_path / "worktree-replace-failure"
        local_path = worktree_dir / ".claude/config/agent-routing/cli-tools.local.yaml"

        def fail_replace(_source: Path, _destination: Path) -> None:
            raise OSError("forced replace failure")

        monkeypatch.setattr(ev.os, "replace", fail_replace)

        with pytest.raises(OSError, match="forced replace failure"):
            ev.apply_overlay(
                overlay_dir,
                {},
                worktree_dir,
                _SCHEMA_DIR,
                target="routing-config",
                created_by="human",
            )

        assert list(local_path.parent.glob(f".{local_path.name}.tmp-*")) == []

    def test_explicit_null_intermediate_key_is_rejected(self, tmp_path: Path) -> None:
        overlay_dir = self._write_patch_overlay(
            tmp_path / "overlay-null-collision",
            [
                {
                    "file": "agent-routing/cli-tools.yaml",
                    "key_path": "codex.model",
                    "value": "gpt-5.6-sol",
                }
            ],
        )
        worktree_dir = tmp_path / "worktree-null-collision"
        local_path = worktree_dir / ".claude/config/agent-routing/cli-tools.local.yaml"
        local_path.parent.mkdir(parents=True)
        local_path.write_text("codex: null\n", encoding="utf-8")

        with pytest.raises(ev.EvaluatorStageError, match="key collides with scalar"):
            ev.apply_overlay(
                overlay_dir,
                {},
                worktree_dir,
                _SCHEMA_DIR,
                target="routing-config",
                created_by="human",
            )

        assert local_path.read_text(encoding="utf-8") == "codex: null\n"

    def test_child_patch_overrides_parent_patch_for_same_key(self, tmp_path: Path) -> None:
        main_root = tmp_path / "main"
        source_commit = "a" * 40

        def register_patch_candidate(
            cand_id: str, parent_id: str | None, generation: int, value: str
        ) -> dict:
            overlay_dir = self._write_patch_overlay(
                tmp_path / f"overlay-{cand_id}",
                [
                    {
                        "file": "agent-routing/cli-tools.yaml",
                        "key_path": "agents.debugger.tool",
                        "value": value,
                    }
                ],
            )
            patch = ev.mh.read_config_patch_file(overlay_dir / ev.mh.CONFIG_PATCH_FILENAME)
            manifest = ev.mh.build_candidate_manifest(
                cand_id=cand_id,
                parent_id=parent_id,
                generation=generation,
                target="routing-config",
                source_commit=source_commit,
                config_hash=ev.mh.compute_config_hash(overlay_dir, {}),
                overlay_files=[],
                description=f"set debugger tool to {value}",
                created_by="human",
                config_patch_hash=ev.mh.compute_config_patch_hash(patch),
            )
            ev.mh.register_candidate(
                main_root,
                {},
                cand_id=cand_id,
                manifest=manifest,
                overlay_dir=overlay_dir,
                overlay_files=[],
                target="routing-config",
                created_by="human",
                schema_dir=_SCHEMA_DIR,
            )
            return manifest

        parent_id = "cand-20260716-120000-parent-abcd"
        child_id = "cand-20260716-120001-child-abcd"
        register_patch_candidate(parent_id, None, 0, "codex")
        child_manifest = register_patch_candidate(child_id, parent_id, 1, "claude-direct")
        worktree_dir = tmp_path / "worktree-lineage"
        worktree_dir.mkdir()

        ev.apply_registered_candidate_overlay(
            main_root=main_root,
            config={},
            manifest=child_manifest,
            worktree_dir=worktree_dir,
            schema_dir=_SCHEMA_DIR,
        )

        local_path = worktree_dir / ".claude/config/agent-routing/cli-tools.local.yaml"
        assert yaml.safe_load(local_path.read_text(encoding="utf-8"))["agents"]["debugger"] == {
            "tool": "claude-direct"
        }

    def test_mixed_overlay_is_rejected_before_worktree_changes(self, tmp_path: Path) -> None:
        overlay_dir = tmp_path / "overlay-mixed"
        (overlay_dir / "facets/example").mkdir(parents=True)
        (overlay_dir / "facets/example/SKILL.md").write_text("changed", encoding="utf-8")
        (overlay_dir / "config-patch.json").write_text(
            json.dumps(
                [
                    {
                        "file": "agent-routing/cli-tools.yaml",
                        "key_path": "codex.model",
                        "value": "gpt-5.3-codex",
                    }
                ]
            ),
            encoding="utf-8",
        )
        worktree_dir = tmp_path / "worktree-mixed"
        worktree_dir.mkdir()

        try:
            ev.apply_overlay(
                overlay_dir,
                {},
                worktree_dir,
                _SCHEMA_DIR,
                target="routing-config",
                created_by="human",
            )
        except ev.EvaluatorStageError as exc:
            assert "must not contain file overlays" in str(exc)
        else:
            raise AssertionError("mixed config patch and file overlay must be rejected")

        assert not (worktree_dir / "facets/example/SKILL.md").exists()
        assert not (worktree_dir / ".claude/config/agent-routing/cli-tools.local.yaml").exists()

    @pytest.mark.parametrize(
        ("target", "with_patch", "expected"),
        [
            ("routing-config", False, "require a non-empty config patch"),
            ("claude-harness", True, "require target='routing-config'"),
        ],
    )
    def test_target_patch_biconditional_is_rechecked_before_writes(
        self,
        tmp_path: Path,
        target: str,
        with_patch: bool,
        expected: str,
    ) -> None:
        overlay_dir = tmp_path / f"overlay-{target}"
        overlay_dir.mkdir()
        if with_patch:
            (overlay_dir / ev.mh.CONFIG_PATCH_FILENAME).write_text(
                json.dumps(
                    [
                        {
                            "file": "agent-routing/cli-tools.yaml",
                            "key_path": "codex.model",
                            "value": "gpt-5.3-codex",
                        }
                    ]
                ),
                encoding="utf-8",
            )
        worktree_dir = tmp_path / f"worktree-{target}"
        worktree_dir.mkdir()

        with pytest.raises(ev.EvaluatorStageError, match=expected):
            ev.apply_overlay(
                overlay_dir,
                {},
                worktree_dir,
                _SCHEMA_DIR,
                target=target,
                created_by="human",
            )

        assert list(worktree_dir.iterdir()) == []

    @pytest.mark.parametrize("mutation", ["tamper", "delete"])
    def test_changed_registered_sidecar_is_rejected_before_worktree_changes(
        self, tmp_path: Path, mutation: str
    ) -> None:
        main_root = tmp_path / "main"
        cand_id = "cand-20260716-120000-routing-abcd"
        overlay_dir = ev.mh.candidates_dir(main_root, {}) / cand_id / "overlay"
        overlay_dir.mkdir(parents=True)
        patch = [
            {
                "file": "agent-routing/cli-tools.yaml",
                "key_path": "codex.model",
                "value": "gpt-5.3-codex",
            }
        ]
        patch_path = overlay_dir / ev.mh.CONFIG_PATCH_FILENAME
        patch_path.write_text(json.dumps(patch), encoding="utf-8")
        manifest = {
            "cand_id": cand_id,
            "parent_id": None,
            "target": "routing-config",
            "created_by": "human",
            "source_commit": "a" * 40,
            "overlay_files": [],
            "config_hash": ev.mh.compute_config_hash(overlay_dir, {}),
            "config_patch_hash": ev.mh.compute_config_patch_hash(patch),
        }
        if mutation == "tamper":
            patch[0]["value"] = "tampered-model"
            patch_path.write_text(json.dumps(patch), encoding="utf-8")
        else:
            patch_path.unlink()
        worktree_dir = tmp_path / "worktree-tampered"
        worktree_dir.mkdir()

        with pytest.raises(ev.EvaluatorStageError, match=r"hash mismatch|sidecar is missing"):
            ev.apply_registered_candidate_overlay(
                main_root=main_root,
                config={},
                manifest=manifest,
                worktree_dir=worktree_dir,
                schema_dir=_SCHEMA_DIR,
            )

        assert not (worktree_dir / ".claude/config/agent-routing/cli-tools.local.yaml").exists()

"""worktree ライフサイクルのテスト（EV-14, EV-31, Sec2-1）。

`git` は実プロセス（既定 runner=subprocess.run）で操作する（claude/codex は使わない）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from tests.module_loader import load_module

ev = load_module(
    "meta_harness_evaluator_worktree_lifecycle",
    "packages/meta-harness/lib/evaluator.py",
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

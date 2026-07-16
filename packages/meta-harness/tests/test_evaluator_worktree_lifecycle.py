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
                        "value": "gpt-5.3-codex",
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
            "codex": {"enabled": False, "model": "gpt-5.3-codex"},
        }
        assert local_path.read_text(encoding="utf-8").startswith("agents:\n")
        merged = hook_common.load_cli_tools_config(str(worktree_dir))
        assert merged["codex"]["enabled"] is False
        assert merged["codex"]["model"] == "gpt-5.3-codex"
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

        with pytest.raises(ev.EvaluatorStageError, match="created_by='human'"):
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
                    "value": "gpt-5.3-codex",
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
            "gpt-5.3-codex"
        )

    def test_tmp_file_is_removed_when_atomic_replace_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        overlay_dir = self._write_patch_overlay(
            tmp_path / "overlay-replace-failure",
            [
                {
                    "file": "agent-routing/cli-tools.yaml",
                    "key_path": "codex.model",
                    "value": "gpt-5.3-codex",
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
                    "value": "gpt-5.3-codex",
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

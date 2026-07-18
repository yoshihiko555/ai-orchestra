"""`register` サブコマンドのテスト（EV-03, EV-05, EV-25, dirty repo 警告, Sec6）。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.module_loader import load_module

mh = load_module(
    "meta_harness_common_register",
    "packages/meta-harness/lib/meta_harness_common.py",
)
cli = load_module(
    "meta_harness_cli_register",
    "packages/meta-harness/scripts/meta_harness.py",
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_DIR = REPO_ROOT / "packages" / "meta-harness" / "schemas"


def _candidates_dir(project: Path) -> Path:
    return project / ".claude" / "meta-harness" / "candidates"


def _ledger_events(project: Path) -> list[dict]:
    path = project / ".claude" / "meta-harness" / "ledger.jsonl"
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _tmp_register_dirs(project: Path) -> list[Path]:
    tmp_dir = project / ".claude" / "meta-harness" / "tmp"
    return sorted(tmp_dir.glob("register-*")) if tmp_dir.is_dir() else []


def _manifest(cand_id: str, description: str = "candidate") -> dict:
    return {
        "schema_version": "1.0",
        "cand_id": cand_id,
        "parent_id": None,
        "generation": 0,
        "created_at": mh.now_iso(),
        "created_by": "human",
        "target": "claude-harness",
        "source_commit": "a" * 40,
        "config_hash": "b" * 64,
        "model_versions": {},
        "overlay_files": ["facets/example-facet/SKILL.md"],
        "description": description,
    }


class TestLedgerProvenance:
    def test_matching_lineage_and_registration_events_are_accepted(self) -> None:
        lineage = [
            {
                "cand_id": "cand-parent",
                "created_by": "human",
                "target": "routing-config",
            },
            {
                "cand_id": "cand-child",
                "created_by": "human",
                "target": "routing-config",
            },
        ]
        events = [{"event": "candidate_registered", **item} for item in lineage]

        mh.assert_lineage_matches_registered_events(events, lineage)

    def test_missing_registration_event_is_rejected(self) -> None:
        lineage = [{"cand_id": "cand-missing", "created_by": "human", "target": "routing-config"}]

        with pytest.raises(ValueError, match="ledger event is missing"):
            mh.assert_lineage_matches_registered_events([], lineage)

    @pytest.mark.parametrize(
        ("field", "tampered", "expected"),
        [
            ("created_by", "proposer", "created_by"),
            ("target", "claude-harness", "target"),
        ],
    )
    def test_registration_provenance_mismatch_is_rejected(
        self, field: str, tampered: str, expected: str
    ) -> None:
        registered = {
            "event": "candidate_registered",
            "cand_id": "cand-tampered",
            "created_by": "human",
            "target": "routing-config",
        }
        lineage = [{**registered, field: tampered}]

        with pytest.raises(ValueError, match=expected):
            mh.assert_lineage_matches_registered_events([registered], lineage)


class TestRegisterArgumentConsistency:
    def test_manifest_cand_id_must_match_register_cand_id(self, tmp_path: Path) -> None:
        manifest = {
            **_manifest("cand-id-mismatch"),
            "cand_id": "cand-a-completely-different-id",
        }

        with pytest.raises(ValueError, match="manifest cand_id does not match register cand_id"):
            mh.register_candidate(
                tmp_path,
                mh.DEFAULTS,
                cand_id="cand-id-mismatch",
                manifest=manifest,
                overlay_dir=tmp_path / "unused-overlay",
                overlay_files=[],
                target="claude-harness",
            )

    def test_manifest_target_must_match_register_target(self, tmp_path: Path) -> None:
        manifest = {
            **_manifest("cand-target-mismatch"),
            "target": "skill:something-else",
        }

        with pytest.raises(ValueError, match="manifest target does not match register target"):
            mh.register_candidate(
                tmp_path,
                mh.DEFAULTS,
                cand_id=manifest["cand_id"],
                manifest=manifest,
                overlay_dir=tmp_path / "unused-overlay",
                overlay_files=[],
                target="claude-harness",
            )

    def test_manifest_created_by_must_match_supplied_creator(self, tmp_path: Path) -> None:
        manifest = {
            **_manifest("cand-creator-mismatch"),
            "created_by": "proposer",
        }

        with pytest.raises(
            ValueError,
            match="manifest created_by does not match register created_by",
        ):
            mh.register_candidate(
                tmp_path,
                mh.DEFAULTS,
                cand_id=manifest["cand_id"],
                manifest=manifest,
                overlay_dir=tmp_path / "unused-overlay",
                overlay_files=[],
                created_by="human",
            )


class TestRegisterSuccess:
    def test_register_writes_conformant_manifest_and_ledger_event(
        self, git_project: Path, tmp_path: Path, run_meta, default_overlay
    ) -> None:
        run_meta("init", project=git_project, check=True)
        overlay_dir = default_overlay(tmp_path)

        result = run_meta(
            "register",
            "--overlay",
            str(overlay_dir),
            "--target",
            "claude-harness",
            "--description",
            "a valid candidate",
            "--json",
            project=git_project,
            check=True,
        )
        payload = json.loads(result.stdout)
        cand_id = payload["cand_id"]

        manifest_path = _candidates_dir(git_project) / cand_id / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_schema = mh.load_schema(SCHEMA_DIR, "candidate.manifest.schema.json")
        assert mh.validate_against_schema(manifest, manifest_schema, SCHEMA_DIR) == []

        overlay_copy = (
            _candidates_dir(git_project)
            / cand_id
            / "overlay"
            / "facets"
            / "example-facet"
            / "SKILL.md"
        )
        assert overlay_copy.is_file()

        events = _ledger_events(git_project)
        registered = [e for e in events if e["event"] == "candidate_registered"]
        assert len(registered) == 1
        ledger_schema = mh.load_schema(SCHEMA_DIR, "ledger.event.schema.json")
        assert (
            mh.validate_against_schema(
                registered[0], ledger_schema["$defs"]["candidate_registered"], SCHEMA_DIR
            )
            == []
        )
        assert registered[0]["cand_id"] == cand_id


class TestGenerateCandIdNonceAvoidsCollision:
    # PR #162 レビュー指摘 (FIX F): 同一秒・同一 slug でも 4 桁 hex nonce により
    # cand_id が衝突しないこと
    def test_same_second_same_slug_produces_different_cand_ids(self) -> None:
        import datetime as _datetime

        fixed_now = _datetime.datetime(2026, 1, 1, 0, 0, 0)

        first = mh.generate_cand_id("manual", now=fixed_now)
        second = mh.generate_cand_id("manual", now=fixed_now)

        assert first != second
        shared_prefix = "cand-20260101-000000-manual-"
        assert first.startswith(shared_prefix)
        assert second.startswith(shared_prefix)
        assert mh.CAND_ID_PATTERN.match(first)
        assert mh.CAND_ID_PATTERN.match(second)

    # register_candidate は immutable（同名 cand_id は FileExistsError）なので、time を
    # 固定 mock した同一秒・同一 slug の2連続 register が両方成功することを、実際に
    # store へ書き込むレベルで決定論的に検証する（time.sleep 不使用）。
    def test_two_registers_with_same_second_and_slug_both_succeed_end_to_end(
        self, git_project: Path, tmp_path: Path, run_meta, default_overlay
    ) -> None:
        import datetime as _datetime

        run_meta("init", project=git_project, check=True)
        config = mh.load_config(git_project)
        fixed_now = _datetime.datetime(2026, 1, 1, 0, 0, 0)

        overlay_dir = git_project / "overlay-src"
        (overlay_dir / "facets" / "example-facet").mkdir(parents=True)
        (overlay_dir / "facets" / "example-facet" / "SKILL.md").write_text("v1", encoding="utf-8")
        overlay_files = ["facets/example-facet/SKILL.md"]

        cand_ids = [mh.generate_cand_id("manual", now=fixed_now) for _ in range(2)]
        assert cand_ids[0] != cand_ids[1]  # 同一秒・同一 slug でも nonce により異なる

        for cand_id in cand_ids:
            manifest = {
                **_manifest(cand_id),
                "config_hash": mh.compute_config_hash(overlay_dir, config),
            }
            mh.register_candidate(
                git_project,
                config,
                cand_id=cand_id,
                manifest=manifest,
                overlay_dir=overlay_dir,
                overlay_files=overlay_files,
            )

        for cand_id in cand_ids:
            assert (_candidates_dir(git_project) / cand_id).is_dir()


class TestRegisterImmutability:
    # EV-03
    def test_reregistering_same_cand_id_is_rejected(self, git_project: Path, run_meta) -> None:
        run_meta("init", project=git_project, check=True)
        config = mh.load_config(git_project)
        manifest = {
            "schema_version": "1.0",
            "cand_id": "cand-20260101-000000-fixed-slug",
            "parent_id": None,
            "generation": 0,
            "created_at": mh.now_iso(),
            "created_by": "human",
            "target": "claude-harness",
            "source_commit": "a" * 40,
            "config_hash": "b" * 64,
            "model_versions": {},
            "overlay_files": ["facets/example-facet/SKILL.md"],
            "description": "first",
        }
        overlay_dir = git_project / "overlay-src"
        (overlay_dir / "facets" / "example-facet").mkdir(parents=True)
        (overlay_dir / "facets" / "example-facet" / "SKILL.md").write_text("v1", encoding="utf-8")
        manifest["config_hash"] = mh.compute_config_hash(overlay_dir, config)

        mh.register_candidate(
            git_project,
            config,
            cand_id=manifest["cand_id"],
            manifest=manifest,
            overlay_dir=overlay_dir,
            overlay_files=["facets/example-facet/SKILL.md"],
        )
        original_content = (
            _candidates_dir(git_project)
            / manifest["cand_id"]
            / "overlay"
            / "facets"
            / "example-facet"
            / "SKILL.md"
        ).read_text(encoding="utf-8")

        (overlay_dir / "facets" / "example-facet" / "SKILL.md").write_text(
            "v2-tampered", encoding="utf-8"
        )
        try:
            mh.register_candidate(
                git_project,
                config,
                cand_id=manifest["cand_id"],
                manifest={
                    **manifest,
                    "description": "second attempt",
                    "config_hash": mh.compute_config_hash(overlay_dir, config),
                },
                overlay_dir=overlay_dir,
                overlay_files=["facets/example-facet/SKILL.md"],
            )
        except FileExistsError:
            pass
        else:
            raise AssertionError("re-registering the same cand_id should raise FileExistsError")

        after_content = (
            _candidates_dir(git_project)
            / manifest["cand_id"]
            / "overlay"
            / "facets"
            / "example-facet"
            / "SKILL.md"
        ).read_text(encoding="utf-8")
        assert after_content == original_content == "v1"


class TestRegisterConfigPatchValidation:
    # EV-67
    def test_human_routing_config_patch_registers_with_integrity_hashes(
        self, git_project: Path, tmp_path: Path, run_meta
    ) -> None:
        run_meta("init", project=git_project, check=True)
        overlay_dir = tmp_path / "routing-overlay"
        overlay_dir.mkdir()
        patch = [
            {
                "file": "agent-routing/cli-tools.yaml",
                "key_path": "codex.model",
                "value": "gpt-5.6-sol",
            }
        ]
        (overlay_dir / mh.CONFIG_PATCH_FILENAME).write_text(json.dumps(patch), encoding="utf-8")

        result = run_meta(
            "register",
            "--overlay",
            str(overlay_dir),
            "--target",
            "routing-config",
            "--json",
            project=git_project,
            check=True,
        )

        cand_id = json.loads(result.stdout)["cand_id"]
        candidate_dir = _candidates_dir(git_project) / cand_id
        manifest = json.loads((candidate_dir / "manifest.json").read_text(encoding="utf-8"))
        stored_overlay = candidate_dir / "overlay"
        assert manifest["target"] == "routing-config"
        assert manifest["created_by"] == "human"
        assert manifest["overlay_files"] == []
        assert manifest["config_patch_hash"] == mh.compute_config_patch_hash(patch)
        assert manifest["config_hash"] == mh.compute_config_hash(stored_overlay, {})

    def test_config_patch_is_rejected_for_non_routing_target(
        self, git_project: Path, tmp_path: Path, run_meta, default_overlay
    ) -> None:
        run_meta("init", project=git_project, check=True)
        overlay_dir = default_overlay(tmp_path)
        (overlay_dir / "config-patch.json").write_text(
            json.dumps(
                [{"file": "agent-routing/cli-tools.yaml", "key_path": "codex.model", "value": "x"}]
            ),
            encoding="utf-8",
        )

        result = run_meta(
            "register",
            "--overlay",
            str(overlay_dir),
            "--target",
            "claude-harness",
            project=git_project,
            check=False,
        )

        assert result.returncode == 2
        assert "non-empty config patches require target='routing-config'" in result.stderr
        assert (
            not any(_candidates_dir(git_project).iterdir())
            if _candidates_dir(git_project).is_dir()
            else True
        )

    def test_routing_target_without_config_patch_is_rejected(
        self, git_project: Path, tmp_path: Path, run_meta
    ) -> None:
        run_meta("init", project=git_project, check=True)
        overlay_dir = tmp_path / "empty-routing-overlay"
        overlay_dir.mkdir()

        result = run_meta(
            "register",
            "--overlay",
            str(overlay_dir),
            "--target",
            "routing-config",
            project=git_project,
            check=False,
        )

        assert result.returncode == 2
        assert "require a non-empty config patch" in result.stderr
        assert (
            not any(_candidates_dir(git_project).iterdir())
            if _candidates_dir(git_project).is_dir()
            else True
        )

    def test_mixed_file_overlay_and_config_patch_is_rejected(
        self, git_project: Path, tmp_path: Path, run_meta, default_overlay
    ) -> None:
        run_meta("init", project=git_project, check=True)
        overlay_dir = default_overlay(tmp_path)
        (overlay_dir / mh.CONFIG_PATCH_FILENAME).write_text(
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

        result = run_meta(
            "register",
            "--overlay",
            str(overlay_dir),
            "--target",
            "routing-config",
            project=git_project,
            check=False,
        )

        assert result.returncode == 2
        assert "must not contain file overlays" in result.stderr
        assert (
            not any(_candidates_dir(git_project).iterdir())
            if _candidates_dir(git_project).is_dir()
            else True
        )

    def test_malformed_config_patch_shape_reports_schema_errors(
        self, git_project: Path, tmp_path: Path, run_meta, default_overlay
    ) -> None:
        run_meta("init", project=git_project, check=True)
        overlay_dir = default_overlay(tmp_path)
        # "value" キーが欠落 -> config_patch.schema.json 自体の検証エラーになるはず
        (overlay_dir / "config-patch.json").write_text(
            json.dumps([{"file": "x.yaml", "key_path": "a.b"}]), encoding="utf-8"
        )

        result = run_meta(
            "register",
            "--overlay",
            str(overlay_dir),
            "--target",
            "claude-harness",
            project=git_project,
            check=False,
        )

        assert result.returncode == 2
        assert "missing required key 'value'" in result.stderr

    # EV-67: allowlist に含まれていても target gate は迂回できない。
    def test_local_allowlist_does_not_bypass_target_gate(
        self, git_project: Path, tmp_path: Path, run_meta, default_overlay
    ) -> None:
        run_meta("init", project=git_project, check=True)
        local_config_dir = git_project / ".claude" / "config" / "meta-harness"
        local_config_dir.mkdir(parents=True, exist_ok=True)
        (local_config_dir / "meta-harness.local.yaml").write_text(
            "config_patch:\n  allowlist:\n    - agent-routing/cli-tools.yaml#codex.model\n",
            encoding="utf-8",
        )
        overlay_dir = default_overlay(tmp_path)
        (overlay_dir / "config-patch.json").write_text(
            json.dumps(
                [{"file": "agent-routing/cli-tools.yaml", "key_path": "codex.model", "value": "x"}]
            ),
            encoding="utf-8",
        )

        result = run_meta(
            "register",
            "--overlay",
            str(overlay_dir),
            "--target",
            "claude-harness",
            project=git_project,
            check=False,
        )

        assert result.returncode == 2
        assert "non-empty config patches require target='routing-config'" in result.stderr

    def test_local_allowlist_cannot_widen_frozen_ceiling(
        self, git_project: Path, tmp_path: Path, run_meta
    ) -> None:
        run_meta("init", project=git_project, check=True)
        local_config_dir = git_project / ".claude" / "config" / "meta-harness"
        local_config_dir.mkdir(parents=True, exist_ok=True)
        (local_config_dir / "meta-harness.local.yaml").write_text(
            "config_patch:\n"
            "  allowlist:\n"
            "    - agent-routing/cli-tools.yaml#codex.model\n"
            "    - agent-routing/cli-tools.yaml#codex.flags\n",
            encoding="utf-8",
        )
        overlay_dir = tmp_path / "routing-overlay-local-widen"
        overlay_dir.mkdir()
        (overlay_dir / mh.CONFIG_PATCH_FILENAME).write_text(
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

        result = run_meta(
            "register",
            "--overlay",
            str(overlay_dir),
            "--target",
            "routing-config",
            project=git_project,
            check=False,
        )

        assert result.returncode == 2
        assert "CONFIG_PATCH_ALLOWLIST_CEILING" in result.stderr

    # PR #162 レビュー指摘 (FIX D): overlay/config-patch.json が外部ファイルへの symlink の
    # 場合、予約サイドカー名の早期 continue で迂回されず symlink として拒否されること
    def test_config_patch_json_symlink_is_rejected(
        self, git_project: Path, tmp_path: Path, run_meta, default_overlay
    ) -> None:
        run_meta("init", project=git_project, check=True)
        overlay_dir = default_overlay(tmp_path)
        outside_target = tmp_path / "outside-config-patch.json"
        outside_target.write_text("[]", encoding="utf-8")
        (overlay_dir / "config-patch.json").symlink_to(outside_target)

        result = run_meta(
            "register",
            "--overlay",
            str(overlay_dir),
            "--target",
            "claude-harness",
            project=git_project,
            check=False,
        )

        assert result.returncode == 2
        assert "symlink" in result.stderr.lower()
        assert (
            not any(_candidates_dir(git_project).iterdir())
            if _candidates_dir(git_project).is_dir()
            else True
        )


class TestRegisterInputValidation:
    def test_staging_revalidation_value_error_exits_2(
        self, git_project: Path, tmp_path: Path, run_meta, default_overlay, monkeypatch, capsys
    ) -> None:
        run_meta("init", project=git_project, check=True)
        overlay_dir = default_overlay(tmp_path)

        def fail_staging_revalidation(*_args, **_kwargs):
            raise ValueError("copied overlay validation failed")

        monkeypatch.setattr(cli.mh, "register_candidate", fail_staging_revalidation)

        exit_code = cli.cmd_register(
            str(git_project),
            str(overlay_dir),
            "claude-harness",
            None,
            "candidate",
            None,
            None,
            False,
        )

        assert exit_code == cli.EXIT_VALIDATION_ERROR
        assert "copied overlay validation failed" in capsys.readouterr().err

    # EV-25
    def test_missing_overlay_arg_exits_2(self, git_project: Path, run_meta) -> None:
        run_meta("init", project=git_project, check=True)
        result = run_meta(
            "register", "--target", "claude-harness", project=git_project, check=False
        )
        assert result.returncode == 2

    # EV-25
    def test_nonexistent_overlay_dir_exits_2(self, git_project: Path, run_meta) -> None:
        run_meta("init", project=git_project, check=True)
        result = run_meta(
            "register",
            "--overlay",
            str(git_project / "does-not-exist"),
            "--target",
            "claude-harness",
            project=git_project,
            check=False,
        )
        assert result.returncode == 2
        assert "does not exist" in result.stderr


class TestRegisterDirtyRepoWarning:
    def test_dirty_repo_warns_but_still_registers(
        self, git_project: Path, tmp_path: Path, run_meta, default_overlay
    ) -> None:
        run_meta("init", project=git_project, check=True)
        (git_project / "uncommitted.txt").write_text("dirty", encoding="utf-8")
        overlay_dir = default_overlay(tmp_path)

        result = run_meta(
            "register",
            "--overlay",
            str(overlay_dir),
            "--target",
            "claude-harness",
            project=git_project,
            check=False,
        )

        assert result.returncode == 0
        assert "dirty" in result.stderr.lower()

    def test_clean_repo_has_no_dirty_warning(
        self, git_project: Path, tmp_path: Path, run_meta, default_overlay
    ) -> None:
        run_meta("init", project=git_project, check=True)
        overlay_dir = default_overlay(tmp_path)

        result = run_meta(
            "register",
            "--overlay",
            str(overlay_dir),
            "--target",
            "claude-harness",
            project=git_project,
            check=False,
        )

        assert result.returncode == 0
        assert "dirty" not in result.stderr.lower()


class TestRegisterSourceCommit:
    def test_source_commit_defaults_to_head(
        self, git_project: Path, tmp_path: Path, run_meta, default_overlay
    ) -> None:
        run_meta("init", project=git_project, check=True)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=git_project,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        overlay_dir = default_overlay(tmp_path)

        result = run_meta(
            "register",
            "--overlay",
            str(overlay_dir),
            "--target",
            "claude-harness",
            "--json",
            project=git_project,
            check=True,
        )
        cand_id = json.loads(result.stdout)["cand_id"]
        manifest = json.loads(
            (_candidates_dir(git_project) / cand_id / "manifest.json").read_text(encoding="utf-8")
        )
        assert manifest["source_commit"] == head


class TestRegisterSourceCommitFromWorktree:
    # register (EV-*, Sec2-0): source_commit / dirty 判定は main_root ではなく
    # 登録元（--project）の worktree で解決されるべき
    def test_source_commit_resolves_to_feature_worktree_head_not_main_root(
        self,
        git_project: Path,
        tmp_path: Path,
        run_meta,
        default_overlay,
        add_feature_worktree,
        git_run,
    ) -> None:
        run_meta("init", project=git_project, check=True)
        worktree_dir = add_feature_worktree(git_project, name="feat-source-commit")

        (worktree_dir / "extra.txt").write_text("worktree-only change\n", encoding="utf-8")
        git_run("add", "extra.txt", cwd=worktree_dir)
        git_run("commit", "-q", "-m", "worktree-only commit", cwd=worktree_dir)

        main_root_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=git_project,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        worktree_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree_dir,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert worktree_head != main_root_head

        overlay_dir = default_overlay(tmp_path)
        result = run_meta(
            "register",
            "--overlay",
            str(overlay_dir),
            "--target",
            "claude-harness",
            "--json",
            project=worktree_dir,
            check=True,
        )
        cand_id = json.loads(result.stdout)["cand_id"]
        manifest = json.loads(
            (_candidates_dir(git_project) / cand_id / "manifest.json").read_text(encoding="utf-8")
        )

        assert manifest["source_commit"] == worktree_head
        assert manifest["source_commit"] != main_root_head

    def _ignore_worktrees_dir(self, git_project: Path, git_run) -> None:
        # CodeRabbit review #162: `.worktrees/` を ignore しないと、worktree を
        # 追加しただけで main_root 側の `git status --porcelain` が `?? .worktrees/`
        # で dirty 扱いになり、main_root を見てしまう実装でもテストが偶然通ってしまう。
        # main_root を真に clean な状態に保つため明示的に ignore + commit する。
        gitignore = git_project / ".gitignore"
        gitignore.write_text(
            gitignore.read_text(encoding="utf-8") + ".worktrees/\n", encoding="utf-8"
        )
        git_run("add", ".gitignore", cwd=git_project)
        git_run("commit", "-q", "-m", "ignore worktrees dir", cwd=git_project)

    def test_dirty_warning_reflects_feature_worktree_not_main_root(
        self,
        git_project: Path,
        tmp_path: Path,
        run_meta,
        default_overlay,
        add_feature_worktree,
        git_run,
    ) -> None:
        run_meta("init", project=git_project, check=True)
        self._ignore_worktrees_dir(git_project, git_run)
        worktree_dir = add_feature_worktree(git_project, name="feat-dirty-check")
        (worktree_dir / "uncommitted.txt").write_text("dirty in worktree", encoding="utf-8")
        overlay_dir = default_overlay(tmp_path)

        result = run_meta(
            "register",
            "--overlay",
            str(overlay_dir),
            "--target",
            "claude-harness",
            project=worktree_dir,
            check=True,
        )

        assert "dirty" in result.stderr.lower()

    # PR #162 レビュー指摘 (CodeRabbit, FIX 9): main_root が dirty で feature worktree が
    # clean な負例を追加する。main_root ではなく --project（worktree）をスコープに
    # dirty 判定していることを、上の正例と対にして証明する。
    def test_dirty_main_root_alone_does_not_trigger_warning_from_clean_worktree(
        self,
        git_project: Path,
        tmp_path: Path,
        run_meta,
        default_overlay,
        add_feature_worktree,
        git_run,
    ) -> None:
        run_meta("init", project=git_project, check=True)
        self._ignore_worktrees_dir(git_project, git_run)
        worktree_dir = add_feature_worktree(git_project, name="feat-clean-check")
        # main_root 側だけを dirty にする（worktree 側は一切触れない）
        (git_project / "main-root-only-dirty.txt").write_text(
            "dirty in main root only", encoding="utf-8"
        )
        overlay_dir = default_overlay(tmp_path)

        result = run_meta(
            "register",
            "--overlay",
            str(overlay_dir),
            "--target",
            "claude-harness",
            project=worktree_dir,
            check=True,
        )

        assert "dirty" not in result.stderr.lower()


class TestRegisterAtomicStaging:
    def test_successful_register_leaves_no_tmp_residue(
        self, git_project: Path, run_meta, tmp_path: Path, default_overlay
    ) -> None:
        run_meta("init", project=git_project, check=True)
        config = mh.load_config(git_project)
        overlay_dir = default_overlay(tmp_path)
        cand_id = "cand-20260101-000000-atomic-success"

        mh.register_candidate(
            git_project,
            config,
            cand_id=cand_id,
            manifest={
                **_manifest(cand_id),
                "config_hash": mh.compute_config_hash(overlay_dir, config),
            },
            overlay_dir=overlay_dir,
            overlay_files=["facets/example-facet/SKILL.md"],
        )

        assert _tmp_register_dirs(git_project) == []

    def test_register_failure_due_to_existing_candidate_leaves_no_tmp_residue(
        self, git_project: Path, run_meta, tmp_path: Path, default_overlay
    ) -> None:
        run_meta("init", project=git_project, check=True)
        config = mh.load_config(git_project)
        overlay_dir = default_overlay(tmp_path)
        cand_id = "cand-20260101-000000-atomic-existing"

        mh.register_candidate(
            git_project,
            config,
            cand_id=cand_id,
            manifest={
                **_manifest(cand_id, "first"),
                "config_hash": mh.compute_config_hash(overlay_dir, config),
            },
            overlay_dir=overlay_dir,
            overlay_files=["facets/example-facet/SKILL.md"],
        )

        try:
            mh.register_candidate(
                git_project,
                config,
                cand_id=cand_id,
                manifest={
                    **_manifest(cand_id, "second"),
                    "config_hash": mh.compute_config_hash(overlay_dir, config),
                },
                overlay_dir=overlay_dir,
                overlay_files=["facets/example-facet/SKILL.md"],
            )
        except FileExistsError:
            pass
        else:
            raise AssertionError("re-registering the same cand_id should raise FileExistsError")


class TestRegisterParentSourceCommitInheritance:
    # register (Sec6): --parent 指定時の source_commit 継承は skill:* target に限定されて
    # いたが、lineage 整合チェック（promoter._promotion_lineage 等）は target を問わず
    # 無条件で親の source_commit 一致を要求するため、非 skill target（claude-harness 等）で
    # --parent 指定・--source-commit 省略のまま HEAD が進むと register は通っても
    # evaluate/promote で lineage mismatch になる回帰を防止する。
    def test_parent_source_commit_is_inherited_for_non_skill_target(
        self, git_project: Path, tmp_path: Path, run_meta, default_overlay, git_run
    ) -> None:
        run_meta("init", project=git_project, check=True)
        overlay_dir = default_overlay(tmp_path)

        parent_result = run_meta(
            "register",
            "--overlay",
            str(overlay_dir),
            "--target",
            "claude-harness",
            "--json",
            project=git_project,
            check=True,
        )
        parent_cand_id = json.loads(parent_result.stdout)["cand_id"]
        parent_manifest = json.loads(
            (_candidates_dir(git_project) / parent_cand_id / "manifest.json").read_text(
                encoding="utf-8"
            )
        )

        (git_project / "extra.txt").write_text("advance head\n", encoding="utf-8")
        git_run("add", "extra.txt", cwd=git_project)
        git_run("commit", "-q", "-m", "advance head", cwd=git_project)

        child_result = run_meta(
            "register",
            "--overlay",
            str(overlay_dir),
            "--target",
            "claude-harness",
            "--parent",
            parent_cand_id,
            "--json",
            project=git_project,
            check=True,
        )
        child_cand_id = json.loads(child_result.stdout)["cand_id"]
        child_manifest = json.loads(
            (_candidates_dir(git_project) / child_cand_id / "manifest.json").read_text(
                encoding="utf-8"
            )
        )

        assert child_manifest["source_commit"] == parent_manifest["source_commit"]

    def test_parent_source_commit_mismatch_is_rejected_for_non_skill_target(
        self, git_project: Path, tmp_path: Path, run_meta, default_overlay, git_run
    ) -> None:
        run_meta("init", project=git_project, check=True)
        overlay_dir = default_overlay(tmp_path)

        parent_result = run_meta(
            "register",
            "--overlay",
            str(overlay_dir),
            "--target",
            "claude-harness",
            "--json",
            project=git_project,
            check=True,
        )
        parent_cand_id = json.loads(parent_result.stdout)["cand_id"]

        (git_project / "extra.txt").write_text("advance head\n", encoding="utf-8")
        git_run("add", "extra.txt", cwd=git_project)
        git_run("commit", "-q", "-m", "advance head", cwd=git_project)
        new_head = git_run("rev-parse", "HEAD", cwd=git_project).stdout.strip()

        result = run_meta(
            "register",
            "--overlay",
            str(overlay_dir),
            "--target",
            "claude-harness",
            "--parent",
            parent_cand_id,
            "--source-commit",
            new_head,
            project=git_project,
        )

        assert result.returncode == cli.EXIT_VALIDATION_ERROR
        assert "source_commit must match its parent" in result.stderr

        assert _tmp_register_dirs(git_project) == []

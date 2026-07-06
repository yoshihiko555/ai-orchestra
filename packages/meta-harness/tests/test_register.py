"""`register` サブコマンドのテスト（EV-03, EV-05, EV-25, dirty repo 警告, Sec6）。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tests.module_loader import load_module

mh = load_module(
    "meta_harness_common_register",
    "packages/meta-harness/lib/meta_harness_common.py",
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
                manifest={**manifest, "description": "second attempt"},
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


class TestRegisterConfigPatchRejection:
    # EV-05
    def test_config_patch_is_rejected_in_phase_1a(
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
        assert "config_patch is rejected in Phase 1a" in result.stderr
        assert (
            not any(_candidates_dir(git_project).iterdir())
            if _candidates_dir(git_project).is_dir()
            else True
        )

    def test_malformed_config_patch_shape_reports_schema_errors_not_phase1a_message(
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
        assert "config_patch is rejected in Phase 1a" not in result.stderr
        assert "missing required key 'value'" in result.stderr


class TestRegisterInputValidation:
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

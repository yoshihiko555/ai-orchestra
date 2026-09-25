"""orchestra-manager.py の context 管理機能テスト。"""

from __future__ import annotations

import datetime
import json
import os
import stat
import sys
from pathlib import Path


def _setup_context_packages(orchestra_dir: Path, extra_packages: dict | None = None) -> None:
    """context_files を含む minimal manifest を作成する。"""
    packages = {
        "core": {
            "name": "core",
            "version": "0.0.0",
            "depends": [],
            "context_files": {
                "source": "agents.md",
                "managed": ["orchestra.md"],
                "template": "templates/project/AGENTS.md",
                "init": ["AGENTS.md"],
                "sync": ["AGENTS.md"],
            },
        },
    }
    if extra_packages:
        packages.update(extra_packages)
    packages_dir = orchestra_dir / "packages"
    packages_dir.mkdir(parents=True, exist_ok=True)
    for package_name, manifest in packages.items():
        package_dir = packages_dir / package_name
        package_dir.mkdir(parents=True, exist_ok=True)
        (package_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


import pytest

from tests.module_loader import REPO_ROOT, load_module

manager_mod = load_module("orchestra_manager", "scripts/orchestra-manager.py")
OrchestraManager = manager_mod.OrchestraManager


def _setup_context_sources(orchestra_dir: Path, extra_packages: dict | None = None) -> None:
    context_dir = orchestra_dir / "templates" / "context"
    context_dir.mkdir(parents=True, exist_ok=True)
    (context_dir / "agents.md").write_text(
        "# AGENTS\n\nagents body\n",
        encoding="utf-8",
    )
    (context_dir / "orchestra.md").write_text(
        "## Managed\n\nmanaged body\n",
        encoding="utf-8",
    )
    _setup_context_packages(orchestra_dir, extra_packages)


def _setup_orchestra_json(
    project_dir: Path,
    installed_packages: list[str] | None = None,
) -> None:
    """Create .claude/orchestra.json with specified installed packages."""
    if installed_packages is None:
        installed_packages = []
    claude_dir = project_dir / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    orchestra_state = {
        "installed_packages": installed_packages,
        "orchestra_dir": "",
        "last_sync": "",
    }
    (claude_dir / "orchestra.json").write_text(
        json.dumps(orchestra_state),
        encoding="utf-8",
    )


def _read_text_preserving_newlines(path: Path) -> str:
    with path.open(encoding="utf-8", newline="") as source:
        return source.read()


def _extract_managed_span(manager: OrchestraManager, content: str) -> str:
    begin = manager.MANAGED_BLOCK_BEGIN
    end = manager.MANAGED_BLOCK_END
    assert content.count(begin) == 1
    assert content.count(end) == 1
    begin_index = content.index(begin)
    end_index = content.index(end, begin_index) + len(end)
    return content[begin_index:end_index]


def _core_context_files(**overrides: object) -> dict[str, object]:
    context_files: dict[str, object] = {
        "source": "agents.md",
        "managed": ["orchestra.md"],
        "template": "templates/project/AGENTS.md",
        "init": ["AGENTS.md"],
        "sync": ["AGENTS.md"],
    }
    context_files.update(overrides)
    return context_files


def _write_package_manifest(
    orchestra_dir: Path,
    package_name: str,
    context_files: dict[str, object],
) -> None:
    _setup_context_packages(
        orchestra_dir,
        {
            package_name: {
                "name": package_name,
                "version": "0.0.0",
                "depends": [],
                "context_files": context_files,
            }
        },
    )


class TestContextBuildAndCheck:
    def test_build_generates_single_agents_template_with_managed_block(
        self, tmp_path: Path
    ) -> None:
        _setup_context_sources(tmp_path)
        manager = OrchestraManager(tmp_path)

        changed = manager.context_build()

        generated_path = tmp_path / "templates" / "project" / "AGENTS.md"
        generated = generated_path.read_text(encoding="utf-8")
        assert changed == 1
        assert "agents body" in generated
        assert "managed body" in generated
        assert manager.MANAGED_BLOCK_BEGIN in generated
        assert manager.MANAGED_BLOCK_END in generated
        assert generated.index(manager.MANAGED_BLOCK_BEGIN) < generated.index(
            manager.MANAGED_BLOCK_END
        )

    def test_check_detects_direct_template_mutation(self, tmp_path: Path) -> None:
        _setup_context_sources(tmp_path)
        manager = OrchestraManager(tmp_path)
        manager.context_build()

        assert manager.context_check() is True

        generated_path = tmp_path / "templates" / "project" / "AGENTS.md"
        generated_path.write_text("# stale\n", encoding="utf-8")

        assert manager.context_check() is False


class TestContextSyncCreateForceDryRun:
    def test_sync_creates_agents_with_full_render(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_context_sources(orchestra_dir)
        _setup_orchestra_json(project_dir)
        manager = OrchestraManager(orchestra_dir)

        changed = manager.context_sync(str(project_dir))

        agents = (project_dir / "AGENTS.md").read_text(encoding="utf-8")
        assert changed == 1
        assert "agents body" in agents
        assert "managed body" in agents
        assert manager.MANAGED_BLOCK_BEGIN in agents
        assert manager.MANAGED_BLOCK_END in agents

    def test_dry_run_creates_no_agents_or_backup_directory(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_context_sources(orchestra_dir)
        _setup_orchestra_json(project_dir)

        OrchestraManager(orchestra_dir).context_sync(str(project_dir), dry_run=True)

        assert not (project_dir / "AGENTS.md").exists()
        assert not (project_dir / ".claude" / "state" / "legacy-context").exists()

    def test_force_replaces_handwritten_agents_with_full_render(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_context_sources(orchestra_dir)
        _setup_orchestra_json(project_dir)
        manager = OrchestraManager(orchestra_dir)
        agents_path = project_dir / "AGENTS.md"
        agents_path.write_text("hand-written content\n", encoding="utf-8")

        changed = manager.context_sync(str(project_dir), force=True)

        agents = agents_path.read_text(encoding="utf-8")
        assert changed == 1
        assert "hand-written content" not in agents
        assert agents == manager._render_context_content(
            manager.CONTEXT_SPECS[0].source_rels,
            manager.CONTEXT_SPECS[0].managed_rels,
        )
        assert manager.MANAGED_BLOCK_BEGIN in agents

    def test_new_agents_file_uses_process_default_mode(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_context_sources(orchestra_dir)
        _setup_orchestra_json(project_dir)
        current_umask = os.umask(0)
        os.umask(current_umask)
        expected_mode = 0o666 & ~current_umask

        OrchestraManager(orchestra_dir).context_sync(str(project_dir))

        actual_mode = stat.S_IMODE((project_dir / "AGENTS.md").stat().st_mode)
        assert actual_mode == expected_mode
        assert actual_mode != 0o600

    def test_force_backs_up_handwritten_agents_before_full_overwrite(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_context_sources(orchestra_dir)
        _setup_orchestra_json(project_dir)
        manager = OrchestraManager(orchestra_dir)
        agents_path = project_dir / "AGENTS.md"
        original = "# Hand-written project instructions\n\nkeep this in backup\n"
        agents_path.write_text(original, encoding="utf-8")

        changed = manager.context_sync(str(project_dir), force=True)

        backups = list(
            (project_dir / ".claude" / "state" / "legacy-context").glob("AGENTS.md.*.bak")
        )
        expected = manager._render_context_content(
            manager.CONTEXT_SPECS[0].source_rels,
            manager.CONTEXT_SPECS[0].managed_rels,
        )
        assert changed == 1
        assert len(backups) == 1
        assert backups[0].read_text(encoding="utf-8") == original
        assert agents_path.read_text(encoding="utf-8") == expected

    def test_force_twice_in_same_second_keeps_both_backups(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_context_sources(orchestra_dir)
        _setup_orchestra_json(project_dir)
        manager = OrchestraManager(orchestra_dir)
        context_mod = sys.modules[type(manager)._backup_legacy_file.__module__]
        frozen = datetime.datetime(2026, 9, 26, tzinfo=datetime.UTC)

        class _FrozenDatetime(datetime.datetime):
            @classmethod
            def now(cls, tz: datetime.tzinfo | None = None) -> datetime.datetime:
                return frozen

        monkeypatch.setattr(context_mod.datetime, "datetime", _FrozenDatetime)
        agents_path = project_dir / "AGENTS.md"
        agents_path.write_text("# first\n", encoding="utf-8")
        manager.context_sync(str(project_dir), force=True)
        agents_path.write_text("# second\n", encoding="utf-8")
        manager.context_sync(str(project_dir), force=True)

        backups = sorted(
            (project_dir / ".claude" / "state" / "legacy-context").glob("AGENTS.md.*.bak")
        )
        assert sorted(b.read_text(encoding="utf-8") for b in backups) == ["# first\n", "# second\n"]

    def test_backup_refuses_symlinked_directory_outside_project(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        (project_dir / ".claude" / "state").mkdir(parents=True)
        (project_dir / ".claude" / "state" / "legacy-context").symlink_to(outside_dir)
        _setup_context_sources(orchestra_dir)
        _setup_orchestra_json(project_dir)
        agents_path = project_dir / "AGENTS.md"
        original = "# Hand-written\n"
        agents_path.write_text(original, encoding="utf-8")

        OrchestraManager(orchestra_dir).context_sync(str(project_dir), force=True)

        assert list(outside_dir.iterdir()) == []
        assert agents_path.read_text(encoding="utf-8") == original

    def test_force_backs_up_invalid_utf8_before_full_overwrite(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_context_sources(orchestra_dir)
        _setup_orchestra_json(project_dir)
        manager = OrchestraManager(orchestra_dir)
        agents_path = project_dir / "AGENTS.md"
        original = b"\xff\xfe\x00invalid-utf8-\x80\x81"
        agents_path.write_bytes(original)

        changed = manager.context_sync(str(project_dir), force=True)

        backups = list(
            (project_dir / ".claude" / "state" / "legacy-context").glob("AGENTS.md.*.bak")
        )
        rendered = agents_path.read_text(encoding="utf-8")
        assert changed == 1
        assert manager.MANAGED_BLOCK_BEGIN in rendered
        assert manager.MANAGED_BLOCK_END in rendered
        assert len(backups) == 1
        assert backups[0].read_bytes() == original

    @pytest.mark.parametrize(
        "existing_content",
        ["", "   \n\n\t\n"],
        ids=["empty", "whitespace-only"],
    )
    def test_empty_existing_agents_receives_full_render(
        self, tmp_path: Path, existing_content: str
    ) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_context_sources(orchestra_dir)
        _setup_orchestra_json(project_dir)
        manager = OrchestraManager(orchestra_dir)
        agents_path = project_dir / "AGENTS.md"
        agents_path.write_text(existing_content, encoding="utf-8")

        changed = manager.context_sync(str(project_dir))

        rendered = agents_path.read_text(encoding="utf-8")
        expected = manager._render_context_content(
            manager.CONTEXT_SPECS[0].source_rels,
            manager.CONTEXT_SPECS[0].managed_rels,
        )
        assert changed == 1
        assert rendered == expected
        assert "agents body" in rendered
        assert manager.MANAGED_BLOCK_BEGIN in rendered
        assert "managed body" in rendered
        assert manager.MANAGED_BLOCK_END in rendered

    def test_sync_skips_symlinked_agents_target(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_context_sources(orchestra_dir)
        _setup_orchestra_json(project_dir)
        manager = OrchestraManager(orchestra_dir)
        outside_file = tmp_path / "outside.md"
        outside_file.write_text("outside\n", encoding="utf-8")
        agents_path = project_dir / "AGENTS.md"
        try:
            agents_path.symlink_to(outside_file)
        except OSError:
            pytest.skip("symlink unsupported in this environment")

        changed = manager.context_sync(str(project_dir), force=True)

        assert changed == 0
        assert outside_file.read_text(encoding="utf-8") == "outside\n"

    def test_sync_skips_target_below_symlinked_parent_outside_project(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        nested_package = {
            "nested-docs": {
                "name": "nested-docs",
                "version": "0.0.0",
                "depends": [],
                "context_files": {
                    "source": "agents.md",
                    "template": "templates/nested/NOTES.md",
                    "init": [".sub/NOTES.md"],
                    "sync": [".sub/NOTES.md"],
                },
            }
        }
        _setup_context_sources(orchestra_dir, extra_packages=nested_package)
        _setup_orchestra_json(project_dir, installed_packages=["nested-docs"])
        manager = OrchestraManager(orchestra_dir)
        external_dir = tmp_path / "external"
        external_dir.mkdir()
        symlinked_parent = project_dir / ".sub"
        try:
            symlinked_parent.symlink_to(external_dir, target_is_directory=True)
        except OSError:
            pytest.skip("symlink unsupported in this environment")

        changed = manager.context_sync(str(project_dir), force=True)

        assert changed == 1
        assert (project_dir / "AGENTS.md").is_file()
        assert not (external_dir / "NOTES.md").exists()


class TestContextSyncManagedBlockMerge:
    def test_sync_appends_managed_block_below_handwritten_content(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_context_sources(orchestra_dir)
        _setup_orchestra_json(project_dir)
        manager = OrchestraManager(orchestra_dir)
        agents_path = project_dir / "AGENTS.md"
        handwritten = "# Project\n\nproject guidance\n"
        agents_path.write_text(handwritten, encoding="utf-8")
        managed_body = manager._render_fragment_group(
            manager.CONTEXT_SPECS[0].managed_rels,
            "\n\n",
        )
        expected_block = manager._build_managed_block(managed_body)

        changed = manager.context_sync(str(project_dir))

        merged = agents_path.read_text(encoding="utf-8")
        assert changed == 1
        assert merged == handwritten.rstrip("\n") + "\n\n" + expected_block + "\n"
        assert merged.endswith(manager.MANAGED_BLOCK_END + "\n")
        captured = capsys.readouterr()
        assert "AGENTS.md" in captured.out
        assert "追記" in captured.out

    def test_sync_preserves_legacy_marker_quoted_mid_document(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_context_sources(orchestra_dir)
        _setup_orchestra_json(project_dir)
        manager = OrchestraManager(orchestra_dir)
        agents_path = project_dir / "AGENTS.md"
        original = f"# Notes\n\nSee also: {manager.GENERATED_MARKER}\n"
        agents_path.write_text(original, encoding="utf-8")

        changed = manager.context_sync(str(project_dir))

        merged = agents_path.read_text(encoding="utf-8")
        backup_dir = project_dir / ".claude" / "state" / "legacy-context"
        assert changed == 1
        assert not backup_dir.exists()
        assert merged.startswith(original.rstrip("\n") + "\n\n")
        assert f"See also: {manager.GENERATED_MARKER}" in merged
        assert merged.count(manager.MANAGED_BLOCK_BEGIN) == 1
        assert merged.count(manager.MANAGED_BLOCK_END) == 1

    def test_shorter_inner_fence_does_not_close_longer_fence(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_context_sources(orchestra_dir)
        _setup_orchestra_json(project_dir)
        manager = OrchestraManager(orchestra_dir)
        agents_path = project_dir / "AGENTS.md"
        fenced_example = "\n".join(
            [
                "# Marker example",
                "",
                "````markdown",
                "```text",
                manager.MANAGED_BLOCK_BEGIN,
                "example managed body",
                manager.MANAGED_BLOCK_END,
                "```",
                "````",
                "",
            ]
        )
        agents_path.write_text(fenced_example, encoding="utf-8")

        manager.context_sync(str(project_dir))

        merged = agents_path.read_text(encoding="utf-8")
        assert merged.startswith(fenced_example)
        assert "example managed body" in merged
        assert merged.count(manager.MANAGED_BLOCK_BEGIN) == 2

    def test_sync_ignores_managed_markers_inside_fenced_code_block(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_context_sources(orchestra_dir)
        _setup_orchestra_json(project_dir)
        manager = OrchestraManager(orchestra_dir)
        agents_path = project_dir / "AGENTS.md"
        fenced_example = "\n".join(
            [
                "# Marker example",
                "",
                "```text",
                manager.MANAGED_BLOCK_BEGIN,
                "example managed body",
                manager.MANAGED_BLOCK_END,
                "```",
                "",
                "Project notes remain.",
            ]
        )
        original_bytes = fenced_example.encode("utf-8")
        agents_path.write_bytes(original_bytes)

        changed = manager.context_sync(str(project_dir))

        merged_bytes = agents_path.read_bytes()
        merged = merged_bytes.decode("utf-8")
        assert changed == 1
        assert merged_bytes.startswith(original_bytes + b"\n\n")
        assert merged.count(manager.MANAGED_BLOCK_BEGIN) == 2
        assert merged.count(manager.MANAGED_BLOCK_END) == 2
        assert "managed body" in merged[merged.rindex(manager.MANAGED_BLOCK_BEGIN) :]

    def test_sync_preserves_existing_file_mode_when_appending_managed_block(
        self, tmp_path: Path
    ) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_context_sources(orchestra_dir)
        _setup_orchestra_json(project_dir)
        agents_path = project_dir / "AGENTS.md"
        agents_path.write_text("# Project guidance\n", encoding="utf-8")
        try:
            os.chmod(agents_path, 0o644)
            if stat.S_IMODE(agents_path.stat().st_mode) != 0o644:
                pytest.skip("chmod/stat mode semantics unsupported in this environment")
        except OSError:
            pytest.skip("chmod/stat mode semantics unsupported in this environment")

        OrchestraManager(orchestra_dir).context_sync(str(project_dir))

        assert stat.S_IMODE(agents_path.stat().st_mode) == 0o644

    def test_sync_replaces_only_stale_managed_block(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_context_sources(orchestra_dir)
        _setup_orchestra_json(project_dir)
        manager = OrchestraManager(orchestra_dir)
        agents_path = project_dir / "AGENTS.md"
        prefix = "# Project\n\nproject guidance\n\n"
        suffix = "\nTrailing project content\n"
        stale_block = "\n".join(
            [
                manager.MANAGED_BLOCK_BEGIN,
                "",
                "stale managed text",
                "",
                manager.MANAGED_BLOCK_END,
            ]
        )
        agents_path.write_text(prefix + stale_block + suffix, encoding="utf-8")

        changed = manager.context_sync(str(project_dir))

        merged = agents_path.read_text(encoding="utf-8")
        assert changed == 1
        assert merged.startswith(prefix)
        assert merged.endswith(suffix)
        assert "managed body" in _extract_managed_span(manager, merged)
        assert "stale managed text" not in merged

    def test_second_sync_keeps_up_to_date_file_byte_identical(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_context_sources(orchestra_dir)
        _setup_orchestra_json(project_dir)
        manager = OrchestraManager(orchestra_dir)
        manager.context_sync(str(project_dir))
        agents_path = project_dir / "AGENTS.md"
        before = agents_path.read_bytes()
        capsys.readouterr()

        changed = manager.context_sync(str(project_dir))

        assert changed == 0
        assert agents_path.read_bytes() == before
        assert "スキップ（差分なし）" in capsys.readouterr().out

    @pytest.mark.parametrize(
        "malformed_content",
        [
            "{begin}\nmissing end\n",
            "{end}\ntext\n{begin}\n",
            "{begin}\n{begin}\ntext\n{end}\n",
        ],
        ids=["missing-end", "end-before-begin", "duplicate-begin"],
    )
    def test_malformed_managed_block_is_left_byte_identical(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        malformed_content: str,
    ) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_context_sources(orchestra_dir)
        _setup_orchestra_json(project_dir)
        manager = OrchestraManager(orchestra_dir)
        agents_path = project_dir / "AGENTS.md"
        malformed = malformed_content.format(
            begin=manager.MANAGED_BLOCK_BEGIN,
            end=manager.MANAGED_BLOCK_END,
        ).encode()
        agents_path.write_bytes(malformed)

        changed = manager.context_sync(str(project_dir))

        assert changed == 0
        assert agents_path.read_bytes() == malformed
        assert "警告" in capsys.readouterr().err

    @pytest.mark.parametrize("has_existing_block", [False, True], ids=["append", "replace"])
    def test_sync_preserves_crlf_in_modified_region_and_existing_prefix(
        self, tmp_path: Path, has_existing_block: bool
    ) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_context_sources(orchestra_dir)
        _setup_orchestra_json(project_dir)
        manager = OrchestraManager(orchestra_dir)
        agents_path = project_dir / "AGENTS.md"
        prefix_lf = "# Project\n\nproject guidance"
        if has_existing_block:
            prefix_lf += "\n\n"
            existing_lf = "\n".join(
                [
                    prefix_lf + manager.MANAGED_BLOCK_BEGIN,
                    "",
                    "stale managed text",
                    "",
                    manager.MANAGED_BLOCK_END,
                    "",
                    "trailing content",
                    "",
                ]
            )
        else:
            existing_lf = prefix_lf
        existing_bytes = existing_lf.replace("\n", "\r\n").encode("utf-8")
        prefix_bytes = prefix_lf.replace("\n", "\r\n").encode("utf-8")
        agents_path.write_bytes(existing_bytes)

        changed = manager.context_sync(str(project_dir))

        merged = _read_text_preserving_newlines(agents_path)
        merged_bytes = merged.encode("utf-8")
        assert changed == 1
        assert merged_bytes.startswith(prefix_bytes)
        assert b"\n" not in merged_bytes.replace(b"\r\n", b"")


class TestLegacyMigration:
    def test_generated_agents_is_backed_up_and_migrated(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_context_sources(orchestra_dir)
        _setup_orchestra_json(project_dir)
        manager = OrchestraManager(orchestra_dir)
        original = f"{manager.GENERATED_MARKER}\n\n# old generated\n\nstale\n".encode()
        agents_path = project_dir / "AGENTS.md"
        agents_path.write_bytes(original)

        changed = manager.context_sync(str(project_dir))

        migrated = agents_path.read_text(encoding="utf-8")
        backups = list(
            (project_dir / ".claude" / "state" / "legacy-context").glob("AGENTS.md.*.bak")
        )
        assert changed == 1
        assert manager.GENERATED_MARKER not in migrated
        assert manager.MANAGED_BLOCK_BEGIN in migrated
        assert "agents body" in migrated
        assert "managed body" in migrated
        assert len(backups) == 1
        assert backups[0].read_bytes() == original

    def test_bom_prefixed_generated_agents_is_backed_up_and_migrated(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_context_sources(orchestra_dir)
        _setup_orchestra_json(project_dir)
        manager = OrchestraManager(orchestra_dir)
        original = (
            b"\xef\xbb\xbf" + manager.GENERATED_MARKER.encode("utf-8") + b"\n\nold stale body\n"
        )
        agents_path = project_dir / "AGENTS.md"
        agents_path.write_bytes(original)

        changed = manager.context_sync(str(project_dir))

        backups = list(
            (project_dir / ".claude" / "state" / "legacy-context").glob("AGENTS.md.*.bak")
        )
        expected = manager._render_context_content(
            manager.CONTEXT_SPECS[0].source_rels,
            manager.CONTEXT_SPECS[0].managed_rels,
        )
        migrated = agents_path.read_text(encoding="utf-8")
        assert changed == 1
        assert migrated == expected
        assert manager.MANAGED_BLOCK_BEGIN in migrated
        assert manager.MANAGED_BLOCK_END in migrated
        assert "old stale body" not in migrated
        assert len(backups) == 1
        assert backups[0].read_bytes() == original

    def test_generated_claude_is_backed_up_and_removed(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_context_sources(orchestra_dir)
        _setup_orchestra_json(project_dir)
        manager = OrchestraManager(orchestra_dir)
        original = f"{manager.GENERATED_MARKER}\n\n# old generated\n".encode()
        legacy_path = project_dir / "CLAUDE.md"
        legacy_path.write_bytes(original)

        changed = manager.context_sync(str(project_dir))

        backups = list(
            (project_dir / ".claude" / "state" / "legacy-context").glob("CLAUDE.md.*.bak")
        )
        assert changed >= 2
        assert (project_dir / "AGENTS.md").is_file()
        assert not legacy_path.exists()
        assert len(backups) == 1
        assert backups[0].read_bytes() == original

    def test_handwritten_claude_is_preserved_and_warned(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_context_sources(orchestra_dir)
        _setup_orchestra_json(project_dir)
        legacy_path = project_dir / "CLAUDE.md"
        original = "# Hand-written Claude guidance\n"
        legacy_path.write_text(original, encoding="utf-8")

        OrchestraManager(orchestra_dir).context_sync(str(project_dir))

        assert legacy_path.read_text(encoding="utf-8") == original
        stderr = capsys.readouterr().err
        assert "CLAUDE.md" in stderr
        assert "AGENTS.md" in stderr

    def test_legacy_marker_quoted_mid_document_keeps_claude_unchanged(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_context_sources(orchestra_dir)
        _setup_orchestra_json(project_dir)
        manager = OrchestraManager(orchestra_dir)
        legacy_path = project_dir / "CLAUDE.md"
        original = f"# My notes\n\n{manager.GENERATED_MARKER}\n\nmore text\n"
        legacy_path.write_text(original, encoding="utf-8")

        manager.context_sync(str(project_dir))

        captured = capsys.readouterr()
        assert legacy_path.read_text(encoding="utf-8") == original
        assert "スキップ（手書きファイルの可能性）: CLAUDE.md" in captured.out
        assert "削除（配布廃止）: CLAUDE.md" not in captured.out
        assert "バックアップ" not in captured.out

    def test_generated_claude_is_kept_when_core_sync_is_malformed(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_context_sources(orchestra_dir)
        _setup_orchestra_json(project_dir)
        manager = OrchestraManager(orchestra_dir)
        malformed_agents = f"{manager.MANAGED_BLOCK_BEGIN}\nmissing end\n"
        (project_dir / "AGENTS.md").write_text(malformed_agents, encoding="utf-8")
        legacy_path = project_dir / "CLAUDE.md"
        legacy_content = f"{manager.GENERATED_MARKER}\n\n# old generated\n"
        legacy_path.write_text(legacy_content, encoding="utf-8")

        changed = manager.context_sync(str(project_dir))

        assert changed == 0
        assert legacy_path.read_text(encoding="utf-8") == legacy_content
        assert manager.GENERATED_MARKER in legacy_path.read_text(encoding="utf-8")

    def test_generated_claude_kept_after_malformed_core_sync_has_only_specific_warning(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_context_sources(orchestra_dir)
        _setup_orchestra_json(project_dir)
        manager = OrchestraManager(orchestra_dir)
        malformed_agents = f"{manager.MANAGED_BLOCK_BEGIN}\nmissing end\n"
        (project_dir / "AGENTS.md").write_text(malformed_agents, encoding="utf-8")
        legacy_path = project_dir / "CLAUDE.md"
        legacy_content = f"{manager.GENERATED_MARKER}\n\n# old generated\n"
        legacy_path.write_text(legacy_content, encoding="utf-8")

        changed = manager.context_sync(str(project_dir))

        stderr = capsys.readouterr().err
        backup_dir = project_dir / ".claude" / "state" / "legacy-context"
        assert changed == 0
        assert legacy_path.read_text(encoding="utf-8") == legacy_content
        assert manager.GENERATED_MARKER in legacy_path.read_text(encoding="utf-8")
        assert not backup_dir.exists()
        assert "AGENTS.md の同期に失敗したため CLAUDE.md を残しました" in stderr
        assert stderr.count("内容を AGENTS.md に移して削除してください") == 0

    @pytest.mark.parametrize("legacy_name", ["AGENTS.md", "CLAUDE.md"])
    def test_dry_run_migrates_or_removes_nothing(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        legacy_name: str,
    ) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_context_sources(orchestra_dir)
        _setup_orchestra_json(project_dir)
        manager = OrchestraManager(orchestra_dir)
        legacy_path = project_dir / legacy_name
        original = f"{manager.GENERATED_MARKER}\n\n# old generated\n".encode()
        legacy_path.write_bytes(original)

        manager.context_sync(str(project_dir), dry_run=True)

        assert legacy_path.read_bytes() == original
        assert not (project_dir / ".claude" / "state" / "legacy-context").exists()
        if legacy_name == "CLAUDE.md":
            assert not (project_dir / "AGENTS.md").exists()
        assert "[DRY-RUN]" in capsys.readouterr().out

    def test_generated_gemini_is_backed_up_and_removed(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_context_sources(orchestra_dir)
        _setup_orchestra_json(project_dir)
        manager = OrchestraManager(orchestra_dir)
        legacy_path = project_dir / ".gemini" / "GEMINI.md"
        legacy_path.parent.mkdir()
        original = f"{manager.GENERATED_MARKER}\n\n# GEMINI\n".encode()
        legacy_path.write_bytes(original)

        manager.context_sync(str(project_dir))

        backups = list(
            (project_dir / ".claude" / "state" / "legacy-context").glob(".gemini__GEMINI.md.*.bak")
        )
        assert not legacy_path.exists()
        assert len(backups) == 1
        assert backups[0].read_bytes() == original

    def test_handwritten_gemini_is_kept(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_context_sources(orchestra_dir)
        _setup_orchestra_json(project_dir)
        legacy_path = project_dir / ".gemini" / "GEMINI.md"
        legacy_path.parent.mkdir()
        original = "# Hand-written Gemini guidance\n"
        legacy_path.write_text(original, encoding="utf-8")

        OrchestraManager(orchestra_dir).context_sync(str(project_dir))

        assert legacy_path.read_text(encoding="utf-8") == original


class TestSpecWithoutManagedBlock:
    def test_existing_file_is_kept_when_spec_has_no_managed_fragments(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_context_sources(orchestra_dir)
        _write_package_manifest(orchestra_dir, "core", _core_context_files(managed=[]))
        _setup_orchestra_json(project_dir)
        agents_path = project_dir / "AGENTS.md"
        original = "# Existing guidance\n"
        agents_path.write_text(original, encoding="utf-8")

        changed = OrchestraManager(orchestra_dir).context_sync(str(project_dir))

        assert changed == 0
        assert agents_path.read_text(encoding="utf-8") == original


class TestShadowWarnings:
    def test_sync_warns_for_project_and_ancestor_claude_files(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "outer" / "project"
        project_dir.mkdir(parents=True)
        _setup_context_sources(orchestra_dir)
        _setup_orchestra_json(project_dir)
        project_claude = project_dir / "CLAUDE.md"
        ancestor_claude = project_dir.parent / "CLAUDE.md"
        project_claude.write_text("project guidance\n", encoding="utf-8")
        ancestor_claude.write_text("ancestor guidance\n", encoding="utf-8")

        OrchestraManager(orchestra_dir).context_sync(str(project_dir))

        stderr = capsys.readouterr().err
        assert project_claude.exists()
        assert ancestor_claude.exists()
        assert str(project_claude.resolve()) in stderr
        assert str(ancestor_claude.resolve()) in stderr

    def test_sync_excludes_only_home_claude_file(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _setup_context_sources(orchestra_dir)
        _setup_orchestra_json(project_dir)
        monkeypatch.setattr(Path, "home", lambda: project_dir)
        home_claude = project_dir / ".claude" / "CLAUDE.md"
        project_claude = project_dir / "CLAUDE.md"
        home_claude.write_text("home guidance\n", encoding="utf-8")
        project_claude.write_text("project guidance\n", encoding="utf-8")

        OrchestraManager(orchestra_dir).context_sync(str(project_dir))

        stderr = capsys.readouterr().err
        assert home_claude.exists()
        assert project_claude.exists()
        assert str(home_claude.resolve()) not in stderr
        assert str(project_claude.resolve()) in stderr


class TestInstallContextInitFiles:
    def test_root_file_is_copied(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        template = orchestra_dir / "templates" / "project" / "AGENTS.md"
        template.parent.mkdir(parents=True)
        template.write_text("agents content", encoding="utf-8")
        _write_package_manifest(orchestra_dir, "core", _core_context_files())
        manager = OrchestraManager(orchestra_dir)

        manager._install_context_init_files(
            manager.load_packages()["core"],
            project_dir,
            dry_run=False,
        )

        destination = project_dir / "AGENTS.md"
        assert destination.is_file()
        assert destination.read_text(encoding="utf-8") == "agents content"

    def test_prefixed_file_is_copied(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        template_dir = orchestra_dir / "templates" / "project"
        template_dir.mkdir(parents=True)
        (template_dir / "config.toml").write_text("toml content", encoding="utf-8")
        _write_package_manifest(
            orchestra_dir,
            "core",
            _core_context_files(init=[".codex/config.toml"]),
        )
        manager = OrchestraManager(orchestra_dir)

        manager._install_context_init_files(
            manager.load_packages()["core"],
            project_dir,
            dry_run=False,
        )

        destination = project_dir / ".codex" / "config.toml"
        assert destination.is_file()
        assert destination.read_text(encoding="utf-8") == "toml content"

    def test_directory_entry_is_copied_recursively(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        skill_dir = orchestra_dir / "templates" / "project" / "skills" / "context-loader"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("skill content", encoding="utf-8")
        _write_package_manifest(
            orchestra_dir,
            "core",
            _core_context_files(init=[".codex/skills/context-loader/"]),
        )
        manager = OrchestraManager(orchestra_dir)

        manager._install_context_init_files(
            manager.load_packages()["core"],
            project_dir,
            dry_run=False,
        )

        destination = project_dir / ".codex" / "skills" / "context-loader" / "SKILL.md"
        assert destination.is_file()
        assert destination.read_text(encoding="utf-8") == "skill content"

    def test_missing_template_root_warns_without_crashing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        _write_package_manifest(
            orchestra_dir,
            "core",
            _core_context_files(template="templates/nonexistent/AGENTS.md"),
        )
        manager = OrchestraManager(orchestra_dir)

        manager._install_context_init_files(
            manager.load_packages()["core"],
            project_dir,
            dry_run=False,
        )

        assert "テンプレートディレクトリ" in capsys.readouterr().err
        assert not (project_dir / "AGENTS.md").exists()

    def test_existing_file_is_not_overwritten(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        template = orchestra_dir / "templates" / "project" / "AGENTS.md"
        template.parent.mkdir(parents=True)
        template.write_text("new content", encoding="utf-8")
        existing = project_dir / "AGENTS.md"
        existing.write_text("existing content", encoding="utf-8")
        _write_package_manifest(orchestra_dir, "core", _core_context_files())
        manager = OrchestraManager(orchestra_dir)

        manager._install_context_init_files(
            manager.load_packages()["core"],
            project_dir,
            dry_run=False,
        )

        assert existing.read_text(encoding="utf-8") == "existing content"

    def test_empty_init_list_is_noop(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        template = orchestra_dir / "templates" / "project" / "AGENTS.md"
        template.parent.mkdir(parents=True)
        template.write_text("content", encoding="utf-8")
        _write_package_manifest(
            orchestra_dir,
            "core",
            _core_context_files(init=[]),
        )
        manager = OrchestraManager(orchestra_dir)

        manager._install_context_init_files(
            manager.load_packages()["core"],
            project_dir,
            dry_run=False,
        )

        assert list(project_dir.iterdir()) == []

    def test_template_dir_copies_codex_files_without_adding_context_spec(
        self, tmp_path: Path
    ) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        codex_template_dir = orchestra_dir / "templates" / "codex"
        skill_dir = codex_template_dir / "skills" / "context-loader"
        skill_dir.mkdir(parents=True)
        (codex_template_dir / "config.toml").write_text(
            "model = 'example'\n",
            encoding="utf-8",
        )
        (skill_dir / "SKILL.md").write_text("skill content\n", encoding="utf-8")
        codex_suggestions = {
            "codex-suggestions": {
                "name": "codex-suggestions",
                "version": "0.0.0",
                "depends": [],
                "context_files": {
                    "template_dir": "templates/codex",
                    "init": [
                        ".codex/config.toml",
                        ".codex/skills/context-loader/",
                    ],
                },
            }
        }
        _setup_context_packages(orchestra_dir, extra_packages=codex_suggestions)
        manager = OrchestraManager(orchestra_dir)

        manager._install_context_init_files(
            manager.load_packages()["codex-suggestions"],
            project_dir,
            dry_run=False,
        )

        config_destination = project_dir / ".codex" / "config.toml"
        skill_destination = project_dir / ".codex" / "skills" / "context-loader" / "SKILL.md"
        assert config_destination.read_text(encoding="utf-8") == "model = 'example'\n"
        assert skill_destination.read_text(encoding="utf-8") == "skill content\n"
        assert len(manager.CONTEXT_SPECS) == 1
        assert manager.CONTEXT_SPECS[0].name == "agents"
        assert manager.CONTEXT_SPECS[0].required_pkg is None


class TestInitRunsContextSync:
    def test_init_creates_agents_via_context_sync_when_init_template_is_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        _setup_context_sources(orchestra_dir)
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        monkeypatch.setenv("AI_ORCHESTRA_DIR", str(orchestra_dir))
        manager = OrchestraManager(orchestra_dir)

        manager.init(str(project_dir))

        agents = (project_dir / "AGENTS.md").read_text(encoding="utf-8")
        assert "agents body" in agents
        assert "managed body" in agents
        assert manager.MANAGED_BLOCK_BEGIN in agents
        assert manager.MANAGED_BLOCK_END in agents


class TestSeverityDefinitionsExpansion:
    MARKER = "<!-- severity-definitions: output-contracts/tiered-review -->"
    SEVERITY_ROWS = (
        ("Critical", "critical criteria", "critical response"),
        ("High", "high criteria", "high response"),
        ("Medium", "medium criteria", "medium response"),
        ("Low", "low criteria", "low response"),
    )
    EXPECTED_LINES = (
        "  - Critical: critical criteria",
        "  - High: high criteria",
        "  - Medium: medium criteria",
        "  - Low: low criteria",
    )

    def _setup_marker_source(self, orchestra_dir: Path, marker: str | None = None) -> None:
        """重要度定義マーカーを管理 fragment に配置する。"""
        _setup_context_sources(orchestra_dir)
        marker_line = marker or self.MARKER
        (orchestra_dir / "templates" / "context" / "orchestra.md").write_text(
            f"## Managed\n\n- Severity labels:\n{marker_line}\n",
            encoding="utf-8",
        )

    def _write_facet(self, orchestra_dir: Path, padded: bool = False) -> Path:
        """前後に別セクションを持つ重要度定義 facet を作成する。"""
        facet_path = orchestra_dir / "facets" / "output-contracts" / "tiered-review.md"
        facet_path.parent.mkdir(parents=True, exist_ok=True)
        if padded:
            table_lines = [
                "| 重要度         | 基準              | 対応              |",
                "| -------------- | ----------------- | ----------------- |",
                *(
                    f"| **{severity}**{' ' * (10 - len(severity))} | "
                    f"{criteria:<17} | {response:<17} |"
                    for severity, criteria, response in self.SEVERITY_ROWS
                ),
            ]
        else:
            table_lines = [
                "| 重要度 | 基準 | 対応 |",
                "|---|---|---|",
                *(
                    f"| **{severity}** | {criteria} | {response} |"
                    for severity, criteria, response in self.SEVERITY_ROWS
                ),
            ]
        facet_path.write_text(
            "\n".join(
                [
                    "# Contract",
                    "",
                    "## Before",
                    "",
                    "before body",
                    "",
                    "## 重要度の定義",
                    "",
                    *table_lines,
                    "",
                    "## After",
                    "",
                    "after body",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return facet_path

    @staticmethod
    def _built_agents_path(orchestra_dir: Path) -> Path:
        """生成されたプロジェクト向け AGENTS.md のパスを返す。"""
        return orchestra_dir / "templates" / "project" / "AGENTS.md"

    @staticmethod
    def _repo_severity_lines() -> list[str]:
        """実リポジトリの重要度表を実装から独立した単純な方法で読む。"""
        facet_path = REPO_ROOT / "facets" / "output-contracts" / "tiered-review.md"
        content = facet_path.read_text(encoding="utf-8")
        section = content.split("## 重要度の定義\n", maxsplit=1)[1]
        section = section.split("\n## ", maxsplit=1)[0]
        table_rows = [line for line in section.splitlines() if line.startswith("|")]
        severity_lines: list[str] = []
        for row in table_rows[2:]:
            cells = [cell.strip() for cell in row.strip("|").split("|")]
            severity_lines.append(f"  - {cells[0].strip('*')}: {cells[1]}")
        assert len(severity_lines) == 4
        return severity_lines

    @staticmethod
    def _extract_severity_block(text: str) -> list[str]:
        """重要度ラベル行の直後に続く定義行ブロックを抽出する。"""
        lines = text.splitlines()
        label_prefix = "- 各指摘に重要度ラベルを付ける:"
        for index, line in enumerate(lines):
            if not line.startswith(label_prefix):
                continue
            block: list[str] = []
            for candidate in lines[index + 1 :]:
                if not candidate.startswith("  - "):
                    break
                block.append(candidate)
            return block
        pytest.fail(f"severity label line not found: {label_prefix!r}")

    def test_build_expands_marker_inside_managed_block(self, tmp_path: Path) -> None:
        self._setup_marker_source(tmp_path)
        self._write_facet(tmp_path)
        manager = OrchestraManager(tmp_path)

        manager.context_build()

        generated = self._built_agents_path(tmp_path).read_text(encoding="utf-8")
        managed_span = _extract_managed_span(manager, generated)
        for expected_line in self.EXPECTED_LINES:
            assert expected_line in managed_span
        assert "critical response" not in managed_span
        assert self.MARKER not in managed_span

    def test_sync_expands_marker_inside_managed_block(self, tmp_path: Path) -> None:
        orchestra_dir = tmp_path / "orchestra"
        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True)
        self._setup_marker_source(orchestra_dir)
        self._write_facet(orchestra_dir)
        _setup_orchestra_json(project_dir)
        manager = OrchestraManager(orchestra_dir)

        manager.context_sync(str(project_dir))

        agents = (project_dir / "AGENTS.md").read_text(encoding="utf-8")
        managed_span = _extract_managed_span(manager, agents)
        for expected_line in self.EXPECTED_LINES:
            assert expected_line in managed_span
        assert self.MARKER not in managed_span

    def test_facet_change_is_detected_and_rebuilt(self, tmp_path: Path) -> None:
        self._setup_marker_source(tmp_path)
        facet_path = self._write_facet(tmp_path)
        manager = OrchestraManager(tmp_path)
        manager.context_build()
        facet_content = facet_path.read_text(encoding="utf-8")
        facet_path.write_text(
            facet_content.replace("critical criteria", "updated critical criteria"),
            encoding="utf-8",
        )

        assert manager.context_check() is False
        manager.context_build()

        generated = self._built_agents_path(tmp_path).read_text(encoding="utf-8")
        managed_span = _extract_managed_span(manager, generated)
        assert "  - Critical: updated critical criteria" in managed_span

    def test_missing_facet_exits(self, tmp_path: Path) -> None:
        self._setup_marker_source(tmp_path)

        with pytest.raises(SystemExit):
            OrchestraManager(tmp_path).context_build()

    def test_unreadable_facet_exits(self, tmp_path: Path) -> None:
        self._setup_marker_source(tmp_path)
        facet_path = tmp_path / "facets" / "output-contracts" / "tiered-review.md"
        facet_path.mkdir(parents=True)

        with pytest.raises(SystemExit):
            OrchestraManager(tmp_path).context_build()

    def test_missing_severity_section_exits(self, tmp_path: Path) -> None:
        self._setup_marker_source(tmp_path)
        facet_path = tmp_path / "facets" / "output-contracts" / "tiered-review.md"
        facet_path.parent.mkdir(parents=True)
        facet_path.write_text("# Contract\n\n## Different section\n", encoding="utf-8")

        with pytest.raises(SystemExit):
            OrchestraManager(tmp_path).context_build()

    def test_empty_severity_table_exits(self, tmp_path: Path) -> None:
        self._setup_marker_source(tmp_path)
        facet_path = tmp_path / "facets" / "output-contracts" / "tiered-review.md"
        facet_path.parent.mkdir(parents=True)
        facet_path.write_text(
            "## 重要度の定義\n\n| 重要度 | 基準 | 対応 |\n|---|---|---|\n",
            encoding="utf-8",
        )

        with pytest.raises(SystemExit):
            OrchestraManager(tmp_path).context_build()

    @pytest.mark.parametrize(
        "malformed",
        [
            "<!-- severity-definitions: output-contracts/../x -->",
            "<!-- severity-definitions : output-contracts/tiered-review -->",
            "<!--severity-definitions:output-contracts/tiered-review-->",
        ],
    )
    def test_malformed_marker_exits(self, tmp_path: Path, malformed: str) -> None:
        self._setup_marker_source(tmp_path, marker=malformed)

        with pytest.raises(SystemExit):
            OrchestraManager(tmp_path).context_build()

    def test_table_body_row_with_too_few_columns_exits(self, tmp_path: Path) -> None:
        self._setup_marker_source(tmp_path)
        facet_path = tmp_path / "facets" / "output-contracts" / "tiered-review.md"
        facet_path.parent.mkdir(parents=True)
        facet_path.write_text(
            "## 重要度の定義\n\n| 重要度 | 基準 |\n|---|---|\n| **Critical** |\n",
            encoding="utf-8",
        )

        with pytest.raises(SystemExit):
            OrchestraManager(tmp_path).context_build()

    def test_escaped_pipe_in_basis_cell_is_preserved(self, tmp_path: Path) -> None:
        self._setup_marker_source(tmp_path)
        facet_path = tmp_path / "facets" / "output-contracts" / "tiered-review.md"
        facet_path.parent.mkdir(parents=True, exist_ok=True)
        facet_path.write_text(
            "\n".join(
                [
                    "## 重要度の定義",
                    "",
                    "| 重要度 | 基準 | 対応 |",
                    "|---|---|---|",
                    "| **Critical** | critical criteria | critical response |",
                    r"| **High** | A \| B | high response |",
                    "| **Medium** | medium criteria | medium response |",
                    "| **Low** | low criteria | low response |",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        OrchestraManager(tmp_path).context_build()

        generated = self._built_agents_path(tmp_path).read_text(encoding="utf-8")
        managed_span = _extract_managed_span(OrchestraManager(tmp_path), generated)
        assert "  - High: A | B" in managed_span
        assert "A \\" not in managed_span

    def test_prettier_aligned_table_matches_plain_table(self, tmp_path: Path) -> None:
        self._setup_marker_source(tmp_path)
        self._write_facet(tmp_path)
        manager = OrchestraManager(tmp_path)
        manager.context_build()
        plain_output = self._built_agents_path(tmp_path).read_text(encoding="utf-8")

        self._write_facet(tmp_path, padded=True)
        manager.context_build()
        padded_output = self._built_agents_path(tmp_path).read_text(encoding="utf-8")

        assert padded_output == plain_output
        managed_span = _extract_managed_span(manager, padded_output)
        for expected_line in self.EXPECTED_LINES:
            assert expected_line in managed_span

    def test_generated_project_template_matches_repo_severity_table(self) -> None:
        expected_lines = self._repo_severity_lines()
        generated = (REPO_ROOT / "templates" / "project" / "AGENTS.md").read_text(encoding="utf-8")

        assert self._extract_severity_block(generated) == expected_lines

    def test_root_agents_managed_block_matches_fresh_render(self) -> None:
        manager = OrchestraManager(REPO_ROOT)
        assert len(manager.CONTEXT_SPECS) == 1
        spec = manager.CONTEXT_SPECS[0]
        managed_body = manager._render_fragment_group(spec.managed_rels, "\n\n")
        expected_block = manager._build_managed_block(managed_body)
        root_agents = _read_text_preserving_newlines(REPO_ROOT / "AGENTS.md")

        assert _extract_managed_span(manager, root_agents) == expected_block

    def test_orchestra_context_uses_marker_instead_of_handwritten_rows(self) -> None:
        context_source = (REPO_ROOT / "templates" / "context" / "orchestra.md").read_text(
            encoding="utf-8"
        )

        assert self.MARKER in context_source
        assert "  - Critical:" not in context_source

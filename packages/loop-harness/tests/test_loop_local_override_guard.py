"""Unit tests for the project-local override snapshot/tamper-detection guard."""

from __future__ import annotations

import os
from pathlib import Path

from tests.module_loader import load_module

guard = load_module(
    "loop_local_override_guard_tests",
    "packages/loop-harness/lib/loop_local_override_guard.py",
)


def _seed_override(worktree_path: Path) -> Path:
    config_dir = worktree_path / ".claude" / "config"
    config_dir.mkdir(parents=True)
    override = config_dir / "cli-tools.local.yaml"
    override.write_text("codex:\n  model: trusted\n", encoding="utf-8")
    return override


def test_unrelated_new_top_level_directory_does_not_change_the_snapshot(tmp_path: Path) -> None:
    """Codex review, PR #262, High: creating a directory unrelated to `.claude/config` must not
    change the worktree root's recorded snapshot (its `link_count` changes, but that alone is
    not a local-override tamper signal).
    """
    _seed_override(tmp_path)

    before = guard.snapshot_local_overrides(tmp_path)
    (tmp_path / "brand-new-feature-dir").mkdir()
    after = guard.snapshot_local_overrides(tmp_path)

    assert guard.changed_local_override_paths(before, after) == []


def test_worktree_root_mode_change_is_still_detected(tmp_path: Path) -> None:
    """The ancestor chain up to `worktree_path` must still catch permission-bit tampering; only
    the volatile `link_count` field is neutralized for directories.
    """
    _seed_override(tmp_path)
    tmp_path.chmod(0o700)

    before = guard.snapshot_local_overrides(tmp_path)
    tmp_path.chmod(0o755)
    after = guard.snapshot_local_overrides(tmp_path)

    assert guard.changed_local_override_paths(before, after) == ["."]
    tmp_path.chmod(0o700)


def test_symlinked_override_target_content_change_is_detected(tmp_path: Path) -> None:
    """Codex review, PR #262, High: a symlinked `.local.yaml`'s snapshot must change when the
    resolved target's *content* changes, even though the symlink's own path text is unchanged.
    """
    config_dir = tmp_path / ".claude" / "config"
    config_dir.mkdir(parents=True)
    real_target = tmp_path / "real-override.yaml"
    real_target.write_text("codex:\n  model: trusted\n", encoding="utf-8")
    override_link = config_dir / "cli-tools.local.yaml"
    os.symlink(real_target, override_link)

    before = guard.snapshot_local_overrides(tmp_path)
    real_target.write_text("codex:\n  model: tampered\n", encoding="utf-8")
    after = guard.snapshot_local_overrides(tmp_path)

    changed = guard.changed_local_override_paths(before, after)
    assert ".claude/config/cli-tools.local.yaml" in changed

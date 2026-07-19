"""Unit tests for the project-local override snapshot/tamper-detection guard."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

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


def test_symlinked_override_target_tail_tamper_past_first_chunk_is_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR #262 push-front adversarial review, P2: `_resolved_symlink_target_digest()` used to
    cap its read at a fixed 10 MiB (`handle.read(_MAX_SYMLINK_TARGET_READ_BYTES)`), so tampering
    located past that cutoff in a larger target never reached the digest at all, and the
    snapshot carries no size field to catch the truncation independently. The fix streams the
    whole file through SHA-256 in fixed-size chunks instead, with no byte count past which
    tampering goes undetected. Building a real >10 MiB fixture here would make this test slow,
    so the chunk size is monkeypatched down to a few bytes instead: this proves the digest
    actually folds in bytes read *after* the first chunk (i.e. there is no early return once a
    "big enough" prefix has been read), which is exactly the property a >10 MiB fixture would
    also exercise, just at a size this test can afford.
    """
    monkeypatch.setattr(guard, "_SYMLINK_TARGET_HASH_CHUNK_BYTES", 8)
    config_dir = tmp_path / ".claude" / "config"
    config_dir.mkdir(parents=True)
    real_target = tmp_path / "real-override.yaml"
    # Several multiples of the patched chunk size, so the tampered byte below sits well past
    # the first chunk boundary.
    real_target.write_bytes(b"a" * 31 + b"\n")
    override_link = config_dir / "cli-tools.local.yaml"
    os.symlink(real_target, override_link)

    before = guard.snapshot_local_overrides(tmp_path)
    tampered = bytearray(real_target.read_bytes())
    tampered[-1] = ord("Z")  # last byte only, several chunks in
    real_target.write_bytes(bytes(tampered))
    after = guard.snapshot_local_overrides(tmp_path)

    changed = guard.changed_local_override_paths(before, after)
    assert ".claude/config/cli-tools.local.yaml" in changed


def test_symlinked_config_root_fails_closed(tmp_path: Path) -> None:
    """Codex review, PR #262, High (round 5): a symlinked `.claude/config` root must never
    silently produce an empty snapshot -- `load_config()` still follows it and treats the
    pointed-to `*.local.yaml`/`*.local.json` files as effective configuration, so this guard
    must fail closed rather than let `_verify_local_override_snapshot()` compare `()` to `()`
    forever.
    """
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    real_config_dir = tmp_path / "real-config"
    real_config_dir.mkdir()
    (real_config_dir / "cli-tools.local.yaml").write_text(
        "codex:\n  model: trusted\n", encoding="utf-8"
    )
    os.symlink(real_config_dir, claude_dir / "config")

    with pytest.raises(guard.LocalOverrideSnapshotError):
        guard.snapshot_local_overrides(tmp_path)


def test_symlinked_intermediate_directory_fails_closed(tmp_path: Path) -> None:
    """Codex review, PR #262, P2 (round 8, D4): a symlinked directory anywhere *under* the
    `.claude/config` root -- not just the root itself (round 5, above) -- must also fail closed.

    `os.walk(followlinks=False)` keeps this walk from descending into a symlinked subdirectory,
    but that alone just makes any `.local.yaml`/`.local.json` override sitting inside one
    silently invisible to this snapshot instead of merely unreachable: a Maker that swaps an
    intermediate directory under `.claude/config` for a symlink to elsewhere could add, remove,
    or edit overrides inside it with zero tamper-detection signal, since neither this run's nor
    any later run's snapshot ever surfaces them.
    """
    config_dir = tmp_path / ".claude" / "config"
    config_dir.mkdir(parents=True)
    real_nested = tmp_path / "real-nested"
    real_nested.mkdir()
    (real_nested / "cli-tools.local.yaml").write_text(
        "codex:\n  model: trusted\n", encoding="utf-8"
    )
    os.symlink(real_nested, config_dir / "nested")

    with pytest.raises(guard.LocalOverrideSnapshotError):
        guard.snapshot_local_overrides(tmp_path)

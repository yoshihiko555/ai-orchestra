"""旧 worktree-local fail-log の安全な一回限り移行を検証する。"""

from __future__ import annotations

from pathlib import Path

from tests.module_loader import load_module

log_migration = load_module(
    "log_migration",
    "packages/fail-logs/hooks/log_migration.py",
)

LOG_RELATIVE_DIR = ".claude/logs/fail-logs"
LOG_FILE_NAME = "failures.jsonl"


def _log_path(base: Path) -> Path:
    return base / LOG_RELATIVE_DIR / LOG_FILE_NAME


def _migrate(project: Path, root: Path) -> None:
    log_migration.migrate_legacy_worktree_log(
        str(project),
        str(root),
        LOG_RELATIVE_DIR,
        LOG_FILE_NAME,
    )


def test_migrates_once_and_preserves_original_bytes(tmp_path: Path) -> None:
    project = tmp_path / "project"
    root = tmp_path / "root"
    project.mkdir()
    root.mkdir()
    legacy_path = _log_path(project)
    legacy_path.parent.mkdir(parents=True)
    legacy_bytes = b'{"legacy":true}\n\xff\n'
    legacy_path.write_bytes(legacy_bytes)

    _migrate(project, root)

    migrated_path = legacy_path.with_name(f"{legacy_path.name}.migrated")
    assert not legacy_path.exists()
    assert migrated_path.read_bytes() == legacy_bytes
    assert _log_path(root).read_bytes() == legacy_bytes


def test_already_claimed_source_is_a_noop(tmp_path: Path) -> None:
    project = tmp_path / "project"
    root = tmp_path / "root"
    project.mkdir()
    root.mkdir()
    migrating_path = _log_path(project).with_name(f"{LOG_FILE_NAME}.migrating")
    migrating_path.parent.mkdir(parents=True)
    migrating_path.write_bytes(b"claimed\n")

    _migrate(project, root)

    assert migrating_path.read_bytes() == b"claimed\n"
    assert not _log_path(root).exists()


def test_failed_copy_leaves_stuck_claim_and_second_call_is_noop(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "project"
    root = tmp_path / "root"
    project.mkdir()
    root.mkdir()
    legacy_path = _log_path(project)
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_bytes(b"stuck\n")

    def _raise_copy_error(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated copy failure")

    monkeypatch.setattr(log_migration.shutil, "copyfileobj", _raise_copy_error)
    _migrate(project, root)

    migrating_path = legacy_path.with_name(f"{legacy_path.name}.migrating")
    destination_path = _log_path(root)
    assert not legacy_path.exists()
    assert migrating_path.read_bytes() == b"stuck\n"
    destination_before = destination_path.read_bytes()

    _migrate(project, root)

    assert migrating_path.read_bytes() == b"stuck\n"
    assert destination_path.read_bytes() == destination_before
    assert not legacy_path.with_name(f"{legacy_path.name}.migrated").exists()


def test_source_symlink_escape_is_rejected(tmp_path: Path) -> None:
    project = tmp_path / "project"
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    project.mkdir()
    root.mkdir()
    outside.mkdir()
    external_log = outside / LOG_FILE_NAME
    external_log.write_bytes(b"external\n")
    source_dir = _log_path(project).parent
    source_dir.parent.mkdir(parents=True)
    source_dir.symlink_to(outside, target_is_directory=True)

    _migrate(project, root)

    assert external_log.read_bytes() == b"external\n"
    assert not _log_path(root).exists()


def test_destination_symlink_escape_is_rejected(tmp_path: Path) -> None:
    project = tmp_path / "project"
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    project.mkdir()
    root.mkdir()
    outside.mkdir()
    legacy_path = _log_path(project)
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_bytes(b"legacy\n")
    destination_dir = _log_path(root).parent
    destination_dir.parent.mkdir(parents=True)
    destination_dir.symlink_to(outside, target_is_directory=True)

    _migrate(project, root)

    assert legacy_path.read_bytes() == b"legacy\n"
    assert not (outside / LOG_FILE_NAME).exists()


def test_same_log_root_is_a_noop(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    legacy_path = _log_path(project)
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_bytes(b"legacy\n")

    _migrate(project, project)

    assert legacy_path.read_bytes() == b"legacy\n"
    assert not legacy_path.with_name(f"{legacy_path.name}.migrated").exists()


def test_missing_legacy_file_is_a_noop(tmp_path: Path) -> None:
    project = tmp_path / "project"
    root = tmp_path / "root"
    project.mkdir()
    root.mkdir()

    _migrate(project, root)

    assert not _log_path(root).exists()


def test_destination_equal_to_source_realpath_is_a_noop(tmp_path: Path) -> None:
    project = tmp_path / "project"
    root = project / "nested-root"
    shared_dir = root / "shared"
    shared_dir.mkdir(parents=True)
    (project / "shared").symlink_to(shared_dir, target_is_directory=True)
    legacy_path = shared_dir / LOG_FILE_NAME
    legacy_path.write_bytes(b"same\n")

    log_migration.migrate_legacy_worktree_log(
        str(project),
        str(root),
        "shared",
        LOG_FILE_NAME,
    )

    assert legacy_path.read_bytes() == b"same\n"
    assert not legacy_path.with_name(f"{legacy_path.name}.migrated").exists()

"""Race-resistant candidate artifact reader tests."""

from __future__ import annotations

from pathlib import Path

from tests.module_loader import load_module

artifacts = load_module(
    "meta_harness_artifact_reader_tests",
    "packages/meta-harness/lib/artifact_reader.py",
)


def test_reads_regular_file_from_same_open_fd(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    path.write_bytes(b'{"ok":true}')
    artifact = artifacts.read_regular_artifact(tmp_path, path, max_bytes=100)
    assert artifact is not None
    assert artifact.data == b'{"ok":true}'


def test_rejects_leaf_symlink(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-artifact.txt"
    outside.write_text("secret", encoding="utf-8")
    linked = tmp_path / "linked.txt"
    linked.symlink_to(outside)
    assert artifacts.read_regular_artifact(tmp_path, linked, max_bytes=100) is None


def test_rejects_symlinked_parent_directory(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-artifact-dir"
    outside.mkdir(exist_ok=True)
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    (tmp_path / "linked-dir").symlink_to(outside, target_is_directory=True)
    path = tmp_path / "linked-dir" / "secret.txt"
    assert artifacts.read_regular_artifact(tmp_path, path, max_bytes=100) is None


def test_rejects_oversized_regular_file(tmp_path: Path) -> None:
    path = tmp_path / "large.txt"
    path.write_bytes(b"12345")
    assert artifacts.read_regular_artifact(tmp_path, path, max_bytes=4) is None

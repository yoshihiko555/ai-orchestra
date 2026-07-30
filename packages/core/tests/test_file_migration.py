"""core.file_migration の有界移行プリミティブを直接検証する。"""

from __future__ import annotations

from pathlib import Path
from typing import BinaryIO

from tests.module_loader import load_module

file_migration = load_module(
    "file_migration",
    "packages/core/hooks/file_migration.py",
)


def _copy_all_writer(source: BinaryIO, destination_path: str) -> None:
    """テスト用の最小 writer: 残りバイトを一括で追記する。"""
    with open(destination_path, "ab") as destination:
        destination.write(source.read())


def test_migrates_full_content_and_finalizes_claim(tmp_path: Path) -> None:
    source_path = tmp_path / "source.jsonl"
    destination_path = tmp_path / "destination.jsonl"
    source_path.write_bytes(b"line-1\nline-2\n")

    file_migration.migrate_bounded_file(
        str(source_path),
        str(destination_path),
        max_bytes=1024,
        writer=_copy_all_writer,
    )

    migrated_matches = list(tmp_path.glob("source.jsonl.migrated.*"))
    migrating_matches = list(tmp_path.glob("source.jsonl.migrating.*"))
    assert not source_path.exists()
    assert len(migrated_matches) == 1
    assert migrated_matches[0].read_bytes() == b"line-1\nline-2\n"
    assert migrating_matches == []
    assert destination_path.read_bytes() == b"line-1\nline-2\n"


def test_same_realpath_source_and_destination_is_a_noop(tmp_path: Path) -> None:
    shared = tmp_path / "shared.jsonl"
    shared.write_bytes(b"unchanged\n")

    file_migration.migrate_bounded_file(
        str(shared),
        str(shared),
        max_bytes=1024,
        writer=_copy_all_writer,
    )

    assert shared.read_bytes() == b"unchanged\n"
    assert list(tmp_path.glob("shared.jsonl.migrating.*")) == []
    assert list(tmp_path.glob("shared.jsonl.migrated.*")) == []


def test_claim_suffix_is_unique_per_call(tmp_path: Path) -> None:
    seen_suffixes: set[str] = set()
    for index in range(3):
        source_path = tmp_path / f"source-{index}.jsonl"
        destination_path = tmp_path / f"destination-{index}.jsonl"
        source_path.write_bytes(f"payload-{index}\n".encode())

        file_migration.migrate_bounded_file(
            str(source_path),
            str(destination_path),
            max_bytes=1024,
            writer=_copy_all_writer,
        )

        migrated = list(tmp_path.glob(f"source-{index}.jsonl.migrated.*"))
        assert len(migrated) == 1
        suffix = migrated[0].name.split(".migrated.", 1)[1]
        assert suffix not in seen_suffixes
        seen_suffixes.add(suffix)


def test_stale_migrating_claim_is_left_untouched(tmp_path: Path) -> None:
    source_path = tmp_path / "source.jsonl"
    destination_path = tmp_path / "destination.jsonl"
    stale_claim = tmp_path / "source.jsonl.migrating.stale-process"
    stale_claim.write_bytes(b"stale\n")
    source_path.write_bytes(b"fresh\n")

    file_migration.migrate_bounded_file(
        str(source_path),
        str(destination_path),
        max_bytes=1024,
        writer=_copy_all_writer,
    )

    migrating_matches = list(tmp_path.glob("source.jsonl.migrating.*"))
    migrated_matches = list(tmp_path.glob("source.jsonl.migrated.*"))
    assert migrating_matches == [stale_claim]
    assert stale_claim.read_bytes() == b"stale\n"
    assert len(migrated_matches) == 1
    assert migrated_matches[0].read_bytes() == b"fresh\n"
    assert destination_path.read_bytes() == b"fresh\n"


def test_writer_exception_leaves_migrating_claim_and_propagates(tmp_path: Path) -> None:
    source_path = tmp_path / "source.jsonl"
    destination_path = tmp_path / "destination.jsonl"
    source_path.write_bytes(b"payload\n")

    def _raising_writer(_source: BinaryIO, _destination_path: str) -> None:
        raise OSError("simulated writer failure")

    try:
        file_migration.migrate_bounded_file(
            str(source_path),
            str(destination_path),
            max_bytes=1024,
            writer=_raising_writer,
        )
        raised = False
    except OSError:
        raised = True

    migrating_matches = list(tmp_path.glob("source.jsonl.migrating.*"))
    migrated_matches = list(tmp_path.glob("source.jsonl.migrated.*"))
    assert raised
    assert not source_path.exists()
    assert len(migrating_matches) == 1
    assert migrating_matches[0].read_bytes() == b"payload\n"
    assert migrated_matches == []


def test_empty_source_file_migrates_to_empty_destination(tmp_path: Path) -> None:
    source_path = tmp_path / "source.jsonl"
    destination_path = tmp_path / "destination.jsonl"
    source_path.write_bytes(b"")

    file_migration.migrate_bounded_file(
        str(source_path),
        str(destination_path),
        max_bytes=1024,
        writer=_copy_all_writer,
    )

    migrated_matches = list(tmp_path.glob("source.jsonl.migrated.*"))
    assert len(migrated_matches) == 1
    assert migrated_matches[0].read_bytes() == b""
    assert destination_path.read_bytes() == b""


def test_tail_is_capped_at_line_boundary_when_exceeding_max_bytes(tmp_path: Path) -> None:
    source_path = tmp_path / "source.jsonl"
    destination_path = tmp_path / "destination.jsonl"
    line_bytes = b'{"n":"00000000"}\n'
    line_count = 200
    content = line_bytes * line_count
    max_bytes = len(content) - (3 * len(line_bytes)) - 2  # 行境界をまたぐ半端な位置

    source_path.write_bytes(content)

    file_migration.migrate_bounded_file(
        str(source_path),
        str(destination_path),
        max_bytes=max_bytes,
        writer=_copy_all_writer,
    )

    destination_bytes = destination_path.read_bytes()
    destination_lines = destination_bytes.splitlines(keepends=True)
    assert destination_bytes
    assert destination_bytes.endswith(b"\n")
    assert len(destination_bytes) <= max_bytes
    assert all(line == line_bytes for line in destination_lines)
    assert content.endswith(destination_bytes)


def test_long_unterminated_first_line_degrades_to_empty_tail(tmp_path: Path) -> None:
    source_path = tmp_path / "source.jsonl"
    destination_path = tmp_path / "destination.jsonl"
    # 改行が一切無い巨大な単一行。有界化すると readline() が EOF まで到達し、
    # writer に渡るデータは空になる決定的な縮退動作を検証する。
    source_path.write_bytes(b"x" * 2048)

    file_migration.migrate_bounded_file(
        str(source_path),
        str(destination_path),
        max_bytes=1024,
        writer=_copy_all_writer,
    )

    migrated_matches = list(tmp_path.glob("source.jsonl.migrated.*"))
    assert len(migrated_matches) == 1
    assert migrated_matches[0].read_bytes() == b"x" * 2048
    assert destination_path.read_bytes() == b""

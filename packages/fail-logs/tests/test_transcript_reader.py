"""セッションの記録の安全な読み込み（lib/transcript_reader.py）を検証する。

評価セット docs/evaluation/fail-logs.md の EV-46・EV-56 に対応する（設計 §7.3・§7.7）。
"""

from __future__ import annotations

import json
import os

import lessons_helpers as h

reader = h.reader


def _write(path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


def _user(text: str, uuid: str) -> dict:
    return {
        "type": "user",
        "uuid": uuid,
        "isSidechain": False,
        "message": {"role": "user", "content": text},
    }


# EV-56: 許可するルートの配下の通常ファイルだけを開く（外・symlink・相対パスは開かない）。
def test_open_transcript_constraints(tmp_path) -> None:
    root = tmp_path / "projects"
    inside = root / "p" / "s.jsonl"
    _write(inside, [_user("hi", "u1")])
    outside = tmp_path / "other.jsonl"
    _write(outside, [_user("hi", "u1")])
    link = root / "p" / "link.jsonl"
    os.symlink(outside, link)

    fd = reader.open_transcript(str(inside), [str(root)])
    assert fd is not None
    os.close(fd)
    assert reader.open_transcript(str(outside), [str(root)]) is None
    assert reader.open_transcript(str(link), [str(root)]) is None
    assert reader.open_transcript("p/s.jsonl", [str(root)]) is None
    assert reader.open_transcript(None, [str(root)]) is None


# EV-46: 前回の位置から読み、行の途中では終えない。上限を超えるときは末尾から、行の頭にそろえて読む。
def test_read_slice_offsets(tmp_path) -> None:
    path = tmp_path / "projects" / "p" / "s.jsonl"
    _write(path, [_user("one", "u1"), _user("two", "u2")])
    with open(path, "a") as handle:
        handle.write('{"type": "user", "uuid": "u3"')
    fd = reader.open_transcript(str(path), [str(tmp_path / "projects")])
    try:
        first = reader.read_slice(fd, 0, 10**6)
        assert [record["uuid"] for record in first.records] == ["u1", "u2"]
        again = reader.read_slice(fd, first.end_offset, 10**6)
        assert again.records == []
        tail = reader.read_slice(fd, 0, len(json.dumps(_user("two", "u2"))) + 40)
        assert [record["uuid"] for record in tail.records] == ["u2"]
    finally:
        os.close(fd)


# EV-46: フォークで複写された親の行は、最後に既知の uuid と一致した行の次から読む。
def test_skip_copied_prefix() -> None:
    records = [_user("a", "u1"), _user("b", "u2"), _user("c", "u3")]
    assert [r["uuid"] for r in reader.skip_copied_prefix(records, {"u2"})] == ["u3"]
    assert reader.skip_copied_prefix(records, set()) == records
    assert reader.skip_copied_prefix(records, {"zz"}) == records
    assert reader.last_uuid(records) == "u3"


# EV-45: 役割は記録の構造で決める（ツール結果・ツール呼び出し・テキスト）。
def test_classify_structural_roles() -> None:
    records = [
        _user("お願い", "u1"),
        {
            "type": "assistant",
            "uuid": "a1",
            "message": {
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}
                ]
            },
        },
        {
            "type": "user",
            "uuid": "u2",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "x", "is_error": True}
                ]
            },
        },
    ]
    messages = reader.classify(records, frozenset())
    assert [(m.role, m.kind) for m in messages] == [
        ("user", "text"),
        ("assistant", "tool_use"),
        ("tool", "tool_result"),
    ]
    assert messages[1].command == "ls"
    assert messages[2].is_error is True
    assert messages[0].human is True


# EV-56（レビュー指摘）: ルートより上のディレクトリが symlink（~/.claude を dotfiles で管理する等）でも読める。
def test_symlinked_root_directory_is_accepted(tmp_path) -> None:
    real_root = tmp_path / "real-config" / "projects"
    _write(real_root / "p" / "s.jsonl", [_user("hi", "u1")])
    linked = tmp_path / "linked-config"
    os.symlink(tmp_path / "real-config", linked)

    fd = reader.open_transcript(
        str(linked / "projects" / "p" / "s.jsonl"), [str(linked / "projects")]
    )
    assert fd is not None
    os.close(fd)

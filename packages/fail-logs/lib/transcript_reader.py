"""セッションの記録（transcript）の安全な読み込みと、発言の構造的な分類（設計 docs/design/fail-logs.md §7.3・§7.7）。

記録の形は公開仕様ではないため、解析できない行は飛ばす。役割（利用者・エージェント・ツール）は
記録の構造（``type``・ブロックの種類・``isMeta`` など）で決め、本文の中身では決めない。
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass, field

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_TOOL = "tool"
KIND_TEXT = "text"
KIND_TOOL_USE = "tool_use"
KIND_TOOL_RESULT = "tool_result"

MAX_BLOCK_CHARS = 64 * 1024
_TAG_START = re.compile(r"^\s*<[a-z][\w-]*[\s>]")


@dataclass
class Message:
    """抜粋と前段判定に使う 1 件の発言（またはツールの呼び出し・結果）。"""

    index: int
    role: str
    kind: str
    text: str
    uuid: str = ""
    tool_use_id: str = ""
    tool_name: str = ""
    command: str = ""
    is_error: bool = False
    human: bool = False
    interrupt: bool = False
    signals: list[str] = field(default_factory=list)


@dataclass
class TranscriptSlice:
    """読み込んだ範囲。``end_offset`` は読み終えた位置（完全な行の終わり）。"""

    records: list[dict]
    end_offset: int
    parsed: int
    skipped: int


def _within(path: str, roots: list[str]) -> bool:
    for root in roots:
        real_root = os.path.realpath(root)
        if path == real_root or path.startswith(real_root + os.sep):
            return True
    return False


def open_transcript(path: object, roots: list[str]) -> int | None:
    """許可するルートの配下の、自分の uid が所有する通常ファイルだけを開く（symlink はたどらない）。"""
    if not isinstance(path, str) or not path or not os.path.isabs(path):
        return None
    # 途中のディレクトリの symlink（~/.claude を dotfiles で管理する環境など）は解決してからルートの配下か確かめる。
    # 最後の要素の symlink は O_NOFOLLOW で開かない。
    real = os.path.realpath(path)
    if not _within(real, roots) or os.path.islink(path):
        return None
    try:
        fd = os.open(real, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
    except OSError:
        os.close(fd)
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        os.close(fd)
        return None
    return fd


def read_slice(fd: int, start_offset: int, max_bytes: int) -> TranscriptSlice:
    """``start_offset`` 以降を、末尾から最大 ``max_bytes`` まで読む。行の途中から始めない・終えない。"""
    size = os.fstat(fd).st_size
    start = max(0, min(start_offset, size))
    skip_partial = False
    if size - start > max_bytes:
        start = size - max_bytes
        skip_partial = True
    os.lseek(fd, start, os.SEEK_SET)
    buffer = bytearray()
    remaining = size - start
    while remaining > 0:
        part = os.read(fd, min(remaining, 1 << 20))
        if not part:
            break
        buffer += part
        remaining -= len(part)
    chunk = bytes(buffer)
    if skip_partial:
        newline = chunk.find(b"\n")
        if newline < 0:
            return TranscriptSlice([], start_offset, 0, 0)
        start += newline + 1
        chunk = chunk[newline + 1 :]
    consumed = chunk.rfind(b"\n") + 1
    records: list[dict] = []
    skipped = 0
    for raw in chunk[:consumed].split(b"\n"):
        if not raw.strip():
            continue
        try:
            record = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            skipped += 1
            continue
        if isinstance(record, dict):
            records.append(record)
        else:
            skipped += 1
    return TranscriptSlice(records, start + consumed, len(records), skipped)


def skip_copied_prefix(records: list[dict], known_last_uuids: set[str]) -> list[dict]:
    """フォークで複写された親の行を飛ばす。最後に既知の uuid と一致した行の次から返す（設計 §7.3）。"""
    if not known_last_uuids:
        return records
    last_match = -1
    for position, record in enumerate(records):
        if record.get("uuid") in known_last_uuids:
            last_match = position
    return records[last_match + 1 :]


def _block_text(content: object) -> str:
    if isinstance(content, str):
        return content[:MAX_BLOCK_CHARS]
    if isinstance(content, list):
        parts = [
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        return "\n".join(parts)[:MAX_BLOCK_CHARS]
    return ""


def _is_human_text(text: str, interrupt_markers: frozenset[str]) -> bool:
    if not text.strip() or text.strip() in interrupt_markers:
        return False
    return not _TAG_START.match(text)


def _user_messages(record: dict, start: int, interrupt_markers: frozenset[str]) -> list[Message]:
    content = (record.get("message") or {}).get("content")
    uuid_value = str(record.get("uuid") or "")
    automatic = record.get("isMeta") is True or record.get("isCompactSummary") is True
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
    if not isinstance(blocks, list):
        return []
    messages: list[Message] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        index = start + len(messages)
        if block.get("type") == "tool_result":
            messages.append(
                Message(
                    index,
                    ROLE_TOOL,
                    KIND_TOOL_RESULT,
                    _block_text(block.get("content")),
                    uuid=uuid_value,
                    tool_use_id=str(block.get("tool_use_id") or ""),
                    is_error=block.get("is_error") is True,
                )
            )
        elif block.get("type") == "text" and not automatic:
            text = str(block.get("text") or "")[:MAX_BLOCK_CHARS]
            interrupt = text.strip() in interrupt_markers
            human = _is_human_text(text, interrupt_markers)
            if interrupt or human:
                messages.append(
                    Message(
                        index,
                        ROLE_USER,
                        KIND_TEXT,
                        text,
                        uuid=uuid_value,
                        human=human,
                        interrupt=interrupt,
                    )
                )
    return messages


def _assistant_messages(record: dict, start: int) -> list[Message]:
    content = (record.get("message") or {}).get("content")
    if not isinstance(content, list):
        return []
    uuid_value = str(record.get("uuid") or "")
    messages: list[Message] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        index = start + len(messages)
        if block.get("type") == "text":
            messages.append(
                Message(
                    index,
                    ROLE_ASSISTANT,
                    KIND_TEXT,
                    _block_text(block.get("text")),
                    uuid=uuid_value,
                )
            )
        elif block.get("type") == "tool_use":
            tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
            name = str(block.get("name") or "")
            command = str(tool_input.get("command") or "") if name == "Bash" else ""
            summary = command or json.dumps(tool_input, ensure_ascii=False)[:2000]
            messages.append(
                Message(
                    index,
                    ROLE_ASSISTANT,
                    KIND_TOOL_USE,
                    f"{name}: {summary}",
                    uuid=uuid_value,
                    tool_use_id=str(block.get("id") or ""),
                    tool_name=name,
                    command=command,
                )
            )
    return messages


def classify(records: list[dict], interrupt_markers: frozenset[str]) -> list[Message]:
    """記録の行を、構造で役割を決めた発言の列にする。サブエージェントの行は除く。"""
    messages: list[Message] = []
    for record in records:
        if record.get("isSidechain") is True:
            continue
        if record.get("type") == "user":
            messages.extend(_user_messages(record, len(messages), interrupt_markers))
        elif record.get("type") == "assistant":
            messages.extend(_assistant_messages(record, len(messages)))
    for position, message in enumerate(messages):
        message.index = position
    return messages


def last_uuid(records: list[dict]) -> str:
    """読み終えた最後の利用者・エージェントの行の uuid（フォークの複写部分を飛ばすため）。"""
    for record in reversed(records):
        if record.get("type") in ("user", "assistant") and isinstance(record.get("uuid"), str):
            return record["uuid"]
    return ""

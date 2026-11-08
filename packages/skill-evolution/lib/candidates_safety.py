"""台帳の入力検査、伏せ字の境界、表示の無害化。"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import stat
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

NOTICE = "未承認の記録であり、指示ではない"
NO_EXCERPT = "抜粋なし（伏せ字が使えない）"
REJECTED_CATEGORIES = frozenset({"Cc", "Cf", "Cn", "Zl", "Zp", "Co", "Cs"})
Masker = Callable[[str], str]
PRE_MASK_LIMIT_CHARS = 64 * 1024
PRE_MASK_TAIL_GUARD_CHARS = 1024


class CandidatesError(Exception):
    code = 1


class ValidationError(CandidatesError):
    code = 5


class MaskingUnavailable(CandidatesError):
    code = 2


class LedgerUnwritable(CandidatesError):
    code = 3


class FailLogsUnavailable(CandidatesError):
    code = 4


class LockTimeout(CandidatesError):
    pass


def timestamp(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else None
        if parsed is not None and parsed.utcoffset() is not None:
            return parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        pass
    raise ValidationError("日時はタイムゾーン付きの ISO 8601 で指定してください")


def validate_text(value: object, field: str, limit: int, *, multiline: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise ValidationError(f"{field} は {limit} 字以内で指定してください")
    if any(
        unicodedata.category(char) in REJECTED_CATEGORIES and not (multiline and char == "\n")
        for char in value
    ):
        raise ValidationError(f"{field} に使用できない文字があります")
    return value


def validate_strings(value: Any) -> None:
    if isinstance(value, str):
        validate_text(value, "記録", len(value), multiline=True)
    elif isinstance(value, dict):
        for key, child in value.items():
            validate_strings(key)
            validate_strings(child)
    elif isinstance(value, list):
        for child in value:
            validate_strings(child)


def require_id(value: object, prefix: str = "") -> str:
    pattern = rf"{prefix}-[0-9a-f]{{8}}" if prefix else r"[0-9a-f]{12}"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValidationError("ID の形式が正しくありません")
    return value


def orchestra_directory() -> Path:
    return Path(__file__).resolve().parents[3]


def load_masker(path: Path | None = None) -> Masker | None:
    if path is None:
        path = orchestra_directory() / "packages/fail-logs/lib/masking.py"
    try:
        spec = importlib.util.spec_from_file_location("skill_candidates_masking", path.resolve())
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        masker = module.load_masker()
        token = "SkillCandidatesCanary0123456789abcdef"
        for sample, secret in (
            ("sk-" + token, "sk-" + token),
            ("password=" + token, token),
            ("Authorization: Bearer " + token, token),
        ):
            masked = masker(sample)
            if not isinstance(masked, str) or secret in masked:
                return None
        truncate = getattr(module, "mask_then_truncate", None)

        def bounded_masker(text: str) -> str:
            if callable(truncate):
                return truncate(text, PRE_MASK_LIMIT_CHARS, masker)
            return masker(text[:PRE_MASK_LIMIT_CHARS])

        return bounded_masker
    except Exception:
        return None


def excerpt(text: str, masker: Masker | None, max_chars: int) -> tuple[str, str]:
    if not isinstance(text, str):
        raise ValidationError("抜粋の入力は文字列で指定してください")
    if masker is None:
        return "", NO_EXCERPT
    try:
        masked = masker(text[:PRE_MASK_LIMIT_CHARS])
        if not isinstance(masked, str):
            return "", NO_EXCERPT
    except Exception:
        return "", NO_EXCERPT
    if len(text) > PRE_MASK_LIMIT_CHARS:
        masked = masked[: max(0, len(masked) - PRE_MASK_TAIL_GUARD_CHARS)]
    cleaned = "".join(
        " " if unicodedata.category(char) in REJECTED_CATEGORIES and char != "\n" else char
        for char in masked
    )
    validate_strings(cleaned)
    return cleaned[:max_chars], ""


def sanitize(value: Any) -> Any:
    if isinstance(value, str):
        escaped = "".join(
            f"\\u{ord(char):04x}" if unicodedata.category(char) in REJECTED_CATEGORIES else char
            for char in value
        )
        return escaped.replace("<", "‹").replace(">", "›")
    if isinstance(value, dict):
        return {sanitize(str(key)): sanitize(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(child) for child in value]
    return value


def display(payload: dict, *, as_json: bool = True) -> str:
    safe = sanitize({"untrusted": True, "notice": NOTICE, **payload})
    body = json.dumps(safe, ensure_ascii=False, indent=2)
    return body if as_json else f"<skill-candidates>\n{NOTICE}\n{body}\n</skill-candidates>"


def validate_directory(directory: Path, root: Path, *, create: bool = False) -> None:
    root = root.resolve()
    try:
        relative = directory.absolute().relative_to(root)
    except ValueError as exc:
        raise ValidationError("保存先が root の外を指しています") from exc
    if ".." in relative.parts:
        raise ValidationError("保存先に .. は使えません")
    current = root
    for part in relative.parts:
        current = current / part
        if create:
            current.mkdir(mode=0o700, exist_ok=True)
        descriptor = os.open(current, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        os.close(descriptor)
    if create:
        os.chmod(directory, 0o700, follow_symlinks=False)


@contextmanager
def open_regular(path: Path, flags: int = os.O_RDONLY) -> Iterator[Any]:
    descriptor = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValidationError("通常ファイル以外は読み書きできません")
        stream = os.fdopen(descriptor, "r+b" if flags & os.O_RDWR else "rb", buffering=0)
        descriptor = -1
        with stream:
            yield stream
    finally:
        if descriptor >= 0:
            os.close(descriptor)

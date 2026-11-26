"""添付ファイルを読み込み、プロバイダへ渡すデータへ変換する。"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from .types import Attachment, LLMError

SUPPORTED_MIME_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/webp", "image/gif", "application/pdf"}
)


def read_attachment(path: Path) -> Attachment:
    mime_type, _ = mimetypes.guess_type(path.name)
    if mime_type not in SUPPORTED_MIME_TYPES:
        raise LLMError(
            "unsupported_attachment",
            "未対応の添付形式です。PNG/JPEG/WebP/GIF/PDF を指定してください。",
        )
    try:
        data = path.read_bytes()
    except OSError:
        raise LLMError("unsupported_attachment", "添付ファイルを読み込めません。") from None
    return Attachment(path.name, mime_type, data)

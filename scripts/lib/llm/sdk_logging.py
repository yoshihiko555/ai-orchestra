"""SDK の診断ログを、本文やヘッダを評価せず固定文字列へ置換する。"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager


class _SDKLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # getMessage / formatException は本文やヘッダ、SDK例外を文字列化してしまう。
        record.msg = "[REDACTED]"
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


@contextmanager
def redact_sdk_logs() -> Iterator[None]:
    # レコード生成時に保護し、呼び出し中に作られる logger も同じ仕組みで覆う。
    prefixes = (
        "anthropic",
        "openai",
        "google_genai",
        "any_llm",
        "httpx",
        "httpx2",
        "httpcore",
        "httpcore2",
    )
    credential_filter = _SDKLogFilter()
    original_factory = logging.getLogRecordFactory()

    def record_factory(*args, **kwargs):
        record = original_factory(*args, **kwargs)
        if any(
            record.name == prefix or record.name.startswith(prefix + ".") for prefix in prefixes
        ):
            credential_filter.filter(record)
        return record

    logging.setLogRecordFactory(record_factory)
    try:
        yield
    finally:
        logging.setLogRecordFactory(original_factory)

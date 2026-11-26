"""SDK の診断ログから、その呼び出しで使う認証値を取り除く。"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager


class _CredentialFilter(logging.Filter):
    def __init__(self, api_key: str):
        super().__init__()
        self.api_key = api_key

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = record.getMessage().replace(self.api_key, "[REDACTED]")
        record.args = ()
        if record.exc_info:
            # raw exception を残すと、後続 handler が再び秘密値を描画してしまう。
            record.exc_text = logging.Formatter().formatException(record.exc_info)
            record.exc_info = None
        if record.exc_text:
            record.exc_text = record.exc_text.replace(self.api_key, "[REDACTED]")
        if record.stack_info:
            record.stack_info = record.stack_info.replace(self.api_key, "[REDACTED]")
        return True


@contextmanager
def redact_sdk_logs(api_key: str) -> Iterator[None]:
    # SDK を import した後で取り付ける。root の filter は子 logger に伝播しない。
    prefixes = ("anthropic", "openai", "google_genai", "httpx", "httpx2", "httpcore", "httpcore2")
    loggers = [
        logger
        for name, logger in list(logging.Logger.manager.loggerDict.items())
        if isinstance(logger, logging.Logger)
        and any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes)
    ]
    credential_filter = _CredentialFilter(api_key)
    for logger in loggers:
        logger.addFilter(credential_filter)
    try:
        yield
    finally:
        for logger in loggers:
            logger.removeFilter(credential_filter)

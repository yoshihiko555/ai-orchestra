"""SDK の診断ログを秘匿し、呼び出し後には filter を取り除く。"""

import logging

import pytest
from lib.llm.sdk_logging import redact_sdk_logs


@pytest.mark.parametrize(
    "logger_name", ["anthropic._base_client", "openai._base_client", "google_genai._api_client"]
)
def test_sdk_logging_redacts_message_exception_and_stack_and_restores_filter(logger_name, caplog):
    logger = logging.getLogger(logger_name)
    original_filters = list(logger.filters)
    caplog.set_level(logging.DEBUG)
    key = "dummy-private-credentialmarker"

    with pytest.raises(RuntimeError), redact_sdk_logs(key):
        try:
            raise ValueError(key)
        except ValueError:
            logger.exception("認証失敗: %s", key)
        record = logger.makeRecord(logger.name, logging.DEBUG, __file__, 1, key, (), None)
        record.stack_info = key
        logger.handle(record)
        logging.getLogger("orchex_test").warning("SDK 外の通常の診断")
        raise RuntimeError("処理の中断")

    assert "[REDACTED]" in caplog.text
    assert key not in caplog.text
    assert "SDK 外の通常の診断" in caplog.text
    assert logger.filters == original_filters
    logger.debug("処理後の診断")
    assert "処理後の診断" in caplog.text

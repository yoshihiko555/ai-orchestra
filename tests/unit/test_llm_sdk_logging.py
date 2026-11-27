"""SDK の診断ログを生成時に秘匿し、呼び出し後には factory を復元する。"""

import logging

import pytest
from lib.llm.sdk_logging import redact_sdk_logs


@pytest.mark.parametrize(
    "logger_name",
    ["anthropic._base_client", "openai._base_client", "google_genai._api_client", "any_llm.test"],
)
def test_sdk_logging_redacts_message_exception_and_stack_and_restores_factory(logger_name, caplog):
    logger = logging.getLogger(logger_name)
    original_factory = logging.getLogRecordFactory()
    caplog.set_level(logging.DEBUG)
    key = "dummy-private-credentialmarker"

    with pytest.raises(RuntimeError), redact_sdk_logs():
        try:
            raise ValueError(key)
        except ValueError:
            logger.exception("認証失敗: %s", key)
        record = logger.makeRecord(
            logger.name, logging.DEBUG, __file__, 1, key, (), None, sinfo=key
        )
        logger.handle(record)
        logging.getLogger("orchex_test").warning("SDK 外の通常の診断")
        raise RuntimeError("処理の中断")

    assert "[REDACTED]" in caplog.text
    assert key not in caplog.text
    assert "SDK 外の通常の診断" in caplog.text
    sdk_records = [record for record in caplog.records if record.name == logger_name]
    assert len(sdk_records) == 2
    assert [
        (record.msg, record.args, record.exc_info, record.exc_text, record.stack_info)
        for record in sdk_records
    ] == [("[REDACTED]", (), None, None, None)] * 2
    assert logging.getLogRecordFactory() is original_factory
    logger.debug("処理後の診断")
    assert "処理後の診断" in caplog.text


def test_sdk_logging_redacts_late_logger_without_formatting_private_values(caplog):
    class PrivateValue:
        def __str__(self):
            raise AssertionError("診断値を文字列化してはいけません。")

        def __repr__(self):
            raise AssertionError("診断値をreprにしてはいけません。")

    caplog.set_level(logging.DEBUG)
    original_factory = logging.getLogRecordFactory()
    with redact_sdk_logs():
        logger = logging.getLogger("any_llm.new_transport_for_test")
        logger.error("応答: %s", PrivateValue())

    assert "[REDACTED]" in caplog.text
    assert "応答:" not in caplog.text
    assert logging.getLogRecordFactory() is original_factory

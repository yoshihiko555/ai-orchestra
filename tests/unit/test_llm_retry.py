"""レート制限だけを、指定された回数と待ち時間で再試行する契約。"""

from unittest.mock import Mock, call

import pytest
from lib.llm.types import LLMError


def test_retry_returns_success_without_waiting():
    from lib.llm.retry import call_with_rate_limit_retry

    operation = Mock(return_value="回答")
    sleep = Mock()

    result = call_with_rate_limit_retry(
        operation, max_attempts=3, initial_delay_sec=0.25, sleep=sleep
    )

    assert result == "回答"
    operation.assert_called_once_with()
    sleep.assert_not_called()


@pytest.mark.parametrize(("backoff", "delays"), [(2.0, [0.25, 0.5]), (3.0, [0.25, 0.75])])
def test_rate_limit_retry_increases_delay_and_returns_recovered_answer(backoff, delays):
    from lib.llm.retry import call_with_rate_limit_retry

    operation = Mock(
        side_effect=[LLMError("rate_limited", "制限1"), LLMError("rate_limited", "制限2"), "回答"]
    )
    sleep = Mock()

    result = call_with_rate_limit_retry(
        operation, max_attempts=3, initial_delay_sec=0.25, backoff=backoff, sleep=sleep
    )

    assert result == "回答"
    assert operation.call_count == 3
    assert sleep.call_args_list == [call(delay) for delay in delays]


@pytest.mark.parametrize("max_attempts", [1, 3])
def test_retry_exhaustion_raises_final_rate_limit_without_extra_call_or_wait(max_attempts):
    from lib.llm.retry import call_with_rate_limit_retry

    operation = Mock(
        side_effect=[LLMError("rate_limited", f"制限{i}") for i in range(max_attempts)]
    )
    sleep = Mock()

    with pytest.raises(LLMError) as caught:
        call_with_rate_limit_retry(
            operation, max_attempts=max_attempts, initial_delay_sec=0.25, sleep=sleep
        )

    assert caught.value.category == "rate_limited"
    assert str(caught.value) == f"制限{max_attempts - 1}"
    assert operation.call_count == max_attempts
    assert sleep.call_args_list == [call(0.25 * 2**i) for i in range(max_attempts - 1)]


@pytest.mark.parametrize("category", ["quota_exhausted", "auth_error", "timeout", "api_error"])
def test_retry_does_not_retry_other_llm_errors(category):
    from lib.llm.retry import call_with_rate_limit_retry

    operation = Mock(side_effect=LLMError(category, "中止"))
    sleep = Mock()

    with pytest.raises(LLMError) as caught:
        call_with_rate_limit_retry(operation, max_attempts=3, initial_delay_sec=1, sleep=sleep)

    assert caught.value.category == category
    operation.assert_called_once_with()
    sleep.assert_not_called()


def test_retry_propagates_unclassified_exception_without_waiting():
    from lib.llm.retry import call_with_rate_limit_retry

    operation = Mock(side_effect=ValueError("呼び出し側のエラー"))
    sleep = Mock()

    with pytest.raises(ValueError, match="呼び出し側のエラー"):
        call_with_rate_limit_retry(operation, max_attempts=3, initial_delay_sec=1, sleep=sleep)

    operation.assert_called_once_with()
    sleep.assert_not_called()


def test_retry_stops_when_rate_limit_is_followed_by_auth_error():
    from lib.llm.retry import call_with_rate_limit_retry

    operation = Mock(
        side_effect=[LLMError("rate_limited", "制限"), LLMError("auth_error", "認証失敗"), "回答"]
    )
    sleep = Mock()

    with pytest.raises(LLMError) as caught:
        call_with_rate_limit_retry(operation, max_attempts=3, initial_delay_sec=0.25, sleep=sleep)

    assert caught.value.category == "auth_error"
    assert operation.call_count == 2
    assert sleep.call_args_list == [call(0.25)]


@pytest.mark.parametrize(
    ("attempts", "delay"),
    [(0, 1), (-1, 1), (True, 1), (1.5, 1), (2, -0.1), (2, float("inf")), (2, float("nan"))],
)
def test_invalid_retry_arguments_are_rejected_before_calling_operation(attempts, delay):
    from lib.llm.retry import call_with_rate_limit_retry

    operation = Mock()
    sleep = Mock()

    with pytest.raises(ValueError):
        call_with_rate_limit_retry(
            operation, max_attempts=attempts, initial_delay_sec=delay, sleep=sleep
        )

    operation.assert_not_called()
    sleep.assert_not_called()


@pytest.mark.parametrize("backoff", [1, 0.5, float("inf"), float("nan")])
def test_invalid_backoff_is_rejected_before_calling_operation(backoff):
    from lib.llm.retry import call_with_rate_limit_retry

    operation = Mock()
    with pytest.raises(ValueError):
        call_with_rate_limit_retry(operation, max_attempts=2, initial_delay_sec=1, backoff=backoff)
    operation.assert_not_called()

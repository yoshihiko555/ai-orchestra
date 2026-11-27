"""一時的なレート制限だけを、回数を限定して再試行する。"""

from __future__ import annotations

import math
import time
from collections.abc import Callable

from .types import LLMError


def call_with_rate_limit_retry[T](
    operation: Callable[[], T],
    *,
    max_attempts: int,
    initial_delay_sec: float,
    backoff: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
        raise ValueError("max_attempts は1以上の整数が必要です。")
    if not math.isfinite(initial_delay_sec) or initial_delay_sec < 0:
        raise ValueError("initial_delay_sec は0以上の有限数が必要です。")
    if not math.isfinite(backoff) or backoff <= 1:
        raise ValueError("backoff は1より大きい有限数が必要です。")
    delay = initial_delay_sec
    attempts_left = max_attempts
    while True:
        try:
            return operation()
        except LLMError as exc:
            attempts_left -= 1
            if exc.category != "rate_limited" or attempts_left == 0:
                raise
            sleep(delay)
            delay *= backoff

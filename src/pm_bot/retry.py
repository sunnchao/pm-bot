from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 3
    initial_delay_seconds: float = 0.1
    backoff_multiplier: float = 2.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.initial_delay_seconds < 0.0:
            raise ValueError("initial_delay_seconds must be >= 0")
        if self.backoff_multiplier < 1.0:
            raise ValueError("backoff_multiplier must be >= 1")


READ_ONLY_API_RETRY_POLICY = RetryPolicy()


def retry_with_backoff(
    operation: Callable[[], T],
    *,
    should_retry: Callable[[Exception], bool],
    policy: RetryPolicy = READ_ONLY_API_RETRY_POLICY,
    sleep: Callable[[float], None] | None = None,
) -> T:
    sleeper = time.sleep if sleep is None else sleep
    delay_seconds = policy.initial_delay_seconds
    attempt = 1
    while True:
        try:
            return operation()
        except Exception as exc:
            if attempt >= policy.max_attempts or not should_retry(exc):
                raise
            sleeper(delay_seconds)
            delay_seconds *= policy.backoff_multiplier
            attempt += 1


def is_retryable_status_code(status_code: int | None) -> bool:
    return status_code == 429 or (status_code is not None and 500 <= status_code < 600)

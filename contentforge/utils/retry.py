"""Small retry helper with exponential backoff (no third-party dependency)."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

from contentforge.log import get_logger

T = TypeVar("T")
log = get_logger("retry")


class RetryError(RuntimeError):
    """Raised when all attempts have failed. ``last_exception`` holds the final error."""

    def __init__(self, attempts: int, last_exception: BaseException):
        super().__init__(f"Failed after {attempts} attempt(s): {last_exception}")
        self.attempts = attempts
        self.last_exception = last_exception


def retry(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    backoff: float = 2.0,
    multiplier: float = 2.0,
    max_backoff: float = 60.0,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
    label: str = "operation",
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call ``fn`` up to ``attempts`` times with exponential backoff.

    ``attempts`` counts the first try, so ``attempts=1`` means no retry.
    """
    attempts = max(1, attempts)
    delay = backoff
    last: BaseException | None = None
    for i in range(1, attempts + 1):
        try:
            return fn()
        except exceptions as exc:  # noqa: PERF203
            last = exc
            if i == attempts:
                break
            log.warning(
                "%s failed (attempt %d/%d): %s - retrying in %.1fs", label, i, attempts, exc, delay
            )
            sleep(delay)
            delay = min(delay * multiplier, max_backoff)
    assert last is not None
    raise RetryError(attempts, last)

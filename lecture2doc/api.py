"""ScaDS.AI client construction, retry with sensible waits, and a client-side request limiter."""
from __future__ import annotations

import threading
import time
from collections import deque

from openai import APIConnectionError, InternalServerError, OpenAI, RateLimitError

RETRYABLE = (APIConnectionError, InternalServerError, RateLimitError)
RATE_LIMIT_FIRST_WAIT = 15.0   # the gateway's quota window is a minute: waiting 2-16 s does not help
RATE_LIMIT_MAX_WAIT = 60.0
RETRY_AFTER_RANGE = (1.0, 120.0)


def make_client(cfg) -> OpenAI:
    return OpenAI(api_key=cfg.api_key, base_url=cfg.base_url, timeout=300, max_retries=0)


def retry_delay(exc, attempt: int, base_delay: float):
    """Seconds to wait before retry number `attempt` (0-based) after `exc`."""
    if isinstance(exc, RateLimitError):
        header = getattr(getattr(exc, "response", None), "headers", {}).get("retry-after")
        try:
            low, high = RETRY_AFTER_RANGE
            return min(max(float(header), low), high)
        except (TypeError, ValueError):
            return min(RATE_LIMIT_MAX_WAIT, RATE_LIMIT_FIRST_WAIT * 2**attempt)
    return base_delay * 2**attempt


def retry_call(fn, attempts: int = 5, base_delay: float = 2.0, sleep=None, retryable=RETRYABLE):
    for i in range(attempts):
        try:
            return fn()
        except retryable as exc:
            if i == attempts - 1:
                raise
            (sleep or time.sleep)(retry_delay(exc, i, base_delay))


class RateLimiter:
    """Allow at most `max_calls` calls in any `per`-second window; wait() blocks until a call is allowed.

    Thread-safe: callers queue up behind the lock, so concurrent workers share one quota."""

    def __init__(self, max_calls: int, per: float = 60.0, clock=time.monotonic, sleep=time.sleep):
        if max_calls < 1 or per <= 0:
            raise ValueError("max_calls must be >= 1 and per > 0")
        self.max_calls, self.per = max_calls, per
        self._clock, self._sleep = clock, sleep
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            while True:
                now = self._clock()
                while self._calls and now - self._calls[0] >= self.per:
                    self._calls.popleft()
                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return
                self._sleep(self.per - (now - self._calls[0]))

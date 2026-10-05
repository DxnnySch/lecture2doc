import pytest
from lecture2doc.api import retry_call


def test_retries_then_succeeds():
    calls = []

    def fn():
        calls.append(1)
        if len(calls) < 3:
            raise ValueError("boom")
        return "ok"

    delays = []
    assert retry_call(fn, sleep=delays.append, retryable=(ValueError,)) == "ok"
    assert len(calls) == 3
    assert delays == [2.0, 4.0]


def test_gives_up_after_attempts():
    with pytest.raises(ValueError):
        retry_call(lambda: (_ for _ in ()).throw(ValueError("x")), attempts=2, sleep=lambda s: None, retryable=(ValueError,))


def test_non_retryable_propagates_immediately():
    calls = []

    def fn():
        calls.append(1)
        raise KeyError("k")

    with pytest.raises(KeyError):
        retry_call(fn, sleep=lambda s: None, retryable=(ValueError,))
    assert len(calls) == 1


def test_make_client_disables_sdk_retries_and_sets_timeout():
    from lecture2doc.api import make_client
    from lecture2doc.config import Config

    client = make_client(Config("k", "http://x/v1", "stt", "llm", "llm"))
    assert client.max_retries == 0
    assert client.timeout == 300


import threading
import time

import httpx2
import openai

from lecture2doc.api import RateLimiter


def status_error(cls, status, headers=None):
    response = httpx2.Response(status, headers=headers or {}, request=httpx2.Request("POST", "http://x"))
    return cls("boom", response=response, body=None)


def failing_twice(error):
    calls = []

    def fn():
        calls.append(1)
        if len(calls) < 3:
            raise error
        return "ok"

    return fn


def test_rate_limit_waits_long_enough_for_the_quota_window():
    delays = []
    assert retry_call(failing_twice(status_error(openai.RateLimitError, 429)), sleep=delays.append) == "ok"
    assert delays == [15, 30]


def test_rate_limit_honours_retry_after_but_clamps_it():
    for header, expected in (("7", 7.0), ("9999", 120.0), ("0", 1.0)):
        delays = []
        error = status_error(openai.RateLimitError, 429, {"retry-after": header})
        retry_call(failing_twice(error), sleep=delays.append)
        assert delays == [expected, expected], header
    delays = []
    junk = status_error(openai.RateLimitError, 429, {"retry-after": "tomorrow"})
    retry_call(failing_twice(junk), sleep=delays.append)
    assert delays == [15, 30]


def test_other_transient_errors_keep_the_short_waits():
    delays = []
    retry_call(failing_twice(status_error(openai.InternalServerError, 500)), sleep=delays.append)
    assert delays == [2.0, 4.0]


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds


def test_limiter_lets_a_burst_through_then_waits_for_the_oldest_call_to_expire():
    clock = FakeClock()
    limiter = RateLimiter(3, per=10.0, clock=clock.now, sleep=clock.sleep)
    for _ in range(3):
        limiter.wait()
    assert clock.slept == []
    limiter.wait()  # the fourth call must wait for the window to free up
    assert clock.slept == [10.0] and clock.t == 10.0


def test_limiter_waits_only_as_long_as_needed():
    clock = FakeClock()
    limiter = RateLimiter(3, per=10.0, clock=clock.now, sleep=clock.sleep)
    for moment in (0.0, 4.0, 8.0):
        clock.t = moment
        limiter.wait()
    limiter.wait()  # oldest call was at 0.0 -> free again at 10.0
    assert clock.slept == [2.0]


def test_limiter_rejects_nonsense_limits():
    for bad in (0, -1):
        try:
            RateLimiter(bad)
        except ValueError:
            continue
        raise AssertionError("accepted limit %r" % bad)


def test_limiter_is_thread_safe_and_never_exceeds_the_limit():
    limiter = RateLimiter(2, per=0.3)
    stamps, lock = [], threading.Lock()

    def work():
        limiter.wait()
        with lock:
            stamps.append(time.monotonic())

    threads = [threading.Thread(target=work) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stamps.sort()
    assert len(stamps) == 6
    for i in range(len(stamps) - 2):  # no three calls within one window
        assert stamps[i + 2] - stamps[i] >= 0.2  # unthrottled, six threads would finish within milliseconds

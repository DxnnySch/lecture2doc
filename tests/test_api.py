import pytest
from api import retry_call


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

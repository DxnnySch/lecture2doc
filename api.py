"""ScaDS.AI client construction and retry helper."""
from __future__ import annotations

import time

from openai import APIConnectionError, InternalServerError, OpenAI, RateLimitError

RETRYABLE = (APIConnectionError, InternalServerError, RateLimitError)


def make_client(cfg) -> OpenAI:
    return OpenAI(api_key=cfg.api_key, base_url=cfg.base_url, timeout=600)


def retry_call(fn, attempts: int = 5, base_delay: float = 2.0, sleep=time.sleep, retryable=RETRYABLE):
    for i in range(attempts):
        try:
            return fn()
        except retryable:
            if i == attempts - 1:
                raise
            sleep(base_delay * 2**i)

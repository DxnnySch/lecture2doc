"""Pick the fastest of the most powerful chat models for every single request.

The API says nothing about how strong a model is, so the strong ones are a short list (`POWERFUL_MODELS`, or
`SCADS_LLM_MODELS` in `.env`) that is checked against what the API offers right now. Their speed is measured with a
small real request, and then kept up to date from the answers of the real requests: every request goes to the model
that is expected to answer first, taking into account how many requests it is already working on."""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from types import SimpleNamespace

from lecture2doc.api import RETRYABLE, RateLimiter
from lecture2doc.cleanup import CLEAN_SYSTEM, strip_think
from lecture2doc.config import AUTO

log = logging.getLogger(__name__)

PROBE_USER = ("Deck outline:\n1. Machine learning basics\n\nGlossary:\ngradient descent\nlearning rate\n\n"
              "Current slide (1) text:\nGradient descent minimizes the loss.\n\n"
              "Transcript to correct:\nSo the grading descent method updates the weights with the learning rate.")
PROBE_EXPECTS = "gradient descent"  # a model that cannot even fix this is not used
COOLDOWN = 60.0   # seconds a model that just failed is left alone
SMOOTHING = 0.3   # weight of the newest measurement


@dataclass
class Stat:
    speed: float | None = None   # smoothed seconds per unit of work; None = not measured
    in_flight: int = 0
    cooldown_until: float = 0.0
    used: int = 0
    failed: int = 0
    excluded: bool = False       # answered the speed test wrongly: never used


def work_units(messages, completion_tokens: int | None) -> float:
    """How big a request is: long inputs and long answers take longer, so times are compared per unit of work."""
    chars = sum(len(m.get("content") or "") for m in messages)
    return 1.0 + chars / 4000 + (completion_tokens or 0) / 200


def available_chat_models(client) -> set[str] | None:
    """The chat models the API offers right now; None if the list cannot be read."""
    try:
        return {m.id for m in client.models.list().data if getattr(m, "mode", "chat") == "chat"}
    except Exception as exc:
        log.warning("cannot list the models (%s); trying the configured ones", exc)
        return None


class FastestChat:
    """Looks like the client's `chat.completions.create`, but a request with `model=AUTO` goes to the fastest model.

    Requests for any other model name pass straight through to the real client."""

    def __init__(self, client, models, requests_per_minute: int = 25, clock=time.monotonic):
        self.client, self.clock = client, clock
        self.models = list(models)
        if not self.models:
            raise ValueError("no model to choose from")
        self.stats = {m: Stat() for m in self.models}
        self.limiters = {m: RateLimiter(requests_per_minute) for m in self.models}  # the gateway limits per model
        self.lock = threading.Lock()
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    # -- measuring ------------------------------------------------------------------------------------------------
    def benchmark(self) -> list[tuple[str, float | None]]:
        """Time every candidate on the same small request (in parallel). Returns (model, seconds) fastest first;
        seconds is None for a model that failed or answered wrongly, and such a model is not used."""
        messages = [{"role": "system", "content": CLEAN_SYSTEM}, {"role": "user", "content": PROBE_USER}]

        def probe(model: str):
            self.limiters[model].wait()
            start = self.clock()
            try:
                resp = self.client.chat.completions.create(model=model, messages=messages, temperature=0,
                                                           max_tokens=400)
            except Exception as exc:
                log.warning("model %s failed the speed test (%s)", model, exc)
                return model, None, "failed"
            seconds = self.clock() - start
            answer = strip_think(resp.choices[0].message.content or "")
            if PROBE_EXPECTS not in answer.lower():
                log.warning("model %s gave a wrong answer in the speed test; not using it", model)
                return model, None, "wrong"
            usage = getattr(resp, "usage", None)
            return model, seconds, work_units(messages, getattr(usage, "completion_tokens", None))

        with ThreadPoolExecutor(max_workers=len(self.models)) as pool:
            results = list(pool.map(probe, self.models))
        with self.lock:
            for model, seconds, work in results:
                stat = self.stats[model]
                if seconds is None:
                    stat.speed, stat.cooldown_until = None, self.clock() + COOLDOWN
                    stat.excluded = work == "wrong"
                else:
                    stat.speed = seconds / work
        return sorted(((m, s) for m, s, _ in results), key=lambda r: (r[1] is None, r[1] or 0.0))

    # -- choosing -------------------------------------------------------------------------------------------------
    def _ranked(self) -> list[str]:
        """All candidates, the one expected to answer first at the front."""
        now = self.clock()
        known = [s.speed for s in self.stats.values() if s.speed]
        guess = max(known) if known else 1.0  # a model not measured yet is assumed slow until it proves otherwise

        def cost(model: str):
            s = self.stats[model]
            return (s.cooldown_until > now, (s.speed or guess) * (1 + s.in_flight))

        usable = [m for m in self.models if not self.stats[m].excluded] or self.models
        return sorted(usable, key=cost)

    def create(self, *, model: str = AUTO, messages, **kwargs):
        if model != AUTO:
            return self.client.chat.completions.create(model=model, messages=messages, **kwargs)
        last_error = None
        with self.lock:
            order = self._ranked()
        for name in order:
            stat = self.stats[name]
            self.limiters[name].wait()
            with self.lock:
                stat.in_flight += 1
            start = self.clock()
            try:
                resp = self.client.chat.completions.create(model=name, messages=messages, **kwargs)
            except RETRYABLE as exc:  # busy or unreachable: leave this model alone for a while, ask the next one
                with self.lock:
                    stat.in_flight -= 1
                    stat.failed += 1
                    stat.cooldown_until = self.clock() + COOLDOWN
                last_error = exc
                log.warning("model %s failed (%s); trying another", name, exc)
                continue
            except BaseException:
                with self.lock:
                    stat.in_flight -= 1
                raise
            seconds = self.clock() - start
            tokens = getattr(getattr(resp, "usage", None), "completion_tokens", None)
            measured = seconds / work_units(messages, tokens)
            with self.lock:
                stat.in_flight -= 1
                stat.used += 1
                stat.speed = measured if stat.speed is None else (1 - SMOOTHING) * stat.speed + SMOOTHING * measured
            return resp
        raise last_error

    def summary(self) -> str:
        """Which model answered how many requests, for the log."""
        used = sorted(((s.used, m) for m, s in self.stats.items() if s.used), reverse=True)
        return ", ".join(f"{m} {n}x" for n, m in used) or "no request sent"


def pick_models(cfg, client) -> list[str]:
    """The powerful models to choose from: the configured (or default) list, restricted to what the API offers."""
    wanted = list(cfg.llm_models)
    offered = available_chat_models(client)
    chosen = [m for m in wanted if offered is None or m in offered]
    if not chosen:
        raise ValueError("none of the models " + ", ".join(wanted) + " is offered by the API; set SCADS_LLM_MODEL "
                         "to a model of your choice")
    return chosen


def llm_client(cfg, client, say=lambda text: None):
    """The client to use for the AI text requests. With `SCADS_LLM_MODEL` unset (= automatic) it is a `FastestChat`
    whose speed ranking was just measured; otherwise the plain client, which uses the model named in the .env."""
    if AUTO not in (cfg.llm_model, cfg.glossary_model):
        return client
    chat = FastestChat(client, pick_models(cfg, client), cfg.requests_per_minute)
    ranking = chat.benchmark()
    if all(seconds is None for _, seconds in ranking):
        raise RuntimeError("none of the AI models answered the speed test: " + ", ".join(m for m, _ in ranking))
    say("AI models, fastest first: " + ", ".join(
        f"{m} ({s:.1f}s)" if s is not None else f"{m} (not usable)" for m, s in ranking))
    return chat

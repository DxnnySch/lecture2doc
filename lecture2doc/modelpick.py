"""Use one model, or the fastest of several, for every single request of a task.

Every task (building the glossary, correcting the text, speech to text) has a list of models the user finds suitable.
With one model, that model is used. With several, their speed is measured and then kept up to date from the answers
of the real requests: every request goes to the model that is expected to answer first, taking into account how many
requests it is already working on, so busy models share the load and a failing model is left alone for a while."""
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
PROBE_TOKENS = 1500                 # enough for a model that thinks for a while before it answers
REQUEST_TIMEOUT = 120.0  # seconds a model may take for one text request before the next one is asked
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


def chat_work(messages, completion_tokens: int | None) -> float:
    """How big a chat request is: long inputs and long answers take longer, so times are compared per unit of work."""
    chars = sum(len(m.get("content") or "") for m in messages)
    return 1.0 + chars / 4000 + (completion_tokens or 0) / 200


def audio_work(file) -> float:
    """How big a transcription request is: the length of the audio (a 48 kbit/s mp3 is 6000 bytes per second)."""
    try:
        position = file.tell()
        file.seek(0, 2)
        size = file.tell()
        file.seek(position)
        return 1.0 + size / 6000
    except (AttributeError, OSError, ValueError):
        return 1.0


def offered_models(client) -> dict[str, str] | None:
    """{model id: mode} of what the API offers right now (mode: chat, audio_transcription, ...); None if unknown."""
    try:
        return {m.id: getattr(m, "mode", "chat") for m in client.models.list().data}
    except Exception as exc:
        log.warning("cannot list the models (%s); trying the chosen ones", exc)
        return None


class Picker:
    """The shared part: ranking by expected answer time, per-model request quota, failover, statistics."""

    optimistic = False  # a model not measured yet: assumed slow (False) or worth a try (True)

    def __init__(self, client, models, requests_per_minute: int = 25, clock=time.monotonic):
        self.client, self.clock = client, clock
        self.models = list(dict.fromkeys(models))
        if not self.models:
            raise ValueError("no model to choose from")
        self.stats = {m: Stat() for m in self.models}
        self.limiters = {m: RateLimiter(requests_per_minute) for m in self.models}  # the gateway limits per model
        self.lock = threading.Lock()

    def _ranked(self) -> list[str]:
        """All usable candidates, the one expected to answer first at the front."""
        now = self.clock()
        known = [s.speed for s in self.stats.values() if s.speed]
        guess = 0.0 if self.optimistic else (max(known) if known else 1.0)

        def cost(model: str):
            s = self.stats[model]
            return (s.cooldown_until > now, (s.speed if s.speed else guess) * (1 + s.in_flight))

        usable = [m for m in self.models if not self.stats[m].excluded] or self.models
        return sorted(usable, key=cost)

    def _dispatch(self, call, work):
        """Send one request to the best model; `call(model)` makes it, `work(response)` sizes it."""
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
                resp = call(name)
            except RETRYABLE as exc:  # busy or unreachable: leave this model alone for a while, ask the next one
                with self.lock:
                    stat.in_flight -= 1
                    stat.failed += 1
                    stat.cooldown_until = self.clock() + COOLDOWN
                last_error = exc
                log.warning("model %s failed (%s); %s", name, exc, "trying another" if len(order) > 1 else "will retry")
                continue
            except BaseException:
                with self.lock:
                    stat.in_flight -= 1
                raise
            measured = (self.clock() - start) / work(resp)
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


class FastestChat(Picker):
    """Looks like the client's `chat.completions.create`; a request with `model=AUTO` goes to the fastest model.
    Requests for any other model name pass straight through to the real client."""

    def __init__(self, client, models, requests_per_minute: int = 25, clock=time.monotonic):
        super().__init__(client, models, requests_per_minute, clock)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def benchmark(self) -> list[tuple[str, float | None]]:
        """Time every candidate on the same small request (in parallel). Returns (model, seconds) fastest first;
        seconds is None for a model that failed or answered wrongly, and such a model is not used."""
        messages = [{"role": "system", "content": CLEAN_SYSTEM}, {"role": "user", "content": PROBE_USER}]

        def probe(model: str):
            self.limiters[model].wait()
            start = self.clock()
            try:
                resp = self.client.chat.completions.create(model=model, messages=messages, temperature=0,
                                                           max_tokens=PROBE_TOKENS, timeout=REQUEST_TIMEOUT / 2)
            except Exception as exc:
                log.warning("model %s failed the speed test (%s)", model, exc)
                return model, None, "failed"
            seconds = self.clock() - start
            answer = strip_think(resp.choices[0].message.content or "")
            if PROBE_EXPECTS not in answer.lower():
                log.warning("model %s gave a wrong answer in the speed test; not using it", model)
                return model, None, "wrong"
            usage = getattr(resp, "usage", None)
            return model, seconds, chat_work(messages, getattr(usage, "completion_tokens", None))

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

    def create(self, *, model: str = AUTO, messages, **kwargs):
        if model != AUTO:
            return self.client.chat.completions.create(model=model, messages=messages, **kwargs)
        kwargs.setdefault("timeout", REQUEST_TIMEOUT)  # the default (5 minutes) would hold up everything behind it

        def work(resp):
            return chat_work(messages, getattr(getattr(resp, "usage", None), "completion_tokens", None))

        return self._dispatch(lambda name: self.client.chat.completions.create(model=name, messages=messages, **kwargs),
                              work)


class FastestTranscribe(Picker):
    """Looks like the client's `audio.transcriptions.create`; a request with `model=AUTO` goes to the fastest model.
    There is no way to test the answer of a speech model without real speech, so the speed is learned from the real
    clips: a model not tried yet is given a chance first."""

    optimistic = True

    def __init__(self, client, models, requests_per_minute: int = 25, clock=time.monotonic):
        super().__init__(client, models, requests_per_minute, clock)
        self.audio = SimpleNamespace(transcriptions=SimpleNamespace(create=self.create))

    def create(self, *, model: str = AUTO, file, **kwargs):
        if model != AUTO:
            return self.client.audio.transcriptions.create(model=model, file=file, **kwargs)
        size = audio_work(file)

        def call(name):
            file.seek(0)
            return self.client.audio.transcriptions.create(model=name, file=file, **kwargs)

        return self._dispatch(call, lambda resp: size)


def _usable(client, models, mode: str, say) -> list[str]:
    """The chosen models that the API offers (for this kind of task); says which ones were dropped."""
    offered = offered_models(client)
    if offered is None:
        return list(models)
    kept = [m for m in models if offered.get(m) == mode]
    dropped = [m for m in models if m not in kept]
    if dropped:
        say("not offered by the API (for this task), left out: " + ", ".join(dropped))
    if not kept:
        raise ValueError("none of the chosen models is offered by the API for this task: " + ", ".join(models))
    return kept


def select_chat(client, models, requests_per_minute: int = 25, say=lambda text: None, label: str = "AI models"):
    """A client for the models chosen for one chat task. One model: used as it is. Several: their speed is measured
    first and the fastest one answers every request. Returns (client, model name to pass to its create())."""
    models = _usable(client, models, "chat", say)
    chat = FastestChat(client, models, requests_per_minute)
    if len(models) == 1:
        say(f"{label}: {models[0]}")
        return chat, AUTO
    ranking = chat.benchmark()
    if all(seconds is None for _, seconds in ranking):
        raise RuntimeError("none of the AI models answered the speed test: " + ", ".join(m for m, _ in ranking))
    say(f"{label}, fastest first: " + ", ".join(
        f"{m} ({s:.1f}s)" if s is not None else f"{m} (not usable)" for m, s in ranking))
    return chat, AUTO


def select_transcription(client, models, requests_per_minute: int = 25, say=lambda text: None):
    """The same for speech to text; the models are timed on the real clips as they are transcribed."""
    models = _usable(client, models, "audio_transcription", say)
    say("speech-to-text: " + (models[0] if len(models) == 1 else ", ".join(models) + " (the fastest answers each clip)"))
    return FastestTranscribe(client, models, requests_per_minute), AUTO


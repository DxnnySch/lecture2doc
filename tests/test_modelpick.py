import threading
import time

import httpx2
import openai
import pytest

from lecture2doc.config import AUTO, POWERFUL_MODELS, load_config
from lecture2doc.modelpick import FastestChat, llm_client, pick_models, work_units

GOOD = "So the gradient descent method updates the weights with the learning rate."


class Clock:
    """A fake clock the fake models advance, so no test really waits."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class FakeAPI:
    """A client whose models answer after a given time (seconds on the fake clock) or fail."""

    def __init__(self, clock, seconds, answers=None, errors=None, offered=None):
        self.clock, self.seconds, self.answers, self.errors = clock, seconds, answers or {}, errors or {}
        self.calls, self.lock = [], threading.Lock()
        self.offered = offered
        self.chat = type("C", (), {"completions": type("P", (), {"create": self.create})()})()
        self.models = type("M", (), {"list": self.list_models})()

    def list_models(self):
        ids = self.offered if self.offered is not None else list(self.seconds)
        return type("L", (), {"data": [type("M", (), {"id": i, "mode": "chat"}) for i in ids]})()

    def create(self, *, model, messages, **kwargs):
        with self.lock:
            self.calls.append(model)
            self.clock.now += self.seconds[model]
        if model in self.errors:
            raise self.errors[model]
        text = self.answers.get(model, GOOD)
        message = type("Msg", (), {"content": text})()
        return type("R", (), {"choices": [type("Ch", (), {"message": message})()],
                              "usage": type("U", (), {"completion_tokens": 40})()})()


def rate_limit():
    return openai.RateLimitError("slow", response=httpx2.Response(429, request=httpx2.Request("POST", "http://x")),
                                 body=None)


def chat(api, clock, models=None):
    return FastestChat(api, models or list(api.seconds), clock=clock)


def test_benchmark_ranks_the_models_by_speed():
    clock = Clock()
    api = FakeAPI(clock, {"slow": 5.0, "fast": 1.0, "mid": 2.0})
    # the parallel probes share the fake clock, so look at the order only
    ranking = chat(api, clock).benchmark()
    assert [m for m, s in ranking if s is not None]  # all answered
    assert set(m for m, _ in ranking) == {"slow", "fast", "mid"}


def test_every_request_goes_to_the_fastest_model():
    clock = Clock()
    api = FakeAPI(clock, {"slow": 5.0, "fast": 1.0})
    c = chat(api, clock)
    c.stats["slow"].speed, c.stats["fast"].speed = 5.0, 1.0
    for _ in range(3):
        c.create(model=AUTO, messages=[{"role": "user", "content": "x"}])
    assert api.calls == ["fast", "fast", "fast"] and c.summary() == "fast 3x"


def test_a_model_that_gets_slower_is_dropped_by_the_running_measurements():
    clock = Clock()
    api = FakeAPI(clock, {"a": 1.0, "b": 2.0})
    c = chat(api, clock)
    c.stats["a"].speed, c.stats["b"].speed = 0.2, 0.5
    c.create(model=AUTO, messages=[{"role": "user", "content": "x"}])
    api.seconds["a"] = 60.0  # a gets busy
    for _ in range(4):
        c.create(model=AUTO, messages=[{"role": "user", "content": "x"}])
    assert api.calls[-1] == "b" and "a" in api.calls


def test_busy_models_share_the_load():
    clock = Clock()
    api = FakeAPI(clock, {"a": 1.0, "b": 1.5})
    c = chat(api, clock)
    c.stats["a"].speed, c.stats["b"].speed = 1.0, 1.5
    c.stats["a"].in_flight = 3  # a is working on three requests: b is expected to answer first
    assert c._ranked()[0] == "b"


def test_a_failing_model_is_skipped_for_a_while_and_the_next_one_answers():
    clock = Clock()
    api = FakeAPI(clock, {"a": 1.0, "b": 2.0}, errors={"a": rate_limit()})
    c = chat(api, clock)
    c.stats["a"].speed, c.stats["b"].speed = 0.1, 0.5
    resp = c.create(model=AUTO, messages=[{"role": "user", "content": "x"}])
    assert resp.choices[0].message.content == GOOD and api.calls == ["a", "b"]
    c.create(model=AUTO, messages=[{"role": "user", "content": "x"}])
    assert api.calls == ["a", "b", "b"]  # a is left alone during the cool-down
    clock.now += 61
    assert c._ranked()[0] == "a"  # and tried again afterwards


def test_when_every_model_fails_the_error_is_raised_for_the_retry_logic():
    clock = Clock()
    api = FakeAPI(clock, {"a": 1.0, "b": 1.0}, errors={"a": rate_limit(), "b": rate_limit()})
    with pytest.raises(openai.RateLimitError):
        chat(api, clock).create(model=AUTO, messages=[{"role": "user", "content": "x"}])


def test_a_model_with_a_wrong_answer_in_the_speed_test_is_never_used():
    clock = Clock()
    api = FakeAPI(clock, {"good": 2.0, "garbage": 0.1}, answers={"garbage": "I cannot help with that."})
    c = chat(api, clock)
    ranking = dict(c.benchmark())
    assert ranking["garbage"] is None and ranking["good"] is not None
    clock.now += 1000  # even after the cool-down
    assert c._ranked() == ["good"]


def test_a_model_that_fails_the_speed_test_is_tried_again_later():
    clock = Clock()
    api = FakeAPI(clock, {"a": 1.0, "b": 2.0}, errors={"a": rate_limit()})
    c = chat(api, clock)
    c.benchmark()
    assert c._ranked()[0] == "b"
    clock.now += 61
    assert "a" in c._ranked()  # not excluded for good, only cooled down


def test_a_named_model_goes_straight_through():
    clock = Clock()
    api = FakeAPI(clock, {"a": 1.0, "fixed": 3.0})
    chat(api, clock, ["a"]).create(model="fixed", messages=[{"role": "user", "content": "x"}])
    assert api.calls == ["fixed"]


def test_work_units_grow_with_the_request():
    small = work_units([{"content": "x"}], 10)
    assert work_units([{"content": "x" * 40000}], 10) > small < work_units([{"content": "x"}], 2000)


def test_only_offered_models_of_the_list_are_candidates(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("SCADS_API_KEY=k\n")
    cfg = load_config(env)
    assert cfg.llm_model == AUTO and cfg.llm_models == POWERFUL_MODELS
    clock = Clock()
    api = FakeAPI(clock, {m: 1.0 for m in POWERFUL_MODELS}, offered=[POWERFUL_MODELS[1], "other"])
    assert pick_models(cfg, api) == [POWERFUL_MODELS[1]]
    with pytest.raises(ValueError, match="none of the models"):
        pick_models(cfg, FakeAPI(clock, {"x": 1.0}, offered=["x"]))


def test_a_custom_candidate_list_and_a_fixed_model_from_the_env(tmp_path):
    env = tmp_path / ".env"
    env.write_text("SCADS_API_KEY=k\nSCADS_LLM_MODELS= m1 , m2 ,\n")
    assert load_config(env).llm_models == ("m1", "m2")
    env.write_text("SCADS_API_KEY=k\nSCADS_LLM_MODEL=my-llm\n")
    cfg = load_config(env)
    api = FakeAPI(Clock(), {"x": 1.0})
    assert llm_client(cfg, api) is api  # a fixed model: the plain client, no speed test
    assert api.calls == []


def test_llm_client_measures_and_reports_the_ranking(tmp_path):
    env = tmp_path / ".env"
    env.write_text("SCADS_API_KEY=k\nSCADS_LLM_MODELS=a,b\n")
    clock = Clock()
    api = FakeAPI(clock, {"a": 1.0, "b": 2.0})
    said = []
    result = llm_client(load_config(env), api, said.append)
    assert isinstance(result, FastestChat) and said[0].startswith("AI models, fastest first: ")
    assert sorted(api.calls) == ["a", "b"]


def test_without_any_working_model_the_run_stops_with_a_clear_message(tmp_path):
    env = tmp_path / ".env"
    env.write_text("SCADS_API_KEY=k\nSCADS_LLM_MODELS=a\n")
    api = FakeAPI(Clock(), {"a": 1.0}, errors={"a": rate_limit()})
    with pytest.raises(RuntimeError, match="none of the AI models answered"):
        llm_client(load_config(env), api)

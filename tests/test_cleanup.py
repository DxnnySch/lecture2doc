import logging
from types import SimpleNamespace as NS

from PIL import Image

from cleanup import LLMCache, build_glossary, build_outline, cleanup_spans, guard, strip_think
from segment import Span
from slides import Slide


def slides(n=3, text="Gradient descent. Backpropagation (BP)."):
    return [Slide(i + 1, f"Title {i + 1}", f"{text} {i}", Image.new("RGB", (4, 4))) for i in range(n)]


class FakeClient:
    def __init__(self, reply):
        self.calls = []
        self.reply = reply
        self.chat = NS(completions=NS(create=self.create))

    def create(self, model, messages, temperature):
        assert temperature == 0
        self.calls.append((model, messages))
        r = self.reply(messages) if callable(self.reply) else self.reply
        if isinstance(r, Exception):
            raise r
        return NS(choices=[NS(message=NS(content=r))])


def test_strip_think():
    assert strip_think("<think>hmm</think>\nHello") == "Hello"
    assert strip_think("plain") == "plain"


def test_guard_accepts_small_changes_only():
    raw = "grading descent is used here " * 4
    assert guard(raw, raw.replace("grading", "gradient")) is not None
    assert guard(raw, raw[: len(raw) // 2]) is None
    assert guard(raw, raw * 2) is None
    assert guard(raw, "") is None


def test_outline_lists_numbers_and_titles():
    assert build_outline(slides(2)) == "1. Title 1\n2. Title 2"


def test_glossary_dedupes_and_chunks():
    client = FakeClient(lambda m: "Gradient descent\n- Backpropagation (BP)\ngradient descent")
    g = build_glossary(client, "m", slides(4), LLMCache(None), max_chars=60)
    assert len(client.calls) > 1  # deck split into several calls
    assert g.splitlines() == ["Gradient descent", "Backpropagation (BP)"]


def test_cleanup_uses_corrected_text_and_slide_context():
    client = FakeClient("The gradient descent works.")
    out = cleanup_spans(client, "m", "Gradient descent", "1. Title 1", slides(), [Span(2, 0, 5, 1)],
                        ["The grading descent works."], LLMCache(None))
    assert out == ["The gradient descent works."]
    prompt = client.calls[0][1][-1]["content"]
    assert "Gradient descent" in prompt and "The grading descent works." in prompt and "Title 1" in prompt


def test_cleanup_falls_back_to_raw_on_big_change(caplog):
    client = FakeClient("short")
    raw = "this is a fairly long transcript sentence about decision support systems"
    with caplog.at_level(logging.WARNING):
        out = cleanup_spans(client, "m", "", "", slides(), [Span(1, 0, 5, 1)], [raw], LLMCache(None))
    assert out == [raw] and "raw" in caplog.text.lower()


def test_cleanup_falls_back_on_error_and_think_only_reply():
    raw = "some words here that are the transcript"
    for reply in (RuntimeError("api down"), "<think>only thoughts</think>"):
        out = cleanup_spans(FakeClient(reply), "m", "", "", slides(), [Span(1, 0, 5, 1)], [raw], LLMCache(None))
        assert out == [raw]


def test_empty_raw_makes_no_call():
    client = FakeClient("x")
    assert cleanup_spans(client, "m", "", "", slides(), [Span(1, 0, 5, 1)], [""], LLMCache(None)) == [""]
    assert client.calls == []


def test_cache_prevents_repeat_calls(tmp_path):
    raw = "the grading descent works well enough"
    cache = LLMCache(tmp_path / "c.json")
    client = FakeClient("the gradient descent works well enough")
    args = ("m", "g", "o", slides(), [Span(1, 0, 5, 1)], [raw])
    cleanup_spans(client, *args, cache)
    cleanup_spans(client, *args, LLMCache(tmp_path / "c.json"))
    assert len(client.calls) == 1

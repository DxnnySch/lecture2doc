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
    # Issue 1: unclosed think blocks
    assert strip_think("<think>The speaker says") == ""
    assert strip_think("Hello <think>unfinished") == "Hello"


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


def test_chat_does_not_cache_empty_results(tmp_path):
    # Issue 2: _chat must NOT cache empty results
    cache = LLMCache(tmp_path / "c.json")
    calls = [0]

    def reply_fn(messages):
        calls[0] += 1
        return "<think>only</think>" if calls[0] <= 1 else "the corrected text here"

    client = FakeClient(reply_fn)
    # First call: returns "<think>only</think>" which becomes ""
    raw = "the original transcript text"
    out1 = cleanup_spans(client, "m", "", "", slides(), [Span(1, 0, 5, 1)], [raw], cache)
    # Since empty, should use raw
    assert out1 == [raw]

    # Second call with fresh cache file: should retry (not cache the empty result)
    client2 = FakeClient(reply_fn)
    out2 = cleanup_spans(client2, "m", "", "", slides(), [Span(1, 0, 5, 1)], [raw], LLMCache(tmp_path / "c.json"))
    # Should have called LLM again (not cached), and gotten "the corrected text here"
    assert len(client2.calls) == 1  # One new call made
    assert out2 == ["the corrected text here"]


def test_build_glossary_skips_failed_chunks(caplog):
    # Issue 3: build_glossary handles exceptions, logs warning, skips chunk, continues
    call_count = [0]

    def reply_fn(messages):
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("chunk 1 failed")
        return "Term from chunk 2\nAnother term"

    client = FakeClient(reply_fn)
    with caplog.at_level(logging.WARNING):
        g = build_glossary(client, "m", slides(2), LLMCache(None), max_chars=60)

    # Should have called twice (2 chunks), but first failed
    assert len(client.calls) == 2
    # Should have logged warning
    assert "warning" in caplog.text.lower()
    # Should have terms from second chunk
    assert "Term from chunk 2" in g


def test_llm_cache_corrupt_file_treated_as_empty(tmp_path):
    # Issue 4: corrupt cache file is treated as empty cache (no exception)
    cache_path = tmp_path / "corrupt.json"
    cache_path.write_text("not valid json {[}", encoding="utf-8")

    # Should not raise, should treat as empty cache
    cache = LLMCache(cache_path)
    assert cache.get("any_key") is None

    # Should be able to set and write
    cache.set("key", "value")
    assert cache.get("key") == "value"


def test_cleanup_spans_raises_on_length_mismatch():
    # Issue 5a: cleanup_spans raises ValueError if len(spans) != len(raws)
    client = FakeClient("x")
    try:
        cleanup_spans(client, "m", "", "", slides(), [Span(1, 0, 5, 1)], ["raw1", "raw2"], LLMCache(None))
        assert False, "should have raised ValueError"
    except ValueError:
        pass


def test_cleanup_spans_preserves_order_with_workers():
    # Issue 5b: order is preserved across several spans with workers>1
    # Create a client that echoes back slide-specific info
    def reply_fn(messages):
        # Extract slide number from prompt
        for msg in messages:
            if "Current slide (" in msg.get("content", ""):
                # Find the slide number
                import re
                match = re.search(r"Current slide \((\d+)\)", msg["content"])
                if match:
                    slide_num = match.group(1)
                    return f"This is from slide {slide_num}"
        return "unknown text here"

    client = FakeClient(reply_fn)
    spans = [Span(1, 0, 5, 1), Span(2, 5, 10, 1), Span(3, 10, 15, 1), Span(2, 15, 20, 1)]
    raws = ["This is raw text one", "This is raw text two", "This is raw text three", "This is raw text four"]

    out = cleanup_spans(client, "m", "", "", slides(), spans, raws, LLMCache(None), workers=2)

    # Check order is preserved: each output should contain the correct slide number
    assert "slide 1" in out[0]
    assert "slide 2" in out[1]
    assert "slide 3" in out[2]
    assert "slide 2" in out[3]

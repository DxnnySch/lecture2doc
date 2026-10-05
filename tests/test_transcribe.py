from types import SimpleNamespace as NS

import pytest

from config import Config
from helpers import make_pdf, make_video
from segment import Span
from slides import render_pdf
from transcribe import Word, assign_words, estimate_words, parse_response, split_audio, transcribe_words


def test_parse_response_word_level_with_offset():
    res = NS(words=[NS(word=" Hello", start=0.5, end=0.9), {"word": "world", "start": 1.0, "end": 1.4}])
    assert parse_response(res, 300.0, 300) == [Word("Hello", 300.5, 300.9), Word("world", 301.0, 301.4)]


def test_parse_response_falls_back_to_segments():
    res = NS(words=None, segments=[NS(start=0.0, end=4.0, text="a b c d")])
    words = parse_response(res, 10.0, 300)
    assert [w.w for w in words] == ["a", "b", "c", "d"]
    assert words[0].start == 10.0 and words[-1].end == pytest.approx(14.0)


def test_parse_response_falls_back_to_whole_chunk_text():
    res = NS(words=None, segments=None, text="one two")
    words = parse_response(res, 0.0, 60)
    assert [w.w for w in words] == ["one", "two"]
    assert words[-1].end == pytest.approx(60.0)


def test_parse_response_empty_is_empty():
    assert parse_response(NS(words=None, segments=None, text=""), 0.0, 60) == []


def test_estimate_words_spreads_evenly():
    words = estimate_words([(0.0, 2.0, "x y")])
    assert words == [Word("x", 0.0, 1.0), Word("y", 1.0, 2.0)]


def test_assign_words_by_midpoint_and_edges():
    spans = [Span(1, 0, 5, 1), Span(2, 5, 10, 1), Span(1, 10, 15, 1)]
    words = [Word("early", -1, -0.5), Word("a", 1, 2), Word("b", 4.8, 5.4), Word("c", 6, 7),
             Word("d", 11, 12), Word("late", 99, 100)]
    assert assign_words(words, spans) == ["early a", "b c", "d late"]


def test_slide_without_words_gets_empty_text():
    assert assign_words([Word("a", 1, 2)], [Span(1, 0, 5, 1), Span(2, 5, 10, 1)]) == ["a", ""]


def test_split_audio_chunks_and_offsets(tmp_path):
    images, _ = render_pdf(make_pdf(tmp_path / "s.pdf", 1))
    video = make_video(images, [0] * 12, tmp_path / "v.mp4")
    chunks = split_audio(video, tmp_path / "chunks", chunk_seconds=5)
    assert [off for _, off in chunks] == [0, 5, 10]
    assert all(p.exists() for p, _ in chunks)


class FakeClient:
    def __init__(self):
        self.calls = 0
        self.audio = NS(transcriptions=NS(create=self.create))

    def create(self, **kwargs):
        self.calls += 1
        assert kwargs["model"] == "alias-stt" and kwargs["response_format"] == "verbose_json"
        return NS(words=[NS(word="hi", start=0.1, end=0.3)])


def test_transcribe_words_offsets_and_cache(tmp_path):
    images, _ = render_pdf(make_pdf(tmp_path / "s.pdf", 1))
    video = make_video(images, [0] * 12, tmp_path / "v.mp4")
    cfg = Config("k", "http://x", "alias-stt", "llm", "llm")
    client = FakeClient()
    cache = tmp_path / "words.json"
    words = transcribe_words(cfg, video, cache, chunk_seconds=5, client=client)
    assert client.calls == 3
    assert [round(w.start, 1) for w in words] == [0.1, 5.1, 10.1]
    again = transcribe_words(cfg, video, cache, chunk_seconds=5, client=client)
    assert client.calls == 3 and again == words  # served from cache

from types import SimpleNamespace as NS
import json
from pathlib import Path

import pytest
import httpx2
from openai import BadRequestError

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
    def __init__(self, raise_error_on_first=False):
        self.calls = 0
        self.call_kwargs = []
        self.raise_error_on_first = raise_error_on_first
        self.audio = NS(transcriptions=NS(create=self.create))

    def create(self, **kwargs):
        self.calls += 1
        self.call_kwargs.append(kwargs.copy())
        assert kwargs["model"] == "alias-stt" and kwargs["response_format"] == "verbose_json"
        if self.raise_error_on_first and self.calls == 1 and "timestamp_granularities" in kwargs:
            raise BadRequestError("word timestamps not supported", response=httpx2.Response(400, request=httpx2.Request("POST", "http://x")), body=None)
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

    # Verify cache format
    cache_data = json.loads(cache.read_text(encoding="utf-8"))
    assert "meta" in cache_data
    assert "words" in cache_data
    assert cache_data["meta"]["video"] == "v.mp4"
    assert cache_data["meta"]["chunk_seconds"] == 5
    assert cache_data["meta"]["language"] is None

    again = transcribe_words(cfg, video, cache, chunk_seconds=5, client=client)
    assert client.calls == 3 and again == words  # served from cache


def test_transcribe_words_cache_invalidated_by_language(tmp_path):
    images, _ = render_pdf(make_pdf(tmp_path / "s.pdf", 1))
    video = make_video(images, [0] * 12, tmp_path / "v.mp4")
    cfg = Config("k", "http://x", "alias-stt", "llm", "llm")
    client = FakeClient()
    cache = tmp_path / "words.json"
    words1 = transcribe_words(cfg, video, cache, language=None, chunk_seconds=5, client=client)
    assert client.calls == 3
    # Now call with different language - cache should be invalidated
    words2 = transcribe_words(cfg, video, cache, language="de", chunk_seconds=5, client=client)
    assert client.calls == 6  # Should have called 3 more times


def test_transcribe_words_cache_corrupt_json_is_miss(tmp_path):
    images, _ = render_pdf(make_pdf(tmp_path / "s.pdf", 1))
    video = make_video(images, [0] * 12, tmp_path / "v.mp4")
    cfg = Config("k", "http://x", "alias-stt", "llm", "llm")
    client = FakeClient()
    cache = tmp_path / "words.json"
    # Create corrupt cache
    cache.write_text("not valid json", encoding="utf-8")
    words = transcribe_words(cfg, video, cache, chunk_seconds=5, client=client)
    assert client.calls == 3
    assert len(words) > 0


def test_transcribe_words_cache_mismatched_meta_is_miss(tmp_path):
    images, _ = render_pdf(make_pdf(tmp_path / "s.pdf", 1))
    video = make_video(images, [0] * 12, tmp_path / "v.mp4")
    cfg = Config("k", "http://x", "alias-stt", "llm", "llm")
    client = FakeClient()
    cache = tmp_path / "words.json"
    # Create cache with mismatched chunk_seconds
    cache_data = {"meta": {"video": "v.mp4", "size": 1000, "language": None, "chunk_seconds": 10}, "words": []}
    cache.write_text(json.dumps(cache_data), encoding="utf-8")
    words = transcribe_words(cfg, video, cache, chunk_seconds=5, client=client)
    assert client.calls == 3  # Should re-transcribe due to mismatch


def test_split_audio_bad_input_raises(tmp_path):
    # Pass a non-media file
    bad_file = tmp_path / "not_a_video.txt"
    bad_file.write_text("not a video")
    with pytest.raises(RuntimeError, match="audio extraction failed") as exc_info:
        split_audio(bad_file, tmp_path / "chunks", chunk_seconds=5)
    # Assert stderr snippet is in the error message (should contain ffmpeg error)
    error_msg = str(exc_info.value)
    assert "Invalid data found when processing input" in error_msg
    # Verify it's extracting stderr, not just the generic message
    assert len(error_msg) > len("audio extraction failed: ")  # Has stderr content appended


def test_split_audio_no_chunks_produced_raises(tmp_path, monkeypatch):
    # Monkeypatch subprocess.run to succeed without creating chunk files
    import subprocess
    original_run = subprocess.run

    def fake_run(*args, **kwargs):
        # Return successful CompletedProcess but don't create any files
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    bad_file = tmp_path / "test.mp4"
    bad_file.write_bytes(b"not a real video")

    with pytest.raises(RuntimeError, match="no audio extracted from"):
        split_audio(bad_file, tmp_path / "chunks", chunk_seconds=5)


def test_transcribe_words_no_words_extracted_raises(tmp_path):
    images, _ = render_pdf(make_pdf(tmp_path / "s.pdf", 1))
    video = make_video(images, [0] * 12, tmp_path / "v.mp4")
    cfg = Config("k", "http://x", "alias-stt", "llm", "llm")

    class NoWordsClient:
        def __init__(self):
            self.calls = 0
            self.audio = NS(transcriptions=NS(create=self.create))

        def create(self, **kwargs):
            self.calls += 1
            return NS(words=None, segments=None, text="")

    client = NoWordsClient()
    cache = tmp_path / "words.json"
    with pytest.raises(RuntimeError, match="transcription returned no words"):
        transcribe_words(cfg, video, cache, chunk_seconds=5, client=client)
    # Cache file should not exist after error
    assert not cache.exists()


def test_bad_request_error_fallback_and_log(tmp_path, caplog):
    import logging
    images, _ = render_pdf(make_pdf(tmp_path / "s.pdf", 1))
    video = make_video(images, [0] * 12, tmp_path / "v.mp4")
    cfg = Config("k", "http://x", "alias-stt", "llm", "llm")
    client = FakeClient(raise_error_on_first=True)
    cache = tmp_path / "words.json"

    caplog.set_level(logging.WARNING)

    words = transcribe_words(cfg, video, cache, chunk_seconds=5, client=client)

    # (a) Warning text is in caplog.text
    assert "word timestamps not supported" in caplog.text

    # (b) First call has timestamp_granularities, second call (fallback) does not
    assert len(client.call_kwargs) >= 2
    assert "timestamp_granularities" in client.call_kwargs[0], "First call should attempt with timestamp_granularities"
    assert "timestamp_granularities" not in client.call_kwargs[1], "Second call (fallback) should not have timestamp_granularities"

    # (c) Returned words equal expected parsed words
    # FakeClient returns word "hi" with start=0.1, end=0.3
    # For 3 chunks (0, 5, 10), we expect words with adjusted offsets
    assert len(words) == 3
    assert words[0].w == "hi" and words[0].start == pytest.approx(0.1)
    assert words[1].w == "hi" and words[1].start == pytest.approx(5.1)
    assert words[2].w == "hi" and words[2].start == pytest.approx(10.1)

    # (d) Exact number of calls: first chunk fails and retries (2 calls), other chunks succeed (1 call each)
    assert client.calls == 4  # 2 for first chunk (fail + retry), 1 for second, 1 for third


def test_transcribe_words_cache_non_dict_meta_is_miss(tmp_path):
    # Test that a cache with non-dict "meta" is treated as a miss
    images, _ = render_pdf(make_pdf(tmp_path / "s.pdf", 1))
    video = make_video(images, [0] * 12, tmp_path / "v.mp4")
    cfg = Config("k", "http://x", "alias-stt", "llm", "llm")
    client = FakeClient()
    cache = tmp_path / "words.json"

    # Create cache with non-dict meta (string instead of dict)
    cache_data = {"meta": "not a dict", "words": []}
    cache.write_text(json.dumps(cache_data), encoding="utf-8")

    # Should re-transcribe due to invalid meta
    words = transcribe_words(cfg, video, cache, chunk_seconds=5, client=client)
    assert client.calls == 3  # Should have called 3 times for 3 chunks


def test_assign_words_empty_spans_returns_empty():
    words = [Word("a", 1, 2), Word("b", 3, 4)]
    assert assign_words(words, []) == []


class FlakyClient(FakeClient):
    """Fails (non-retryable) on the given call number."""

    def __init__(self, fail_on):
        super().__init__()
        self.fail_on = fail_on

    def create(self, **kwargs):
        if self.calls + 1 == self.fail_on:
            self.calls += 1
            raise RuntimeError("gateway exploded")
        return super().create(**kwargs)


def _setup_stt(tmp_path):
    images, _ = render_pdf(make_pdf(tmp_path / "s.pdf", 1))
    video = make_video(images, [0] * 12, tmp_path / "v.mp4")
    return video, Config("k", "http://x", "alias-stt", "llm", "llm"), tmp_path / "words.json"


def test_stt_resumes_from_chunk_cache_after_failure(tmp_path):
    video, cfg, cache = _setup_stt(tmp_path)
    partial = tmp_path / "words.json.chunks.json"
    flaky = FlakyClient(fail_on=3)
    with pytest.raises(RuntimeError, match="gateway exploded"):
        transcribe_words(cfg, video, cache, chunk_seconds=5, client=flaky)
    assert flaky.calls == 3 and not cache.exists()
    saved = json.loads(partial.read_text(encoding="utf-8"))
    assert sorted(saved["chunks"]) == ["0", "1"] and saved["meta"]["chunk_seconds"] == 5

    good = FakeClient()
    words = transcribe_words(cfg, video, cache, chunk_seconds=5, client=good)
    assert good.calls == 1  # only the missing chunk
    assert [round(w.start, 1) for w in words] == [0.1, 5.1, 10.1]
    assert cache.exists() and not partial.exists()
    assert [p.name for p in tmp_path.glob("*.tmp")] == []


def test_stt_chunk_cache_with_other_language_is_ignored(tmp_path):
    video, cfg, cache = _setup_stt(tmp_path)
    with pytest.raises(RuntimeError, match="gateway exploded"):
        transcribe_words(cfg, video, cache, language=None, chunk_seconds=5, client=FlakyClient(fail_on=3))
    good = FakeClient()
    transcribe_words(cfg, video, cache, language="de", chunk_seconds=5, client=good)
    assert good.calls == 3


def test_stt_cache_files_are_written_atomically(tmp_path):
    video, cfg, cache = _setup_stt(tmp_path)
    words = transcribe_words(cfg, video, cache, chunk_seconds=5, client=FakeClient())
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp" or p.name.endswith(".chunks.json")] == []
    assert [Word(**d) for d in json.loads(cache.read_text(encoding="utf-8"))["words"]] == words

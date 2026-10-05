import json
import subprocess
from pathlib import Path
from types import SimpleNamespace as NS

import httpx2
import openai
import pytest

from audiocuts import Clip
from config import Config
from helpers import make_wav
from transcribe import extract_clip, transcribe_clips

CFG = Config("k", "http://x", "alias-stt", "llm", "llm")


class FakeClient:
    """Answers every clip with 'text for <its file name>' so the tests can see which clip went where."""

    def __init__(self, fail_on=None, error=None):
        self.calls, self.fail_on, self.error = [], fail_on, error
        self.audio = NS(transcriptions=NS(create=self.create))

    def create(self, **kwargs):
        name = Path(kwargs["file"].name).name
        data = kwargs["file"].read()
        self.calls.append({"name": name, "size": len(data), **{k: v for k, v in kwargs.items() if k != "file"}})
        if self.fail_on == name:
            raise self.error or RuntimeError("boom")
        return NS(text=f"  text for {name} ")


@pytest.fixture
def audio(tmp_path):
    return make_wav(tmp_path / "a.wav", [("tone", 12.0)])


def loud(*bounds):
    return [Clip(a, b, b - a) for a, b in zip(bounds, bounds[1:])]


def test_every_clip_is_transcribed_separately_and_returned_in_order(tmp_path, audio):
    client = FakeClient()
    texts = transcribe_clips(CFG, audio, loud(0, 3, 6, 9, 12), tmp_path / "clips.json", client=client, workers=3)
    assert texts == [f"text for clip_{i}.mp3" for i in range(4)]  # stripped, one per clip, in order
    assert sorted(c["name"] for c in client.calls) == [f"clip_{i}.mp3" for i in range(4)]
    assert all(c["model"] == "alias-stt" and c["response_format"] == "verbose_json" and c["size"] > 1000
               and "language" not in c for c in client.calls)


def test_language_is_passed_on(tmp_path, audio):
    client = FakeClient()
    transcribe_clips(CFG, audio, loud(0, 6), tmp_path / "c.json", language="de", client=client)
    assert client.calls[0]["language"] == "de"


def test_clips_without_speech_are_not_sent_and_stay_empty(tmp_path, audio):
    clips = [Clip(0, 3, 3.0), Clip(3, 6, 0.2), Clip(6, 9, 0.0), Clip(9, 12, 3.0)]
    client = FakeClient()
    texts = transcribe_clips(CFG, audio, clips, tmp_path / "c.json", client=client)
    assert texts == ["text for clip_0.mp3", "", "", "text for clip_3.mp3"] and len(client.calls) == 2


def test_extract_clip_cuts_exactly_the_requested_part(tmp_path, audio):
    out = tmp_path / "cut.mp3"
    extract_clip(audio, 2.0, 5.5, out)
    duration = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                     str(out)], capture_output=True, check=True).stdout)
    assert duration == pytest.approx(3.5, abs=0.15)
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"not media")
    with pytest.raises(RuntimeError, match="clip"):
        extract_clip(junk, 0, 1, tmp_path / "x.mp3")


def test_results_are_cached_per_clip(tmp_path, audio):
    cache = tmp_path / "clips.json"
    first = FakeClient()
    texts = transcribe_clips(CFG, audio, loud(0, 4, 8, 12), cache, client=first)
    assert len(first.calls) == 3
    again = FakeClient()
    assert transcribe_clips(CFG, audio, loud(0, 4, 8, 12), cache, client=again) == texts and again.calls == []
    # a hand-corrected cut at another time is a new clip: only that one is sent again
    third = FakeClient()
    transcribe_clips(CFG, audio, loud(0, 4, 9, 12), cache, client=third)
    assert len(third.calls) == 2
    assert not list(tmp_path.glob("*.tmp"))


def test_a_failed_run_resumes_with_the_clips_that_are_still_missing(tmp_path, audio):
    cache = tmp_path / "clips.json"
    broken = FakeClient(fail_on="clip_2.mp3")
    with pytest.raises(RuntimeError, match="boom"):
        transcribe_clips(CFG, audio, loud(0, 3, 6, 9, 12), cache, client=broken, workers=1)
    saved = json.loads(cache.read_text(encoding="utf-8"))["clips"]
    assert len(saved) == 2  # clips 0 and 1 were finished and kept
    healthy = FakeClient()
    texts = transcribe_clips(CFG, audio, loud(0, 3, 6, 9, 12), cache, client=healthy, workers=1)
    assert len(healthy.calls) == 2 and len(texts) == 4 and all(texts)


def test_api_errors_are_not_swallowed_and_stop_the_remaining_work(tmp_path, audio):
    response = httpx2.Response(401, request=httpx2.Request("POST", "http://x"))
    client = FakeClient(fail_on="clip_0.mp3", error=openai.AuthenticationError("bad key", response=response, body=None))
    with pytest.raises(openai.AuthenticationError):
        transcribe_clips(CFG, audio, loud(0, 3, 6, 9, 12), tmp_path / "c.json", client=client, workers=1)
    assert len(client.calls) == 1  # no point asking again for the other clips


def test_cache_for_another_video_language_or_a_corrupt_file_is_ignored(tmp_path, audio):
    cache = tmp_path / "clips.json"
    transcribe_clips(CFG, audio, loud(0, 6), cache, client=FakeClient())
    german = FakeClient()
    transcribe_clips(CFG, audio, loud(0, 6), cache, language="de", client=german)
    assert len(german.calls) == 1
    cache.write_text("{broken", encoding="utf-8")
    again = FakeClient()
    transcribe_clips(CFG, audio, loud(0, 6), cache, client=again)
    assert len(again.calls) == 1
    cache.write_text(json.dumps({"meta": "nonsense", "clips": 3}), encoding="utf-8")
    third = FakeClient()
    transcribe_clips(CFG, audio, loud(0, 6), cache, client=third)
    assert len(third.calls) == 1


class CountingLimiter:
    def __init__(self):
        self.waits = 0

    def wait(self):
        self.waits += 1


def test_every_request_waits_for_the_limiter_but_cached_and_silent_clips_do_not(tmp_path, audio):
    limiter, cache = CountingLimiter(), tmp_path / "c.json"
    clips = [Clip(0, 4, 4.0), Clip(4, 8, 0.0), Clip(8, 12, 4.0)]
    transcribe_clips(CFG, audio, clips, cache, client=FakeClient(), limiter=limiter)
    assert limiter.waits == 2
    transcribe_clips(CFG, audio, clips, cache, client=FakeClient(), limiter=limiter)
    assert limiter.waits == 2


def test_a_media_file_that_cannot_be_cut_is_a_clear_error(tmp_path):
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"not media")
    with pytest.raises(RuntimeError, match="clip"):
        transcribe_clips(CFG, junk, loud(0, 5), tmp_path / "c.json", client=FakeClient())


def test_a_cache_that_cannot_be_saved_does_not_stop_the_transcription(tmp_path, audio, monkeypatch, caplog):
    import logging

    import transcribe

    def locked(path, data, **kwargs):
        raise PermissionError(13, "Zugriff verweigert")

    monkeypatch.setattr(transcribe, "atomic_write_json", locked)
    with caplog.at_level(logging.WARNING):
        texts = transcribe_clips(CFG, audio, loud(0, 3, 6, 9, 12), tmp_path / "clips.json", client=FakeClient())
    assert texts == [f"text for clip_{i}.mp3" for i in range(4)]  # the results are still returned
    assert "could not save" in caplog.text

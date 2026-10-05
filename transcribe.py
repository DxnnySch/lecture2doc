"""Speech to text, one request per slide clip.

The audio of every stretch on one slide is cut out (at the silent gaps between the slides' recordings, see
audiocuts.py) and transcribed on its own, so each word belongs to exactly one slide by construction. Nothing depends
on word timestamps, which the service only estimates."""
from __future__ import annotations

import json
import logging
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from api import make_client, retry_call
from fsutil import atomic_write_json

log = logging.getLogger(__name__)

MIN_SPEECH = 0.5  # seconds: a clip with less speech than this is not sent (silence makes the model hallucinate)


def extract_clip(media, start: float, end: float, out_path) -> None:
    """Cut [start, end] seconds of the audio of `media` into a small 16 kHz mono mp3."""
    cmd = ["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(media),
           "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", str(out_path)]
    try:
        subprocess.run(cmd, capture_output=True, check=True)
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode("utf-8", errors="replace") if e.stderr else ""
        raise RuntimeError(f"cutting the audio clip {start:.1f}-{end:.1f}s failed: {stderr[-500:]}") from e


def _transcribe(client, model: str, path, language: str | None, limiter) -> str:
    kwargs = {"language": language} if language else {}

    def call():
        if limiter is not None:
            limiter.wait()  # every attempt, including retries, counts against the quota
        with open(path, "rb") as f:
            return client.audio.transcriptions.create(model=model, file=f, response_format="verbose_json", **kwargs)

    result = retry_call(call)
    text = result.get("text") if isinstance(result, dict) else getattr(result, "text", "")
    return (text or "").strip()


def _load_cache(path: Path, meta: dict) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        clips = data["clips"]
        if data["meta"] == meta and isinstance(clips, dict):
            return {k: v for k, v in clips.items() if isinstance(v, str)}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return {}


def transcribe_clips(cfg, media, clips, cache_path, language: str | None = None, client=None, limiter=None,
                     workers: int = 4) -> list[str]:
    """The text of every clip (a list as long as `clips`; "" for clips without speech).

    Finished clips are cached at once, so a failed or interrupted run only repeats the clips still missing. The
    first error (an API error, ffmpeg failing) stops the remaining work and is raised."""
    cache_path = Path(cache_path)
    meta = {"video": Path(media).name, "size": Path(media).stat().st_size, "language": language}
    done = _load_cache(cache_path, meta)
    texts = [""] * len(clips)
    todo = []
    for i, clip in enumerate(clips):
        if clip.speech < MIN_SPEECH:
            continue
        key = f"{clip.start:.2f}-{clip.end:.2f}"
        if key in done:
            texts[i] = done[key]
        else:
            todo.append((i, key))
    if not todo:
        return texts
    client = client or make_client(cfg)
    lock, stop, errors, finished = threading.Lock(), threading.Event(), [], [0]
    with tempfile.TemporaryDirectory() as tmp:
        def work(item):
            i, key = item
            if stop.is_set():
                return
            try:
                path = Path(tmp) / f"clip_{i}.mp3"
                extract_clip(media, clips[i].start, clips[i].end, path)
                text = _transcribe(client, cfg.stt_model, path, language, limiter)
            except BaseException as exc:  # reported once all workers have stopped
                with lock:
                    errors.append(exc)
                stop.set()
                return
            with lock:
                texts[i] = text
                done[key] = text
                try:
                    atomic_write_json(cache_path, {"meta": meta, "clips": done})
                except OSError as exc:  # the text is in memory; a locked cache file must not abort the run
                    log.warning("could not save the transcription cache %s (%s); continuing without saving",
                                cache_path, exc)
                finished[0] += 1
                print(f"  transcribed clip {finished[0]}/{len(todo)}", flush=True)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(work, todo))
    if errors:
        raise errors[0]
    return texts

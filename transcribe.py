"""Speech-to-text via the ScaDS.AI endpoint: chunked audio -> timestamped words -> text per slide span."""
from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from openai import BadRequestError

from api import make_client, retry_call


@dataclass
class Word:
    w: str
    start: float
    end: float


def split_audio(video, out_dir: Path, chunk_seconds: int = 300) -> list[tuple[Path, float]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k",
         "-f", "segment", "-segment_time", str(chunk_seconds), "-reset_timestamps", "1",
         str(out_dir / "chunk%03d.mp3")],
        check=True,
    )
    files = sorted(out_dir.glob("chunk*.mp3"))
    return [(f, float(i * chunk_seconds)) for i, f in enumerate(files)]


def _get(obj, key):
    return obj[key] if isinstance(obj, dict) else getattr(obj, key)


def estimate_words(segments: list[tuple[float, float, str]]) -> list[Word]:
    """Spread each segment's words evenly over its time range."""
    out = []
    for start, end, text in segments:
        tokens = text.split()
        if not tokens:
            continue
        step = (end - start) / len(tokens)
        out.extend(Word(t, start + i * step, start + (i + 1) * step) for i, t in enumerate(tokens))
    return out


def parse_response(result, offset: float, chunk_seconds: float) -> list[Word]:
    words = [
        Word(_get(w, "word").strip(), _get(w, "start") + offset, _get(w, "end") + offset)
        for w in (getattr(result, "words", None) or [])
        if _get(w, "word").strip()
    ]
    if words:
        return words
    segments = getattr(result, "segments", None) or []
    if segments:
        return estimate_words([(_get(s, "start") + offset, _get(s, "end") + offset, _get(s, "text")) for s in segments])
    text = (getattr(result, "text", "") or "").strip()
    return estimate_words([(offset, offset + chunk_seconds, text)]) if text else []


def _transcribe_chunk(client, model: str, path: Path, language: str | None):
    kwargs = {"language": language} if language else {}

    def call(extra):
        with open(path, "rb") as f:
            return client.audio.transcriptions.create(
                model=model, file=f, response_format="verbose_json", **extra, **kwargs
            )

    try:
        return retry_call(lambda: call({"timestamp_granularities": ["word", "segment"]}))
    except BadRequestError:  # gateway rejected word timestamps
        return retry_call(lambda: call({}))


def transcribe_words(cfg, video, cache_path: Path, language: str | None = None,
                     chunk_seconds: int = 300, client=None) -> list[Word]:
    cache_path = Path(cache_path)
    if cache_path.exists():
        return [Word(**d) for d in json.loads(cache_path.read_text(encoding="utf-8"))]
    client = client or make_client(cfg)
    words: list[Word] = []
    with tempfile.TemporaryDirectory() as tmp:
        chunks = split_audio(video, Path(tmp), chunk_seconds)
        for i, (path, offset) in enumerate(chunks, 1):
            print(f"  transcribing chunk {i}/{len(chunks)}", flush=True)
            words.extend(parse_response(_transcribe_chunk(client, cfg.stt_model, path, language), offset, chunk_seconds))
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps([asdict(w) for w in words], ensure_ascii=False), encoding="utf-8")
    return words


def assign_words(words: list[Word], spans) -> list[str]:
    """Text per span. A word belongs to the span containing its midpoint; strays go to the first/last span."""
    buckets: list[list[str]] = [[] for _ in spans]
    for w in words:
        mid = (w.start + w.end) / 2
        idx = next((i for i, s in enumerate(spans) if mid < s.end), len(spans) - 1)
        buckets[idx].append(w.w)
    return [" ".join(b) for b in buckets]

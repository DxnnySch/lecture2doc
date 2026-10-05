"""LLM cleanup of transcripts using terminology from the whole slide deck, with guardrails."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from lecture2doc.api import retry_call
from lecture2doc.fsutil import atomic_write_json

log = logging.getLogger(__name__)

GLOSSARY_SYSTEM = (
    "You extract a glossary from university lecture slides. Output one entry per line, no numbering and no "
    "explanations: technical terms, acronyms (with their expansion in parentheses if the slides give one), "
    "standard/product names, and person or place names."
)
CLEAN_SYSTEM = (
    "You correct automatic speech-to-text transcripts of a university lecture. Fix misrecognised technical "
    "terms, names and acronyms using the glossary and slide text, and fix punctuation and capitalisation. "
    "Remove filler sounds (uh, um, äh) only. Do NOT add, remove, summarise, translate or reorder content. "
    "Keep the original language. Output only the corrected transcript. "
    "This transcript is an excerpt from a longer lecture and may start or end in the middle of a sentence. "
    "Never complete, extend or continue sentences. Never add words that were not spoken. "
    "Do not expand, shorten or replace names, abbreviations or terms with other forms from the glossary "
    "unless the spoken word is clearly a misrecognition of that exact term. "
    "When unsure about a correction, leave the original wording unchanged."
)
CONTEXT_CHARS = 1500
MAX_GLOSSARY_LINES = 400


class LLMCache:
    """Tiny JSON-file cache; path=None keeps it in memory only."""

    def __init__(self, path: Path | None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._data: dict[str, str] = {}
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._data = data
            except (json.JSONDecodeError, ValueError):
                pass  # Treat corrupt cache as empty

    def get(self, key: str) -> str | None:
        return self._data.get(key)

    def set(self, key: str, value: str) -> None:
        with self._lock:
            self._data[key] = value
            if self.path:
                try:
                    atomic_write_json(self.path, self._data)
                except OSError as exc:  # the result is still valid in memory; do not abort the run for a cache file
                    log.warning("could not save the cache %s (%s); continuing without saving", self.path, exc)


def strip_think(text: str) -> str:
    # Remove closed think blocks
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    # Remove unclosed think blocks (from <think> to end)
    text = re.sub(r"<think>.*$", "", text, flags=re.DOTALL)
    return text.strip()


def guard(raw: str, cleaned: str, tol: float = 0.25) -> str | None:
    if not cleaned.strip():
        return None
    if abs(len(cleaned) - len(raw)) / max(len(raw), 1) > tol:
        return None
    return cleaned


def build_outline(slides) -> str:
    return "\n".join(f"{s.number}. {s.title or '(untitled)'}" for s in slides)


def _chat(client, model: str, messages: list[dict], cache: LLMCache, limiter=None) -> str:
    key = hashlib.sha256(json.dumps([model, messages], ensure_ascii=False).encode()).hexdigest()
    hit = cache.get(key)
    if hit is not None:
        return hit
    def call():
        if limiter is not None:
            limiter.wait()  # every attempt, including retries, counts against the quota
        return client.chat.completions.create(model=model, messages=messages, temperature=0)

    resp = retry_call(call)
    out = strip_think(resp.choices[0].message.content or "")
    # Only cache non-empty results
    if out.strip():
        cache.set(key, out)
    return out


def build_glossary(client, model: str, slides, cache: LLMCache, max_chars: int = 40000, limiter=None,
                   on_progress=None) -> str:
    """`on_progress(done, total, decks)` is called after every request: `decks` is how many different slide decks
    (slides with a `deck` attribute) the finished requests covered."""
    chunks, decks, current, current_decks, size = [], [], [], set(), 0
    for s in slides:
        piece = f"Slide {s.number} ({s.title or 'untitled'}):\n{s.text}\n"
        if current and size + len(piece) > max_chars:
            chunks.append("".join(current))
            decks.append(current_decks)
            current, current_decks, size = [], set(), 0
        current.append(piece)
        current_decks.add(getattr(s, "deck", None))
        size += len(piece)
    if current:
        chunks.append("".join(current))
        decks.append(current_decks)
    seen: dict[str, str] = {}
    covered: set = set()
    for i, chunk in enumerate(chunks):
        try:
            reply = _chat(client, model, [{"role": "system", "content": GLOSSARY_SYSTEM},
                                          {"role": "user", "content": chunk}], cache, limiter)
        except Exception as exc:
            log.warning("build_glossary chunk extraction failed: %s", exc)
            reply = ""
        for line in reply.splitlines():
            term = re.sub(r"^\s*[-*•\d.)]+\s*", "", line).strip()
            if term:
                seen.setdefault(term.lower(), term)
        covered |= decks[i]
        if on_progress is not None:
            on_progress(i + 1, len(chunks), len(covered - {None}))
    return "\n".join(list(seen.values())[:MAX_GLOSSARY_LINES])


def _slide_text(slides, number: int) -> str:
    return slides[number - 1].text[:CONTEXT_CHARS] if 1 <= number <= len(slides) else ""


def cleanup_spans(client, model: str, glossary: str, outline: str, slides, spans, raws: list[str],
                  cache: LLMCache, workers: int = 4, limiter=None, on_progress=None) -> list[str]:
    if len(spans) != len(raws):
        raise ValueError(f"len(spans)={len(spans)} != len(raws)={len(raws)}")

    finished = [0]
    lock = threading.Lock()
    stopped = threading.Event()

    def clean_one(span, raw: str) -> str:
        if stopped.is_set():
            return raw
        result = clean_text(span, raw)
        if on_progress is not None:
            with lock:
                finished[0] += 1
                count = finished[0]
            try:
                on_progress(count, len(spans))
            except BaseException:  # e.g. the user cancelled: do not start the remaining parts
                stopped.set()
                raise
        return result

    def clean_text(span, raw: str) -> str:
        if not raw.strip():
            return raw
        n = span.slide
        user = (
            f"Deck outline:\n{outline}\n\nGlossary:\n{glossary}\n\n"
            f"Previous slide text:\n{_slide_text(slides, n - 1)}\n\n"
            f"Current slide ({n}) text:\n{_slide_text(slides, n)}\n\n"
            f"Next slide text:\n{_slide_text(slides, n + 1)}\n\n"
            f"Transcript to correct:\n{raw}"
        )
        try:
            cleaned = _chat(client, model, [{"role": "system", "content": CLEAN_SYSTEM},
                                            {"role": "user", "content": user}], cache, limiter)
        except Exception as exc:  # degrade to raw rather than abort the whole lecture
            log.warning("cleanup failed for slide %s span at %.0fs (%s); using raw transcript", n, span.start, exc)
            return raw
        ok = guard(raw, cleaned)
        if ok is None:
            log.warning("cleanup for slide %s span at %.0fs rejected by length guard; using raw transcript",
                        n, span.start)
            return raw
        return ok

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(clean_one, spans, raws))

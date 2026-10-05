"""LLM cleanup of transcripts using terminology from the whole slide deck, with guardrails."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from api import retry_call

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
    "Keep the original language. Output only the corrected transcript."
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
            self._data = json.loads(self.path.read_text(encoding="utf-8"))

    def get(self, key: str) -> str | None:
        return self._data.get(key)

    def set(self, key: str, value: str) -> None:
        with self._lock:
            self._data[key] = value
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")


def strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def guard(raw: str, cleaned: str, tol: float = 0.25) -> str | None:
    if not cleaned.strip():
        return None
    if abs(len(cleaned) - len(raw)) / max(len(raw), 1) > tol:
        return None
    return cleaned


def build_outline(slides) -> str:
    return "\n".join(f"{s.number}. {s.title or '(untitled)'}" for s in slides)


def _chat(client, model: str, messages: list[dict], cache: LLMCache) -> str:
    key = hashlib.sha256(json.dumps([model, messages], ensure_ascii=False).encode()).hexdigest()
    hit = cache.get(key)
    if hit is not None:
        return hit
    resp = retry_call(lambda: client.chat.completions.create(model=model, messages=messages, temperature=0))
    out = strip_think(resp.choices[0].message.content or "")
    cache.set(key, out)
    return out


def build_glossary(client, model: str, slides, cache: LLMCache, max_chars: int = 40000) -> str:
    chunks, current, size = [], [], 0
    for s in slides:
        piece = f"Slide {s.number} ({s.title or 'untitled'}):\n{s.text}\n"
        if current and size + len(piece) > max_chars:
            chunks.append("".join(current))
            current, size = [], 0
        current.append(piece)
        size += len(piece)
    if current:
        chunks.append("".join(current))
    seen: dict[str, str] = {}
    for chunk in chunks:
        reply = _chat(client, model, [{"role": "system", "content": GLOSSARY_SYSTEM},
                                      {"role": "user", "content": chunk}], cache)
        for line in reply.splitlines():
            term = re.sub(r"^\s*[-*•\d.)]+\s*", "", line).strip()
            if term:
                seen.setdefault(term.lower(), term)
    return "\n".join(list(seen.values())[:MAX_GLOSSARY_LINES])


def _slide_text(slides, number: int) -> str:
    return slides[number - 1].text[:CONTEXT_CHARS] if 1 <= number <= len(slides) else ""


def cleanup_spans(client, model: str, glossary: str, outline: str, slides, spans, raws: list[str],
                  cache: LLMCache, workers: int = 4) -> list[str]:
    def clean_one(span, raw: str) -> str:
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
                                            {"role": "user", "content": user}], cache)
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

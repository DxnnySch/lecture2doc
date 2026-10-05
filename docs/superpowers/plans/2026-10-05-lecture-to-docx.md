# Lecture Video → Per-Slide Transcript (Word) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A CLI that turns a lecture MP4 + slide PDF + slide PPTX into a Word document with one section per slide (number, title, transcript), using ScaDS.AI for speech-to-text and optional LLM terminology cleanup.

**Architecture:** Flat Python modules, one responsibility each: `config`/`api` (credentials, client, retry), `slides` (PDF render + PPTX titles), `segment` (ffmpeg frames → slide matching → time spans), `transcribe` (audio chunks → STT words → words per span), `cleanup` (glossary + per-span LLM correction with guardrails), `docx_out` (Word writer), `lecture2doc` (CLI orchestration). Frames are matched to PDF pages by correlation of downscaled grayscale images; there is a single speaker, so there is no diarization.

**Tech Stack:** Python 3.14, ffmpeg (on PATH), pymupdf, python-pptx, python-docx, Pillow, numpy, openai (ScaDS OpenAI-compatible endpoint), python-dotenv, pytest.

**Spec:** `docs/superpowers/specs/2026-10-05-lecture-to-docx-design.md`

## Global Constraints

- Credentials only from `.env` / environment: `SCADS_API_KEY`, `SCADS_BASE_URL` (default `https://llm.scads.ai/v1`), `SCADS_STT_MODEL` (default `alias-stt`), `SCADS_LLM_MODEL` (default `alias-huge-no-thinking`), `SCADS_GLOSSARY_MODEL` (default: value of `SCADS_LLM_MODEL`). The key is never printed, logged or written to any output file.
- STT: 16 kHz mono audio, 5-minute chunks, word timestamps requested, fall back to segment-level then chunk-level timing estimation.
- Cleanup: temperature 0; if cleaned length differs from raw by more than 25%, or the call fails, use the raw text and log a warning. Raw text always kept in `segments.json`.
- Word output uses real heading styles: `Slide N – Title` (just `Slide N` if no title); slides with no speech get `(no speech)`; revisited slides get one heading with several time-prefixed paragraphs (`[mm:ss]`, or `[h:mm:ss]` past one hour).
- Single speaker: no diarization, no speaker labels.
- Flat module layout in the project root, tests in `tests/`, `pytest.ini` with `pythonpath = .`.
- Never touch or commit `.env`, `samples/`, `output/` (all git-ignored).
- Commits end with the trailer `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

## Review Focus

Inputs and conditions the spec implies but would not otherwise be tested, most likely first. Each has a pinning test in the owning task.

1. **Hidden PPTX slides** (not exported to the PDF) make slide counts differ → titles must not silently shift; fall back to PDF-derived titles with a warning. (Task 2)
2. **Lecturer goes back to an earlier slide** → one heading for that slide, later span appended with a time prefix, no duplicate headings. (Tasks 3, 6)
3. **Black/blank or leading non-slide frames** (recording start, transitions, demo) → no NaN/crash; speech is attached to the neighbouring slide, never dropped. (Task 3)
4. **STT returns no word timestamps, or a slide has no speech** → timing estimated; slide still appears with `(no speech)`. (Tasks 4, 6)
5. **LLM returns empty/truncated/`<think>`-wrapped text, or errors** → raw transcript used for that span, run continues. (Task 5)

---

### Task 1: Project scaffold, config and API helpers

**Files:**
- Create: `requirements.txt`, `requirements-dev.txt`, `pytest.ini`, `config.py`, `api.py`, `tests/test_config.py`, `tests/test_api.py`

**Interfaces:**
- Produces: `config.Config` (frozen dataclass: `api_key, base_url, stt_model, llm_model, glossary_model`), `config.load_config(env_path: Path | None = None) -> Config`, `config.check_ffmpeg() -> None`, `config.ConfigError(Exception)`; `api.make_client(cfg) -> OpenAI`, `api.retry_call(fn, attempts=5, base_delay=2.0, sleep=time.sleep, retryable=RETRYABLE)`, `api.RETRYABLE`.

- [ ] **Step 1: Create files and environment**

`requirements.txt`:
```
openai>=1.0.0
python-dotenv>=1.0.0
pymupdf>=1.24
python-pptx>=1.0
python-docx>=1.1
pillow>=10.0
numpy>=1.26
```
`requirements-dev.txt`:
```
-r requirements.txt
pytest>=8.0
```
`pytest.ini`:
```
[pytest]
testpaths = tests
pythonpath = .
```
Run (Git Bash):
```bash
cd /c/Users/DannySchober/Documents/lecture_video-to-text
git init
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt
mkdir -p tests
```
Expected: installs succeed.

- [ ] **Step 2: Write failing tests**

`tests/test_config.py`:
```python
import pytest
from config import ConfigError, load_config

KEYS = ["SCADS_API_KEY", "SCADS_BASE_URL", "SCADS_STT_MODEL", "SCADS_LLM_MODEL", "SCADS_GLOSSARY_MODEL"]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in KEYS:
        monkeypatch.delenv(k, raising=False)


def test_defaults_and_glossary_falls_back_to_llm_model(tmp_path):
    env = tmp_path / ".env"
    env.write_text("SCADS_API_KEY=k\nSCADS_LLM_MODEL=my-llm\n")
    cfg = load_config(env)
    assert cfg.api_key == "k"
    assert cfg.base_url == "https://llm.scads.ai/v1"
    assert cfg.stt_model == "alias-stt"
    assert cfg.llm_model == "my-llm"
    assert cfg.glossary_model == "my-llm"


def test_glossary_model_can_differ(tmp_path):
    env = tmp_path / ".env"
    env.write_text("SCADS_API_KEY=k\nSCADS_GLOSSARY_MODEL=big\n")
    assert load_config(env).glossary_model == "big"
    assert load_config(env).llm_model == "alias-huge-no-thinking"


def test_real_environment_overrides_file(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("SCADS_API_KEY=from-file\n")
    monkeypatch.setenv("SCADS_API_KEY", "from-env")
    assert load_config(env).api_key == "from-env"


def test_missing_key_raises_without_leaking(tmp_path):
    env = tmp_path / ".env"
    env.write_text("SCADS_BASE_URL=x\n")
    with pytest.raises(ConfigError, match="SCADS_API_KEY"):
        load_config(env)
```
`tests/test_api.py`:
```python
import pytest
from api import retry_call


def test_retries_then_succeeds():
    calls = []

    def fn():
        calls.append(1)
        if len(calls) < 3:
            raise ValueError("boom")
        return "ok"

    delays = []
    assert retry_call(fn, sleep=delays.append, retryable=(ValueError,)) == "ok"
    assert len(calls) == 3
    assert delays == [2.0, 4.0]


def test_gives_up_after_attempts():
    with pytest.raises(ValueError):
        retry_call(lambda: (_ for _ in ()).throw(ValueError("x")), attempts=2, sleep=lambda s: None, retryable=(ValueError,))


def test_non_retryable_propagates_immediately():
    calls = []

    def fn():
        calls.append(1)
        raise KeyError("k")

    with pytest.raises(KeyError):
        retry_call(fn, sleep=lambda s: None, retryable=(ValueError,))
    assert len(calls) == 1
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_config.py tests/test_api.py -v`
Expected: FAIL (`ModuleNotFoundError: config` / `api`)

- [ ] **Step 4: Implement**

`config.py`:
```python
"""Configuration from .env / environment. Real environment variables win over the file."""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

DEFAULT_ENV = Path(__file__).parent / ".env"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    api_key: str
    base_url: str
    stt_model: str
    llm_model: str
    glossary_model: str

    def __repr__(self) -> str:  # never expose the key
        return f"Config(base_url={self.base_url!r}, stt_model={self.stt_model!r}, llm_model={self.llm_model!r})"


def load_config(env_path: Path | None = None) -> Config:
    values = {**dotenv_values(env_path or DEFAULT_ENV), **os.environ}
    key = values.get("SCADS_API_KEY")
    if not key:
        raise ConfigError("SCADS_API_KEY is not set (put it in .env, see .env.example)")
    llm = values.get("SCADS_LLM_MODEL") or "alias-huge-no-thinking"
    return Config(
        api_key=key,
        base_url=values.get("SCADS_BASE_URL") or "https://llm.scads.ai/v1",
        stt_model=values.get("SCADS_STT_MODEL") or "alias-stt",
        llm_model=llm,
        glossary_model=values.get("SCADS_GLOSSARY_MODEL") or llm,
    )


def check_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        raise ConfigError("ffmpeg not found on PATH")
```
`api.py`:
```python
"""ScaDS.AI client construction and retry helper."""
from __future__ import annotations

import time

from openai import APIConnectionError, InternalServerError, OpenAI, RateLimitError

RETRYABLE = (APIConnectionError, InternalServerError, RateLimitError)


def make_client(cfg) -> OpenAI:
    return OpenAI(api_key=cfg.api_key, base_url=cfg.base_url, timeout=600)


def retry_call(fn, attempts: int = 5, base_delay: float = 2.0, sleep=time.sleep, retryable=RETRYABLE):
    for i in range(attempts):
        try:
            return fn()
        except retryable:
            if i == attempts - 1:
                raise
            sleep(base_delay * 2**i)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest -v`
Expected: 7 passed

- [ ] **Step 6: Commit**

```bash
git add requirements.txt requirements-dev.txt pytest.ini config.py api.py tests .gitignore .env.example docs
git commit -m "feat: project scaffold, config loading and API retry helper" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Slides (PDF render + PPTX titles/text)

**Files:**
- Create: `slides.py`, `tests/helpers.py`, `tests/test_slides.py`

**Interfaces:**
- Produces: `slides.Slide` (dataclass: `number: int` (1-based), `title: str | None`, `text: str`, `image: PIL.Image.Image` RGB 480 px wide); `slides.render_pdf(pdf_path, width=480) -> tuple[list[Image], list[str]]` (images, page texts); `slides.read_pptx(pptx_path) -> list[tuple[str | None, str]]` (visible slides only: title, body text); `slides.load_slides(pdf_path, pptx_path) -> list[Slide]`.
- Test helpers produced: `tests/helpers.py::make_pdf(path, n, titles=None) -> Path` (n 960×540 pages with seeded random rectangles + title text), `make_pptx(path, slides: list[tuple[str | None, str, bool]]) -> Path` (title, body, hidden).

- [ ] **Step 1: Write helpers and failing tests**

`tests/helpers.py`:
```python
from pathlib import Path

import numpy as np
import pymupdf
from pptx import Presentation


def make_pdf(path: Path, n: int, titles: list[str] | None = None) -> Path:
    doc = pymupdf.open()
    for i in range(n):
        page = doc.new_page(width=960, height=540)
        rng = np.random.RandomState(i)
        for _ in range(12):
            x, y = int(rng.randint(0, 800)), int(rng.randint(60, 420))
            w, h = int(rng.randint(40, 160)), int(rng.randint(20, 100))
            page.draw_rect(pymupdf.Rect(x, y, x + w, y + h), color=None, fill=tuple(float(c) for c in rng.rand(3)))
        page.insert_text((40, 40), titles[i] if titles else f"Title {i + 1}", fontsize=24)
    doc.save(path)
    return path


def make_pptx(path: Path, slides: list[tuple[str | None, str, bool]]) -> Path:
    prs = Presentation()
    for title, body, hidden in slides:
        s = prs.slides.add_slide(prs.slide_layouts[1])
        if title is None:
            el = s.shapes.title._element
            el.getparent().remove(el)
        else:
            s.shapes.title.text = title
        s.placeholders[1].text = body
        if hidden:
            s._element.set("show", "0")
    prs.save(path)
    return path
```
`tests/test_slides.py`:
```python
import logging

from helpers import make_pdf, make_pptx
from slides import load_slides, read_pptx, render_pdf


def test_render_pdf(tmp_path):
    images, texts = render_pdf(make_pdf(tmp_path / "a.pdf", 3))
    assert len(images) == 3 and images[0].size == (480, 270)
    assert "Title 1" in texts[0]


def test_titles_from_pptx_when_counts_match(tmp_path):
    pdf = make_pdf(tmp_path / "a.pdf", 2)
    pptx = make_pptx(tmp_path / "a.pptx", [("Intro  to\nGDI", "body one", False), (None, "only body", False)])
    slides = load_slides(pdf, pptx)
    assert [s.number for s in slides] == [1, 2]
    assert slides[0].title == "Intro to GDI"
    assert "body one" in slides[0].text
    assert slides[1].title is None


def test_hidden_pptx_slides_are_skipped(tmp_path):
    pptx = make_pptx(tmp_path / "a.pptx", [("A", "x", False), ("Hidden", "y", True), ("C", "z", False)])
    assert [t for t, _ in read_pptx(pptx)] == ["A", "C"]
    pdf = make_pdf(tmp_path / "a.pdf", 2)  # PDF export omits hidden slide
    assert [s.title for s in load_slides(pdf, pptx)] == ["A", "C"]


def test_count_mismatch_falls_back_to_pdf_titles(tmp_path, caplog):
    pdf = make_pdf(tmp_path / "a.pdf", 3)
    pptx = make_pptx(tmp_path / "a.pptx", [("A", "x", False), ("B", "y", False)])
    with caplog.at_level(logging.WARNING):
        slides = load_slides(pdf, pptx)
    assert [s.title for s in slides] == ["Title 1", "Title 2", "Title 3"]
    assert "does not match" in caplog.text
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/test_slides.py -v`
Expected: FAIL (`ModuleNotFoundError: slides`)

- [ ] **Step 3: Implement `slides.py`**

```python
"""Slide material: page images from the PDF, titles/text from the PPTX."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import pymupdf
from PIL import Image
from pptx import Presentation

log = logging.getLogger(__name__)


@dataclass
class Slide:
    number: int
    title: str | None
    text: str
    image: Image.Image


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _first_line(text: str) -> str | None:
    for line in text.splitlines():
        if line.strip():
            return _squash(line)
    return None


def render_pdf(pdf_path, width: int = 480) -> tuple[list[Image.Image], list[str]]:
    images, texts = [], []
    with pymupdf.open(pdf_path) as doc:
        for page in doc:
            zoom = width / page.rect.width
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
            images.append(Image.frombytes("RGB", (pix.width, pix.height), pix.samples))
            texts.append(page.get_text())
    return images, texts


def read_pptx(pptx_path) -> list[tuple[str | None, str]]:
    """(title, body text) for every visible slide; hidden slides are not in the PDF export."""
    out = []
    for s in Presentation(pptx_path).slides:
        if s._element.get("show") == "0":
            continue
        t = s.shapes.title
        title = _squash(t.text_frame.text) if t is not None and t.has_text_frame else ""
        body = "\n".join(
            sh.text_frame.text for sh in s.shapes if sh.has_text_frame and sh.text_frame.text.strip()
        )
        out.append((title or None, body))
    return out


def load_slides(pdf_path, pptx_path) -> list[Slide]:
    images, pdf_texts = render_pdf(pdf_path)
    pptx = read_pptx(pptx_path)
    if len(pptx) == len(images):
        return [
            Slide(i + 1, title, body or pdf_texts[i].strip(), img)
            for i, ((title, body), img) in enumerate(zip(pptx, images))
        ]
    log.warning(
        "PPTX has %d visible slides but the PDF has %d pages: slide count does not match; "
        "using PDF text for titles", len(pptx), len(images),
    )
    return [Slide(i + 1, _first_line(t), t.strip(), img) for i, (t, img) in enumerate(zip(pdf_texts, images))]
```
- [ ] **Step 4: Run tests**

Run: `.venv/Scripts/python -m pytest tests/test_slides.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add slides.py tests/helpers.py tests/test_slides.py
git commit -m "feat: load slide images and titles from PDF/PPTX" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Segmentation (frames → slide matching → spans)

**Files:**
- Create: `segment.py`, `tests/test_segment.py`
- Modify: `tests/helpers.py` (add `make_video`)

**Interfaces:**
- Consumes: `slides.render_pdf`, `tests/helpers.make_pdf`.
- Produces: `segment.Span` (dataclass: `slide: int | None` 1-based, `start: float`, `end: float`, `confidence: float`); `segment.extract_frames(video, interval=1.0) -> np.ndarray` shape `(N, 108, 192)` uint8; `segment.slide_features(images: list[Image]) -> np.ndarray` shape `(K, D)`; `segment.match_frames(frames, feats, threshold=0.6) -> tuple[np.ndarray, np.ndarray]` (labels int, `-1` = no match, 0-based slide index; confidences float); `segment.smooth(labels, window=5) -> np.ndarray`; `segment.build_spans(labels, conf, interval) -> list[Span]`; `segment.absorb_unmatched(spans) -> list[Span]` (no `None` slides, adjacent same-slide spans merged; raises `ValueError` if nothing matched); `segment.unmatched_seconds(spans) -> float`.
- Test helper produced: `tests/helpers.py::make_video(images, order, path, fps=1) -> Path` — `order` is a list of slide indices (`None` = black frame), one frame per second, with a sine audio track of equal length.

- [ ] **Step 1: Add `make_video` helper and write failing tests**

Append to `tests/helpers.py`:
```python
import subprocess

from PIL import Image


def make_video(images, order, path: Path, fps: int = 1) -> Path:
    frames = path.parent / (path.stem + "_frames")
    frames.mkdir(exist_ok=True)
    for i, idx in enumerate(order):
        img = Image.new("RGB", (320, 180)) if idx is None else images[idx].resize((320, 180))
        img.save(frames / f"f{i:03d}.png")
    seconds = len(order) / fps
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-framerate", str(fps), "-i", str(frames / "f%03d.png"),
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(fps), "-c:a", "aac", "-shortest", str(path)],
        check=True,
    )
    return path
```
`tests/test_segment.py`:
```python
import numpy as np
import pytest

from helpers import make_pdf, make_video
from segment import (Span, absorb_unmatched, build_spans, extract_frames, match_frames, slide_features,
                     smooth, unmatched_seconds)
from slides import render_pdf


def run_pipeline(tmp_path, order, n_slides=3, threshold=0.6):
    images, _ = render_pdf(make_pdf(tmp_path / "s.pdf", n_slides))
    video = make_video(images, order, tmp_path / "v.mp4")
    frames = extract_frames(video, 1.0)
    labels, conf = match_frames(frames, slide_features(images), threshold)
    return build_spans(smooth(labels), conf, 1.0), frames


def test_extract_frames_shape(tmp_path):
    images, _ = render_pdf(make_pdf(tmp_path / "s.pdf", 2))
    video = make_video(images, [0] * 4, tmp_path / "v.mp4")
    frames = extract_frames(video, 1.0)
    assert frames.shape[1:] == (108, 192) and abs(len(frames) - 4) <= 1


def test_revisit_gives_three_spans(tmp_path):
    spans, _ = run_pipeline(tmp_path, [0] * 4 + [1] * 4 + [0] * 4)
    assert [s.slide for s in spans] == [1, 2, 1]
    assert abs(spans[0].end - 4) <= 1 and abs(spans[1].end - 8) <= 1


def test_smooth_removes_single_frame_glitch():
    labels = np.array([0, 0, 1, 0, 0])
    assert smooth(labels, 5).tolist() == [0, 0, 0, 0, 0]


def test_smooth_keeps_short_but_real_change():
    labels = np.array([0, 0, 0, 1, 1, 2, 2, 2])
    assert smooth(labels, 5)[3] == 1


def test_blank_and_leading_frames_are_unmatched_then_absorbed(tmp_path):
    spans, _ = run_pipeline(tmp_path, [None] * 3 + [0] * 4 + [None] * 3 + [1] * 4)
    assert spans[0].slide is None
    assert unmatched_seconds(spans) > 0
    resolved = absorb_unmatched(spans)
    assert all(s.slide is not None for s in resolved)
    assert [s.slide for s in resolved] == [1, 2]  # leading -> next slide, middle -> previous slide
    assert resolved[0].start == 0 and resolved[-1].end == spans[-1].end
    assert not any(np.isnan(s.confidence) for s in spans)


def test_absorb_raises_if_nothing_matched():
    with pytest.raises(ValueError, match="no frame matched"):
        absorb_unmatched([Span(None, 0, 5, 0.1)])


def test_build_spans_confidence_is_mean():
    spans = build_spans(np.array([0, 0, 1]), np.array([0.8, 1.0, 0.7]), 2.0)
    assert (spans[0].slide, spans[0].start, spans[0].end) == (1, 0.0, 4.0)
    assert spans[0].confidence == pytest.approx(0.9)
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/test_segment.py -v`
Expected: FAIL (`ModuleNotFoundError: segment`)

- [ ] **Step 3: Implement `segment.py`**

```python
"""Find which slide is on screen when: ffmpeg frames matched against PDF page images."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, replace

import numpy as np
from PIL import Image

FRAME_W, FRAME_H = 192, 108


@dataclass
class Span:
    slide: int | None  # 1-based; None = no slide matched
    start: float
    end: float
    confidence: float


def extract_frames(video, interval: float = 1.0) -> np.ndarray:
    cmd = [
        "ffmpeg", "-v", "error", "-i", str(video), "-an",
        "-vf", f"fps=1/{interval},scale={FRAME_W}:{FRAME_H}:flags=area,format=gray",
        "-f", "rawvideo", "-",
    ]
    data = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(data, np.uint8).reshape(-1, FRAME_H, FRAME_W)


def _normalise(mat: np.ndarray) -> np.ndarray:
    """Zero-mean, unit-norm rows. Constant (blank) rows become all-zero, scoring 0 against everything."""
    mat = mat.astype(np.float32)
    mat -= mat.mean(axis=1, keepdims=True)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    return np.divide(mat, norms, out=np.zeros_like(mat), where=norms > 1e-6)


def slide_features(images: list[Image.Image]) -> np.ndarray:
    rows = [np.asarray(im.convert("L").resize((FRAME_W, FRAME_H), Image.BOX)).ravel() for im in images]
    return _normalise(np.stack(rows))


def match_frames(frames: np.ndarray, feats: np.ndarray, threshold: float = 0.6) -> tuple[np.ndarray, np.ndarray]:
    scores = _normalise(frames.reshape(len(frames), -1)) @ feats.T
    best = scores.argmax(axis=1)
    conf = scores.max(axis=1)
    labels = np.where(conf >= threshold, best, -1)
    return labels, conf


def smooth(labels: np.ndarray, window: int = 5) -> np.ndarray:
    """Majority vote in a sliding window; on ties the current label wins."""
    half = window // 2
    out = labels.copy()
    for i in range(len(labels)):
        vals, counts = np.unique(labels[max(0, i - half): i + half + 1], return_counts=True)
        winners = vals[counts == counts.max()]
        if labels[i] not in winners:
            out[i] = winners[0]
    return out


def build_spans(labels: np.ndarray, conf: np.ndarray, interval: float) -> list[Span]:
    spans, i = [], 0
    while i < len(labels):
        j = i
        while j < len(labels) and labels[j] == labels[i]:
            j += 1
        slide = None if labels[i] < 0 else int(labels[i]) + 1
        spans.append(Span(slide, i * interval, j * interval, float(conf[i:j].mean())))
        i = j
    return spans


def unmatched_seconds(spans: list[Span]) -> float:
    return sum(s.end - s.start for s in spans if s.slide is None)


def absorb_unmatched(spans: list[Span]) -> list[Span]:
    """Give unmatched stretches to the previous slide (or the next one at the start); merge neighbours."""
    if all(s.slide is None for s in spans):
        raise ValueError(
            "no frame matched any slide; check that the PDF belongs to this video or lower --threshold"
        )
    last = None
    resolved = []
    for s in spans:
        if s.slide is None and last is not None:
            s = replace(s, slide=last)
        if s.slide is not None:
            last = s.slide
        resolved.append(s)
    first = next(s.slide for s in resolved if s.slide is not None)
    resolved = [replace(s, slide=first) if s.slide is None else s for s in resolved]
    merged = [resolved[0]]
    for s in resolved[1:]:
        prev = merged[-1]
        if s.slide == prev.slide:
            d1, d2 = prev.end - prev.start, s.end - s.start
            conf = (prev.confidence * d1 + s.confidence * d2) / (d1 + d2)
            merged[-1] = Span(prev.slide, prev.start, s.end, conf)
        else:
            merged.append(s)
    return merged
```

- [ ] **Step 4: Run tests**

Run: `.venv/Scripts/python -m pytest tests/test_segment.py -v`
Expected: 7 passed. If `test_revisit_gives_three_spans` is off by more than one second at boundaries, check `extract_frames` frame count first (video encoding at 1 fps can add a trailing frame).

- [ ] **Step 5: Commit**

```bash
git add segment.py tests/helpers.py tests/test_segment.py
git commit -m "feat: match video frames to PDF slides and build time spans" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Transcription (audio chunks → words → text per span)

**Files:**
- Create: `transcribe.py`, `tests/test_transcribe.py`

**Interfaces:**
- Consumes: `api.make_client`, `api.retry_call`, `config.Config`, `segment.Span`, `tests/helpers.make_video/make_pdf`.
- Produces: `transcribe.Word` (dataclass `w: str, start: float, end: float`); `transcribe.split_audio(video, out_dir, chunk_seconds=300) -> list[tuple[Path, float]]` (chunk path, offset seconds); `transcribe.estimate_words(segments: list[tuple[float, float, str]]) -> list[Word]`; `transcribe.parse_response(result, offset, chunk_seconds) -> list[Word]`; `transcribe.transcribe_words(cfg, video, cache_path, language=None, chunk_seconds=300, client=None) -> list[Word]` (reads/writes JSON cache); `transcribe.assign_words(words, spans) -> list[str]` (one text per span, same order).

- [ ] **Step 1: Write failing tests**

`tests/test_transcribe.py`:
```python
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
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/test_transcribe.py -v`
Expected: FAIL (`ModuleNotFoundError: transcribe`)

- [ ] **Step 3: Implement `transcribe.py`**

```python
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
```

- [ ] **Step 4: Run tests**

Run: `.venv/Scripts/python -m pytest tests/test_transcribe.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add transcribe.py tests/test_transcribe.py
git commit -m "feat: chunked ScaDS transcription with word timing and per-span assignment" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 5: LLM cleanup (glossary + guarded per-span correction)

**Files:**
- Create: `cleanup.py`, `tests/test_cleanup.py`

**Interfaces:**
- Consumes: `slides.Slide`, `segment.Span`, `api.retry_call`.
- Produces: `cleanup.LLMCache(path: Path | None)` with `get(key) -> str | None`, `set(key, value)`; `cleanup.strip_think(text) -> str`; `cleanup.guard(raw, cleaned, tol=0.25) -> str | None` (cleaned text if acceptable else `None`); `cleanup.build_outline(slides) -> str`; `cleanup.build_glossary(client, model, slides, cache, max_chars=40000) -> str`; `cleanup.cleanup_spans(client, model, glossary, outline, slides, spans, raws, cache, workers=4) -> list[str]` (same order and length as `spans`; raw text wherever cleaning is rejected or fails).

- [ ] **Step 1: Write failing tests**

`tests/test_cleanup.py`:
```python
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
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/test_cleanup.py -v`
Expected: FAIL (`ModuleNotFoundError: cleanup`)

- [ ] **Step 3: Implement `cleanup.py`**

```python
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
```

- [ ] **Step 4: Run tests**

Run: `.venv/Scripts/python -m pytest tests/test_cleanup.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add cleanup.py tests/test_cleanup.py
git commit -m "feat: glossary-based LLM transcript cleanup with guardrails and cache" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Word output

**Files:**
- Create: `docx_out.py`, `tests/test_docx_out.py`

**Interfaces:**
- Consumes: `slides.Slide`.
- Produces: `docx_out.SpanText` (dataclass `slide: int, start: float, text: str, raw: str`); `docx_out.fmt_time(seconds) -> str` (`mm:ss`, or `h:mm:ss` from one hour); `docx_out.paragraphize(text, per=4) -> list[str]` (groups of `per` sentences); `docx_out.write_docx(path, title, slides, span_texts, thumbnails=True, keep_raw=False) -> None`.

- [ ] **Step 1: Write failing tests**

`tests/test_docx_out.py`:
```python
from docx import Document
from PIL import Image

from docx_out import SpanText, fmt_time, paragraphize, write_docx
from slides import Slide


def slides():
    img = Image.new("RGB", (48, 27), "white")
    return [Slide(1, "Intro", "", img), Slide(2, None, "", img), Slide(3, "Outro", "", img)]


def read(path):
    doc = Document(path)
    return doc, [(p.style.name, p.text) for p in doc.paragraphs]


def test_fmt_time():
    assert fmt_time(75) == "01:15" and fmt_time(3725) == "1:02:05"


def test_paragraphize_groups_sentences():
    assert paragraphize("A. B! C? D. E.", per=4) == ["A. B! C? D.", "E."]
    assert paragraphize("no punctuation here") == ["no punctuation here"]


def test_headings_titles_and_no_speech(tmp_path):
    out = tmp_path / "o.docx"
    write_docx(out, "My Lecture", slides(), [SpanText(1, 0, "Hello there.", "hello there")])
    doc, paras = read(out)
    assert ("Title", "My Lecture") in paras
    assert ("Heading 1", "Slide 1 – Intro") in paras
    assert ("Heading 1", "Slide 2") in paras          # untitled -> number only
    assert ("Heading 1", "Slide 3 – Outro") in paras
    assert paras.count(("Normal", "(no speech)")) == 2
    assert ("Normal", "Hello there.") in paras        # single span: no time prefix
    assert len(doc.inline_shapes) == 3


def test_revisited_slide_has_one_heading_and_timed_paragraphs(tmp_path):
    out = tmp_path / "o.docx"
    spans = [SpanText(1, 0, "First visit.", "r"), SpanText(2, 30, "Middle.", "r"),
             SpanText(1, 3700, "Back again.", "r")]
    write_docx(out, "L", slides(), spans)
    _, paras = read(out)
    assert [t for s, t in paras if s == "Heading 1"].count("Slide 1 – Intro") == 1
    assert ("Normal", "[00:00] First visit.") in paras
    assert ("Normal", "[1:01:40] Back again.") in paras
    assert ("Normal", "Middle.") in paras


def test_options_no_thumbnails_and_keep_raw(tmp_path):
    out = tmp_path / "o.docx"
    write_docx(out, "L", slides(), [SpanText(1, 0, "Clean.", "raw words")], thumbnails=False, keep_raw=True)
    doc, paras = read(out)
    assert len(doc.inline_shapes) == 0
    assert ("Normal", "Raw: raw words") in paras
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/test_docx_out.py -v`
Expected: FAIL (`ModuleNotFoundError: docx_out`)

- [ ] **Step 3: Implement `docx_out.py`**

```python
"""Write the per-slide transcript Word document."""
from __future__ import annotations

import re
from dataclasses import dataclass
from io import BytesIO

from docx import Document
from docx.shared import Inches, Pt


@dataclass
class SpanText:
    slide: int
    start: float
    text: str
    raw: str


def fmt_time(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def paragraphize(text: str, per: int = 4) -> list[str]:
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]
    return [" ".join(sentences[i: i + per]) for i in range(0, len(sentences), per)]


def write_docx(path, title: str, slides, span_texts: list[SpanText],
               thumbnails: bool = True, keep_raw: bool = False) -> None:
    doc = Document()
    doc.add_heading(title, 0)
    by_slide: dict[int, list[SpanText]] = {}
    for st in sorted(span_texts, key=lambda s: s.start):
        if st.text.strip():
            by_slide.setdefault(st.slide, []).append(st)
    for s in slides:
        doc.add_heading(f"Slide {s.number} – {s.title}" if s.title else f"Slide {s.number}", 1)
        if thumbnails:
            buf = BytesIO()
            s.image.save(buf, "PNG")
            buf.seek(0)
            doc.add_picture(buf, width=Inches(3.5))
        entries = by_slide.get(s.number, [])
        if not entries:
            doc.add_paragraph().add_run("(no speech)").italic = True
            continue
        timed = len(entries) > 1
        for e in entries:
            for k, para in enumerate(paragraphize(e.text)):
                prefix = f"[{fmt_time(e.start)}] " if timed and k == 0 else ""
                doc.add_paragraph(prefix + para)
            if keep_raw:
                run = doc.add_paragraph().add_run("Raw: " + e.raw)
                run.italic = True
                run.font.size = Pt(9)
    doc.save(path)
```

- [ ] **Step 4: Run tests**

Run: `.venv/Scripts/python -m pytest tests/test_docx_out.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add docx_out.py tests/test_docx_out.py
git commit -m "feat: Word document writer with headings, thumbnails and revisit handling" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 7: CLI orchestration and end-to-end (synthetic) test

**Files:**
- Create: `lecture2doc.py`, `tests/test_pipeline.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `lecture2doc.main(argv: list[str] | None = None) -> int`; CLI `python lecture2doc.py VIDEO PDF PPTX [-o OUT.docx] [--workdir DIR] [--language CODE] [--no-cleanup] [--keep-raw] [--no-thumbnails] [--from-segments FILE] [--interval 1.0] [--threshold 0.6] [--smooth 5]`. Writes `<workdir>/words.json` (STT cache), `<workdir>/llm_cache.json`, `<workdir>/segments.json` (`{"video": str, "spans": [{"slide", "start", "end", "confidence", "raw", "clean"}]}`) and the docx. Default workdir `output/<video stem>/`, default docx `<workdir>/<video stem>.docx`. `--from-segments` reuses the `slide/start/end` of a (possibly hand-edited) `segments.json` and skips frame matching.

- [ ] **Step 1: Write failing end-to-end test**

`tests/test_pipeline.py`:
```python
import json

from docx import Document

import lecture2doc
from helpers import make_pdf, make_pptx, make_video
from slides import render_pdf
from transcribe import Word


def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("SCADS_API_KEY", "dummy")
    pdf = make_pdf(tmp_path / "s.pdf", 3)
    pptx = make_pptx(tmp_path / "s.pptx", [("Alpha", "a", False), ("Beta", "b", False), ("Gamma", "c", False)])
    images, _ = render_pdf(pdf)
    video = make_video(images, [0] * 4 + [1] * 4 + [0] * 4, tmp_path / "lec.mp4")
    words = [Word("hello", 1.0, 1.5), Word("world", 5.0, 5.5), Word("again", 9.0, 9.5)]
    monkeypatch.setattr(lecture2doc, "transcribe_words", lambda *a, **k: words)
    return video, pdf, pptx


def test_end_to_end_no_cleanup(tmp_path, monkeypatch):
    video, pdf, pptx = setup(tmp_path, monkeypatch)
    out = tmp_path / "out" / "lec.docx"
    assert lecture2doc.main([str(video), str(pdf), str(pptx), "-o", str(out), "--workdir", str(tmp_path / "w"),
                             "--no-cleanup"]) == 0
    texts = [(p.style.name, p.text) for p in Document(out).paragraphs]
    assert ("Heading 1", "Slide 1 – Alpha") in texts and ("Heading 1", "Slide 3 – Gamma") in texts
    assert ("Normal", "[00:00] hello") in texts and ("Normal", "[00:08] again") in texts or any(
        t.endswith("again") for _, t in texts)
    assert ("Normal", "world") in texts
    assert ("Normal", "(no speech)") in texts  # slide 3 never shown
    seg = json.loads((tmp_path / "w" / "segments.json").read_text(encoding="utf-8"))
    assert [s["slide"] for s in seg["spans"]] == [1, 2, 1]
    assert "dummy" not in json.dumps(seg)


def test_from_segments_skips_matching(tmp_path, monkeypatch):
    video, pdf, pptx = setup(tmp_path, monkeypatch)
    seg = tmp_path / "seg.json"
    seg.write_text(json.dumps({"spans": [{"slide": 3, "start": 0, "end": 12, "confidence": 1.0}]}))
    monkeypatch.setattr(lecture2doc, "extract_frames", lambda *a, **k: (_ for _ in ()).throw(AssertionError("matched")))
    out = tmp_path / "o.docx"
    lecture2doc.main([str(video), str(pdf), str(pptx), "-o", str(out), "--workdir", str(tmp_path / "w"),
                      "--no-cleanup", "--from-segments", str(seg)])
    texts = [p.text for p in Document(out).paragraphs]
    assert "hello world again" in texts


def test_from_segments_rejects_unknown_slide(tmp_path, monkeypatch):
    video, pdf, pptx = setup(tmp_path, monkeypatch)
    seg = tmp_path / "seg.json"
    seg.write_text(json.dumps({"spans": [{"slide": 99, "start": 0, "end": 12, "confidence": 1.0}]}))
    import pytest
    with pytest.raises(SystemExit, match="99"):
        lecture2doc.main([str(video), str(pdf), str(pptx), "-o", str(tmp_path / "o.docx"),
                          "--workdir", str(tmp_path / "w"), "--no-cleanup", "--from-segments", str(seg)])
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/test_pipeline.py -v`
Expected: FAIL (`ModuleNotFoundError: lecture2doc`)

- [ ] **Step 3: Implement `lecture2doc.py`**

```python
#!/usr/bin/env python3
"""Turn a lecture video + slide PDF/PPTX into a Word document with one transcript section per slide.

Usage:
    python lecture2doc.py VIDEO.mp4 SLIDES.pdf SLIDES.pptx [-o OUT.docx] [--no-cleanup] [--keep-raw]
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from api import make_client
from cleanup import LLMCache, build_glossary, build_outline, cleanup_spans
from config import ConfigError, check_ffmpeg, load_config
from docx_out import SpanText, fmt_time, write_docx
from segment import (Span, absorb_unmatched, build_spans, extract_frames, match_frames, slide_features, smooth,
                     unmatched_seconds)
from slides import load_slides
from transcribe import assign_words, transcribe_words


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("video")
    p.add_argument("pdf")
    p.add_argument("pptx")
    p.add_argument("-o", "--output", help="output .docx (default: <workdir>/<video stem>.docx)")
    p.add_argument("--workdir", help="cache/debug directory (default: output/<video stem>)")
    p.add_argument("--language", help="language code for STT (default: auto-detect)")
    p.add_argument("--no-cleanup", action="store_true", help="skip LLM transcript cleanup")
    p.add_argument("--keep-raw", action="store_true", help="also put the raw transcript in the document")
    p.add_argument("--no-thumbnails", action="store_true")
    p.add_argument("--from-segments", help="reuse slide/time spans from a (hand-corrected) segments.json")
    p.add_argument("--interval", type=float, default=1.0, help="seconds between sampled frames")
    p.add_argument("--threshold", type=float, default=0.6, help="minimum match score for a frame to count")
    p.add_argument("--smooth", type=int, default=5, help="majority-vote window in frames")
    return p.parse_args(argv)


def load_spans(path, n_slides: int) -> list[Span]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))["spans"]
    spans = [Span(d["slide"], d["start"], d["end"], d.get("confidence", 1.0)) for d in raw]
    bad = sorted({s.slide for s in spans if s.slide is None or not 1 <= s.slide <= n_slides})
    if bad:
        raise SystemExit(f"segments file refers to slides {bad}, but the deck has slides 1-{n_slides}")
    return spans


def print_summary(spans: list[Span], n_slides: int) -> None:
    seen = {s.slide for s in spans if s.slide}
    total = sum(s.end - s.start for s in spans) or 1.0
    unmatched = unmatched_seconds(spans)
    print(f"  {len(spans)} spans; {unmatched:.0f}s of {total:.0f}s ({100 * unmatched / total:.0f}%) matched no slide")
    missing = [n for n in range(1, n_slides + 1) if n not in seen]
    if missing:
        print(f"  slides never shown: {missing}")


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    try:
        check_ffmpeg()
        cfg = load_config()
    except ConfigError as exc:
        raise SystemExit(str(exc))
    video = Path(args.video)
    workdir = Path(args.workdir) if args.workdir else Path("output") / video.stem
    workdir.mkdir(parents=True, exist_ok=True)
    out = Path(args.output) if args.output else workdir / f"{video.stem}.docx"
    out.parent.mkdir(parents=True, exist_ok=True)

    print("Loading slides...")
    slides = load_slides(args.pdf, args.pptx)
    print(f"  {len(slides)} slides")

    if args.from_segments:
        spans = load_spans(args.from_segments, len(slides))
    else:
        print("Matching video frames to slides...")
        frames = extract_frames(video, args.interval)
        labels, conf = match_frames(frames, slide_features([s.image for s in slides]), args.threshold)
        spans = build_spans(smooth(labels, args.smooth), conf, args.interval)
        print_summary(spans, len(slides))
        try:
            spans = absorb_unmatched(spans)
        except ValueError as exc:
            raise SystemExit(str(exc))

    print("Transcribing...")
    words = transcribe_words(cfg, video, workdir / "words.json", args.language)
    raws = assign_words(words, spans)

    if args.no_cleanup:
        cleans = raws
    else:
        print("Cleaning up transcripts...")
        client = make_client(cfg)
        cache = LLMCache(workdir / "llm_cache.json")
        outline = build_outline(slides)
        glossary = build_glossary(client, cfg.glossary_model, slides, cache)
        cleans = cleanup_spans(client, cfg.llm_model, glossary, outline, slides, spans, raws, cache)

    (workdir / "segments.json").write_text(json.dumps({
        "video": str(video),
        "spans": [{"slide": s.slide, "start": s.start, "end": s.end, "confidence": round(s.confidence, 3),
                   "raw": r, "clean": c} for s, r, c in zip(spans, raws, cleans)],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    write_docx(out, video.stem, slides,
               [SpanText(s.slide, s.start, c, r) for s, r, c in zip(spans, raws, cleans)],
               thumbnails=not args.no_thumbnails, keep_raw=args.keep_raw)
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the whole suite**

Run: `.venv/Scripts/python -m pytest -v`
Expected: all tests pass (≈ 38). In `test_end_to_end_no_cleanup` the compound `or` assertion is deliberately lenient about the exact timestamp of the revisit; if `("Normal", "world")` or the heading assertions fail, debug segmentation first (print `spans`).

- [ ] **Step 5: Commit**

```bash
git add lecture2doc.py tests/test_pipeline.py
git commit -m "feat: lecture2doc CLI tying segmentation, STT, cleanup and Word output together" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Real-lecture subset run and tuning

**Files:**
- Modify: `lecture2doc.py` / `segment.py` only if tuning demands it; add `README.md` (usage, `.env` variables, `--from-segments` workflow, tuning flags).

**Interfaces:** consumes the finished CLI. Uses `samples/mp4/L-GDI-2-SDSS.mp4`, `samples/pdf/L-GDI-2-SDSS.pdf` (46 pages), `samples/pptx/L-GDI-2-SDSS.pptx`.

- [ ] **Step 1: Cut the test subset (original untouched)**

```bash
ffmpeg -y -ss 600 -t 480 -i samples/mp4/L-GDI-2-SDSS.mp4 -c copy samples/mp4/L-GDI-2-SDSS_subset.mp4
```
Expected: an 8-minute file. (Frame at 600 s is slide 11, "Techniques for decision support".)

- [ ] **Step 2: Run without cleanup first (cheap check of matching + STT)**

```bash
.venv/Scripts/python lecture2doc.py samples/mp4/L-GDI-2-SDSS_subset.mp4 samples/pdf/L-GDI-2-SDSS.pdf samples/pptx/L-GDI-2-SDSS.pptx --no-cleanup --workdir output/subset
```
Expected: summary line with a low unmatched percentage; `output/subset/segments.json`, `words.json`, and a docx. Inspect `segments.json`: slide numbers should be mostly increasing; for 3 spans, extract the frame at the span start (`ffmpeg -ss <start+600> -i samples/mp4/L-GDI-2-SDSS.mp4 -frames:v 1 f.png`) and confirm visually it equals the PDF page.

- [ ] **Step 3: Tune if needed**

If many spans are unmatched or flip between neighbouring slides, adjust `--threshold` (try 0.5–0.8) and `--smooth` (3–9), rerun (STT is cached in `words.json`, so reruns are free), and record the chosen defaults in `lecture2doc.py` if they differ from 0.6/5.

- [ ] **Step 4: Run with cleanup and compare**

```bash
.venv/Scripts/python lecture2doc.py samples/mp4/L-GDI-2-SDSS_subset.mp4 samples/pdf/L-GDI-2-SDSS.pdf samples/pptx/L-GDI-2-SDSS.pptx --workdir output/subset --keep-raw -o output/subset/subset_clean.docx
```
Check in the docx that corrected terms look right (raw vs. clean), no content was added/dropped, and any "using raw transcript" warnings are few. Report to the user: matched %, number of spans, notable corrections, any model-choice observations (no-thinking vs reasoning for the glossary).

- [ ] **Step 5: Write `README.md`, commit**

README covers: purpose, setup (`python -m venv .venv`, `pip install -r requirements.txt`, ffmpeg, `.env` variables), usage example, option list, the `segments.json` correct-and-regenerate workflow, and known limits (diarization/summaries out of scope).
```bash
git add README.md lecture2doc.py segment.py
git commit -m "docs: README and tuned defaults after real-lecture run" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Self-Review

- **Spec coverage:** slides/titles (T2); frame matching, smoothing, unmatched handling (T3); chunked STT with word timing + fallbacks + word assignment (T4); glossary + per-span cleanup + guardrails + cache + separate glossary model (T5, config in T1); docx with headings, thumbnails, `(no speech)`, revisit timestamps, `--keep-raw`, `--no-thumbnails` (T6); CLI options `--language`, `--no-cleanup`, `--from-segments`, `segments.json` (T7); unit + one real run (T8). Additions beyond the spec: `words.json` STT cache and `--interval/--threshold/--smooth/--workdir` flags (needed to make tuning and reruns cheap).
- **Placeholders:** none; the one wording note in Task 2 is resolved in its Step 4.
- **Type consistency:** `Span(slide, start, end, confidence)`, `Word(w, start, end)`, `SpanText(slide, start, text, raw)`, `Slide(number, title, text, image)`, `LLMCache`, `cleanup_spans(client, model, glossary, outline, slides, spans, raws, cache)` are used identically in tasks 3–7.
- **Review Focus:** each of the five items has a named test (hidden slides → T2; revisit → T3/T6; blank/leading frames → T3; no word timestamps / no speech → T4/T6; bad LLM output → T5).

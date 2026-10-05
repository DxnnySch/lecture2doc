"""Write the per-slide transcript Word document."""
from __future__ import annotations

import re
from dataclasses import dataclass
from io import BytesIO

from docx import Document
from docx.shared import Inches, Pt

MAX_WORDS = 80


@dataclass
class SpanText:
    slide: int
    start: float
    text: str
    raw: str
    end: float | None = None


def _clean_xml(s: str) -> str:
    """Remove XML-invalid control characters [\x00-\x08\x0b\x0c\x0e-\x1f]."""
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', s)


def fmt_time(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def paragraphize(text: str, per: int = 4) -> list[str]:
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]
    result = []
    current_group = []

    for s in sentences:
        words = s.split()
        if len(words) > MAX_WORDS:
            # Flush current group first
            if current_group:
                result.append(" ".join(current_group))
                current_group = []
            # Emit long sentence as chunks
            for i in range(0, len(words), MAX_WORDS):
                result.append(" ".join(words[i:i + MAX_WORDS]))
        else:
            # Add to current group
            current_group.append(s)
            if len(current_group) == per:
                result.append(" ".join(current_group))
                current_group = []

    # Flush any remaining group
    if current_group:
        result.append(" ".join(current_group))

    return result


def time_ranges(span_texts: list[SpanText]) -> dict[int, str]:
    """Per slide, the video time ranges it was shown, e.g. '12:30 – 15:02 · 40:10 – 41:00'."""
    ranges: dict[int, list[str]] = {}
    for st in sorted(span_texts, key=lambda s: s.start):
        if st.end is not None:
            ranges.setdefault(st.slide, []).append(f"{fmt_time(st.start)} – {fmt_time(st.end)}")
    return {slide: " · ".join(r) for slide, r in ranges.items()}


def _add_heading(doc, slide, page_break: bool) -> None:
    text = f"Slide {slide.number} – {_clean_xml(slide.title)}" if slide.title else f"Slide {slide.number}"
    paragraph = doc.add_heading(text, 1)
    if page_break:
        paragraph.paragraph_format.page_break_before = True


def _add_thumbnail(doc, slide) -> None:
    buf = BytesIO()
    slide.image.save(buf, "PNG")
    buf.seek(0)
    doc.add_picture(buf, width=Inches(3.5))


def _add_transcript(doc, text: str, raw: str, keep_raw: bool, prefix: str = "") -> None:
    for k, para in enumerate(paragraphize(text)):
        doc.add_paragraph((prefix if k == 0 else "") + _clean_xml(para))
    if keep_raw:
        run = doc.add_paragraph().add_run("Raw: " + _clean_xml(raw))
        run.italic = True
        run.font.size = Pt(9)


def _write_segments(doc, slides, span_texts, thumbnails, keep_raw, timestamps, page_breaks) -> None:
    """One page per stretch of the lecture on one slide, in lecture order (a revisited slide gets a new page)."""
    by_number = {s.number: s for s in slides}
    shown = 0
    for st in sorted(span_texts, key=lambda s: s.start):
        slide = by_number.get(st.slide)
        if slide is None:
            continue
        _add_heading(doc, slide, page_breaks and shown > 0)  # the first stays with the document title
        shown += 1
        if timestamps and st.end is not None:
            doc.add_paragraph(f"{fmt_time(st.start)} – {fmt_time(st.end)}", style="Subtitle")
        if thumbnails:
            _add_thumbnail(doc, slide)
        if st.text.strip():
            _add_transcript(doc, st.text, st.raw, keep_raw)
        else:
            doc.add_paragraph().add_run("(no speech)").italic = True


def _write_by_slide(doc, slides, span_texts, thumbnails, keep_raw, timestamps, page_breaks) -> None:
    """Every slide of the deck once, in slide order, with all of its visits under one heading."""
    shown = time_ranges(span_texts) if timestamps else {}
    by_slide: dict[int, list[SpanText]] = {}
    for st in sorted(span_texts, key=lambda s: s.start):
        if st.text.strip():
            by_slide.setdefault(st.slide, []).append(st)
    for index, s in enumerate(slides):
        _add_heading(doc, s, page_breaks and index > 0)  # slide 1 stays with the document title
        if s.number in shown:
            doc.add_paragraph(shown[s.number], style="Subtitle")
        if thumbnails:
            _add_thumbnail(doc, s)
        entries = by_slide.get(s.number, [])
        if not entries:
            doc.add_paragraph().add_run("(no speech)").italic = True
            continue
        timed = len(entries) > 1
        for e in entries:
            _add_transcript(doc, e.text, e.raw, keep_raw, prefix=f"[{fmt_time(e.start)}] " if timed else "")


def write_docx(path, title: str, slides, span_texts: list[SpanText],
               thumbnails: bool = True, keep_raw: bool = False, timestamps: bool = True,
               page_breaks: bool = True, group_by_slide: bool = False) -> None:
    """By default one page per stretch of the lecture on one slide, in lecture order; with group_by_slide every
    slide of the deck appears once, in slide order, with all its visits under one heading."""
    doc = Document()
    doc.add_heading(_clean_xml(title), 0)
    write = _write_by_slide if group_by_slide else _write_segments
    write(doc, slides, span_texts, thumbnails, keep_raw, timestamps, page_breaks)
    doc.save(path)

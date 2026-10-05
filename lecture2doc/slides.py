"""Slide material from the PDF export: page images, titles and text."""
from __future__ import annotations

import re
from dataclasses import dataclass

import pymupdf
from PIL import Image

TITLE_LIMIT = 100
TITLE_REGION = 0.6  # titles sit in the upper part of a slide; footers and page numbers sit at the bottom


@dataclass
class Slide:
    number: int
    title: str | None
    text: str
    image: Image.Image


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _truncate(line: str | None, limit: int = TITLE_LIMIT) -> str | None:
    if line is not None and len(line) > limit:
        return line[: limit - 1].rstrip() + "…"
    return line


def _page_title(page) -> str | None:
    """The largest text in the upper part of the page (topmost on ties); wrapped lines of the same block that
    share that size are joined. Reading order is useless here: in many exports the footer comes first."""
    limit = page.rect.height * TITLE_REGION
    lines = []  # (font size, top y, block number, text)
    for number, block in enumerate(page.get_text("dict")["blocks"]):
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            text = _squash("".join(span["text"] for span in line["spans"]))
            if text and line["bbox"][1] < limit:
                lines.append((max(span["size"] for span in line["spans"]), line["bbox"][1], number, text))
    if not lines:
        return None
    size, _, block_number, _ = max(lines, key=lambda l: (round(l[0], 1), -l[1]))
    parts = sorted((l for l in lines if l[2] == block_number and abs(l[0] - size) < 0.6), key=lambda l: l[1])
    return _truncate(_squash(" ".join(l[3] for l in parts)))


def pdf_titles(pdf_path) -> list[str | None]:
    with pymupdf.open(pdf_path) as doc:
        return [_page_title(page) for page in doc]


def render_pdf(pdf_path, width: int = 480) -> tuple[list[Image.Image], list[str]]:
    images, texts = [], []
    with pymupdf.open(pdf_path) as doc:
        for page in doc:
            zoom = width / page.rect.width
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
            images.append(Image.frombytes("RGB", (pix.width, pix.height), pix.samples))
            texts.append(page.get_text())
    return images, texts


def load_slides(pdf_path) -> list[Slide]:
    images, texts = render_pdf(pdf_path)
    titles = pdf_titles(pdf_path)
    return [Slide(i + 1, titles[i], t.strip(), img) for i, (t, img) in enumerate(zip(texts, images))]

"""Identify the slide on screen from the page number printed in its corner.

A half-built (animated) slide hardly resembles its finished PDF page, but the page number in the corner is always
there. No OCR is needed: the PDF tells us where the numbers sit and what they look like, so the corner of every
video frame is simply compared with the same corner of every PDF page.
"""
from __future__ import annotations

import math
import re
import statistics
import subprocess
from dataclasses import dataclass

import numpy as np
import pymupdf
from PIL import Image

from lecture2doc.segment import _normalise, probe_size

SCALE = 2             # pixels per PDF point in strips and templates
MIN_COVERAGE = 0.6    # share of pages that must print a (distinct) number for the corner to be usable
BASELINE_TOLERANCE = 6  # points: numbers on different pages must sit on the same line
PAD_X, PAD_Y = 6, 3     # points around the numbers (they are wider on some pages than on others)


@dataclass(frozen=True)
class NumberRegion:
    """Where the page number sits, in PDF points on a page of size page_w x page_h."""
    x0: int
    y0: int
    x1: int
    y1: int
    page_w: float
    page_h: float


@dataclass
class NumberEvidence:
    """mask[i, k]: the corner of frame i allows page k; score[i]: how well it matched (0 where it names no page)."""
    mask: np.ndarray
    score: np.ndarray


def find_number_region(pdf_path) -> NumberRegion | None:
    """The corner area holding the page numbers, or None if the deck does not print usable ones."""
    found = []  # (value, x0, y0, x1, y1): the right-most small number in the bottom-right corner of a page
    with pymupdf.open(pdf_path) as doc:
        if len(doc) < 2:
            return None
        page_w, page_h = doc[0].rect.width, doc[0].rect.height
        for page in doc:
            w, h = page.rect.width, page.rect.height
            words = [t for t in page.get_text("words")
                     if re.fullmatch(r"\d{1,3}", t[4]) and t[0] > 0.7 * w and t[1] > 0.8 * h]
            if words:
                x0, y0, x1, y1, text = max(words, key=lambda t: t[2])[:5]
                sx, sy = page_w / w, page_h / h
                found.append((int(text), x0 * sx, y0 * sy, x1 * sx, y1 * sy))
        needed = MIN_COVERAGE * len(doc)
    if len(found) < needed:
        return None
    median_top = statistics.median(f[2] for f in found)
    median_bottom = statistics.median(f[4] for f in found)
    kept = [f for f in found if abs(f[2] - median_top) <= BASELINE_TOLERANCE
            and abs(f[4] - median_bottom) <= BASELINE_TOLERANCE]
    if len(kept) < needed or len({f[0] for f in kept}) < needed:
        return None
    x0 = max(0.0, min(f[1] for f in kept) - PAD_X)
    y0 = max(0.0, min(f[2] for f in kept) - PAD_Y)
    x1 = min(page_w, max(f[3] for f in kept) + PAD_X)
    y1 = min(page_h, max(f[4] for f in kept) + PAD_Y)
    return NumberRegion(math.floor(x0), math.floor(y0), math.ceil(x1), math.ceil(y1), page_w, page_h)


def number_templates(pdf_path, region: NumberRegion) -> np.ndarray:
    """One normalised row per page: that page's corner as it looks at SCALE pixels per point."""
    size = (SCALE * (region.x1 - region.x0), SCALE * (region.y1 - region.y0))
    clip = pymupdf.Rect(region.x0, region.y0, region.x1, region.y1)
    rows = []
    with pymupdf.open(pdf_path) as doc:
        for page in doc:
            pix = page.get_pixmap(matrix=pymupdf.Matrix(SCALE, SCALE), clip=clip, alpha=False,
                                  colorspace=pymupdf.csGRAY)
            corner = Image.frombytes("L", (pix.width, pix.height), pix.samples)
            if corner.size != size:
                corner = corner.resize(size, Image.BOX)
            rows.append(np.asarray(corner).ravel())
    return _normalise(np.stack(rows))


def frame_crop(region: NumberRegion, video_w: int, video_h: int) -> tuple[int, int, int, int]:
    """(x, y, w, h) in video pixels of the number corner, for a slide shown (letter/pillarboxed) in the frame."""
    slide_aspect = region.page_w / region.page_h
    if slide_aspect >= video_w / video_h:
        sw, sh = video_w, video_w / slide_aspect
    else:
        sw, sh = video_h * slide_aspect, video_h
    ox, oy = (video_w - sw) / 2, (video_h - sh) / 2
    x = round(ox + region.x0 / region.page_w * sw)
    y = round(oy + region.y0 / region.page_h * sh)
    w = round((region.x1 - region.x0) / region.page_w * sw)
    h = round((region.y1 - region.y0) / region.page_h * sh)
    x, y = max(0, min(x, video_w - 2)), max(0, min(y, video_h - 2))
    return x, y, max(2, min(w, video_w - x)), max(2, min(h, video_h - y))


def extract_strips(video, region: NumberRegion, interval: float = 1.0) -> np.ndarray:
    """The number corner of one frame per `interval` seconds, shape (frames, SCALE*height, SCALE*width), gray."""
    video_w, video_h = probe_size(video)
    x, y, w, h = frame_crop(region, video_w, video_h)
    out_w, out_h = SCALE * (region.x1 - region.x0), SCALE * (region.y1 - region.y0)
    # gray before crop: in a chroma-subsampled format ffmpeg rounds the crop to even sizes and shifts the corner
    cmd = ["ffmpeg", "-v", "error", "-i", str(video), "-an",
           "-vf", f"fps=1/{interval},format=gray,crop={w}:{h}:{x}:{y},scale={out_w}:{out_h}:flags=area,format=gray",
           "-f", "rawvideo", "-"]
    try:
        data = subprocess.run(cmd, capture_output=True, check=True).stdout
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode("utf-8", errors="replace") if e.stderr else ""
        raise RuntimeError(f"slide number extraction failed: {stderr[-500:]}") from e
    if not data:
        raise RuntimeError(f"no video frames extracted from {video}")
    return np.frombuffer(data, np.uint8).reshape(-1, out_h, out_w)


def numbers_from_strips(strips: np.ndarray, templates: np.ndarray, trust: float = 0.9,
                        tie: float = 0.03) -> NumberEvidence:
    """Pages whose corner matches the frame's corner. Frames whose corner matches no page well enough (blank, a
    different picture) name no page; pages scoring within `tie` of the best all stay candidates, because a deck may
    print the same number on several pages (build steps exported separately)."""
    scores = _normalise(strips.reshape(len(strips), -1)) @ templates.T
    top = scores.max(axis=1)
    trusted = top >= trust
    mask = trusted[:, None] & (scores >= (top - tie)[:, None])
    return NumberEvidence(mask, np.where(trusted, top, 0.0))

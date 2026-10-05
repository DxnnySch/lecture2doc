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
    try:
        data = subprocess.run(cmd, capture_output=True, check=True).stdout
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode("utf-8", errors="replace") if e.stderr else ""
        raise RuntimeError(f"frame extraction failed: {stderr[-500:]}") from e
    if not data:
        raise RuntimeError(f"no video frames extracted from {video}")
    return np.frombuffer(data, np.uint8).reshape(-1, FRAME_H, FRAME_W)


def _normalise(mat: np.ndarray) -> np.ndarray:
    """Zero-mean, unit-norm rows. Constant (blank) rows become all-zero, scoring 0 against everything."""
    mat = mat.astype(np.float32)
    mat -= mat.mean(axis=1, keepdims=True)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    return np.divide(mat, norms, out=np.zeros_like(mat), where=norms > 1e-6)


def probe_aspect(video) -> float:
    """Display aspect ratio (width / height) of the first video stream, via ffprobe."""
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
           "-of", "csv=p=0:s=x", str(video)]
    try:
        out = subprocess.run(cmd, capture_output=True, check=True).stdout.decode("utf-8", errors="replace")
        w, h = (int(v) for v in out.strip().split("x"))
        if w <= 0 or h <= 0:
            raise ValueError("non-positive size")
        return w / h
    except (subprocess.CalledProcessError, OSError, ValueError) as exc:
        raise RuntimeError(f"ffprobe could not read the video size of {video}: {exc}") from exc


def _letterbox(im: Image.Image, aspect: float) -> Image.Image:
    """Fit the image, aspect preserved and centred on black, into a canvas of the given aspect ratio."""
    w, h = im.size
    if abs(w / h - aspect) < 1e-3:
        return im
    if w / h > aspect:
        cw, ch = w, max(1, round(w / aspect))
    else:
        cw, ch = max(1, round(h * aspect)), h
    canvas = Image.new(im.mode, (cw, ch))
    canvas.paste(im, ((cw - w) // 2, (ch - h) // 2))
    return canvas


def slide_features(images: list[Image.Image], aspect: float = 16 / 9) -> np.ndarray:
    """Features of the slides as they appear in a video of the given aspect ratio (letterboxed/pillarboxed)."""
    rows = [
        np.asarray(_letterbox(im.convert("L"), aspect).resize((FRAME_W, FRAME_H), Image.BOX)).ravel()
        for im in images
    ]
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

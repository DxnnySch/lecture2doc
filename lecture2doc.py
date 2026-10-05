#!/usr/bin/env python3
"""Turn a lecture video + slide PDF/PPTX into a Word document with one transcript section per slide.

Usage:
    python lecture2doc.py VIDEO.mp4 SLIDES.pdf SLIDES.pptx [-o OUT.docx] [--no-cleanup] [--keep-raw]
    python lecture2doc.py VIDEO.mp4 SLIDES.pdf SLIDES.pptx --rebuild   # redo the .docx from a finished run's
                                                                        # segments.json: no ffmpeg, no API calls
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import numpy as np
import openai

from api import RateLimiter, make_client
from cleanup import LLMCache, build_glossary, build_outline, cleanup_spans
from audiocuts import CutReport, detect_silences, plan_cuts
from config import ConfigError, check_ffmpeg, load_config
from docx_out import SpanText, write_docx
from pagenumbers import (NumberEvidence, extract_strips, find_number_region, number_templates,
                         numbers_from_strips)
from segment import (Span, absorb_unmatched, build_spans, extract_frames, match_frames, probe_aspect,
                     slide_features, smooth, unmatched_seconds)
from slides import load_slides
from transcribe import transcribe_clips

log = logging.getLogger(__name__)


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
    p.add_argument("--group-by-slide", action="store_true",
                   help="one section per slide of the deck, in slide order, with all visits of a slide together "
                        "(default: one page per stretch of the lecture on one slide, in lecture order)")
    p.add_argument("--no-page-breaks", action="store_true",
                   help="do not start every slide on a new page (more compact document)")
    p.add_argument("--no-timestamps", action="store_true",
                   help="omit the video time range shown under each slide heading")
    p.add_argument("--rebuild", action="store_true",
                   help="rebuild the .docx from an existing segments.json (default: <workdir>/segments.json, or "
                        "--from-segments FILE) without video, ffmpeg, API key or any API calls")
    p.add_argument("--from-segments", help="reuse slide/time spans from a (hand-corrected) segments.json")
    p.add_argument("--interval", type=float, default=1.0, help="seconds between sampled frames")
    p.add_argument("--threshold", type=float, default=0.6, help="minimum match score for a frame to count")
    p.add_argument("--smooth", type=int, default=5, help="majority-vote window in frames")
    p.add_argument("--no-slide-numbers", action="store_true",
                   help="do not use the page number in the corner of the video to identify slides (by default it "
                        "is used when the PDF prints page numbers, so half-built animated slides are not mistaken "
                        "for other slides)")
    return p.parse_args(argv)


def _parse_segments(path, n_slides: int, need_text: bool = False) -> list[tuple[Span, str, str]]:
    """Validated (span, raw, clean) triples sorted by start; raw/clean are only checked when need_text."""
    name = f"segments file {path}"
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise SystemExit(f"cannot read {name}: {exc}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{name} is invalid JSON: {exc}") from None
    raw = data.get("spans") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        raise SystemExit(f"{name} must be an object with a 'spans' list")
    entries = []
    for i, d in enumerate(raw, 1):
        if not isinstance(d, dict):
            raise SystemExit(f"{name}: span {i} is not an object")
        for key in ("slide", "start", "end"):
            if key not in d:
                raise SystemExit(f"{name}: span {i} is missing '{key}'")
        try:
            slide, start, end = int(d["slide"]), float(d["start"]), float(d["end"])
            conf = float(d.get("confidence", 1.0))
        except (TypeError, ValueError):
            raise SystemExit(f"{name}: span {i} has a non-numeric slide/start/end/confidence") from None
        if end <= start:
            raise SystemExit(f"{name}: span {i} has end <= start ({end} <= {start})")
        if need_text:
            for key in ("raw", "clean"):
                if not isinstance(d.get(key), str):
                    raise SystemExit(f"{name}: span {i} needs a text field '{key}' (written by a full run)")
        entries.append((Span(slide, start, end, conf), d.get("raw", ""), d.get("clean", "")))
    bad = sorted({sp.slide for sp, _, _ in entries if not 1 <= sp.slide <= n_slides})
    if bad:
        raise SystemExit(f"{name} refers to slides {bad}, but the deck has slides 1-{n_slides}")
    return sorted(entries, key=lambda e: e[0].start)


def load_spans(path, n_slides: int) -> list[Span]:
    return [span for span, _, _ in _parse_segments(path, n_slides)]


def print_summary(spans: list[Span], n_slides: int) -> None:
    """Spans must be the ones before absorb_unmatched (unmatched = slide None)."""
    per_slide: dict[int, float] = {}
    for s in spans:
        if s.slide is not None:
            per_slide[s.slide] = per_slide.get(s.slide, 0.0) + (s.end - s.start)
    for n in sorted(per_slide):
        print(f"  slide {n:>2}:  {per_slide[n]:.0f}s matched")
    total = sum(s.end - s.start for s in spans) or 1.0
    unmatched = unmatched_seconds(spans)
    print(f"  unmatched: {unmatched:.0f}s of {total:.0f}s ({100 * unmatched / total:.0f}%)")
    missing = [n for n in range(1, n_slides + 1) if n not in per_slide]
    if missing:
        print(f"  slides never shown: {missing}")


def read_slide_numbers(pdf, video, interval: float, n_frames: int) -> NumberEvidence | None:
    """Which pages the number in the corner of each frame allows; None if the deck has no usable page numbers."""
    region = find_number_region(pdf)
    if region is None:
        print("  no page numbers found in the PDF; matching whole slides only")
        return None
    try:
        evidence = numbers_from_strips(extract_strips(video, region, interval), number_templates(pdf, region))
    except RuntimeError as exc:
        log.warning("cannot read the slide numbers (%s); matching whole slides only", exc)
        return None
    mask, score = evidence.mask[:n_frames], evidence.score[:n_frames]
    if len(mask) < n_frames:  # one frame fewer than the picture pass: no information for the rest
        mask = np.vstack([mask, np.zeros((n_frames - len(mask), mask.shape[1]), bool)])
        score = np.concatenate([score, np.zeros(n_frames - len(score))])
    return NumberEvidence(mask, score)


def print_cut_summary(report: CutReport) -> None:
    total = report.in_silence + report.at_video_change
    if not total:
        return
    if report.in_silence:
        gaps = sorted(report.gap_lengths)
        print(f"  {report.in_silence} of {total} slide changes cut in the silent gap between the recordings "
              f"(gap length median {gaps[len(gaps) // 2]:.1f}s, shortest {gaps[0]:.1f}s)")
    if report.at_video_change:
        print(f"  {report.at_video_change} slide changes without a silent gap: cut exactly at the detected change "
              f"(a word right at such a change may land on the neighbouring slide)")


def check_inputs(args) -> None:
    checks = [("PDF", args.pdf), ("PPTX", args.pptx)]
    if not args.rebuild:  # a rebuild never touches the video
        checks.insert(0, ("video", args.video))
    if args.from_segments:
        checks.append(("segments file", args.from_segments))
    for label, path in checks:
        if not Path(path).is_file():
            raise SystemExit(f"{label} not found: {path}")


def rebuild(args) -> int:
    """Re-render the .docx from a finished run's segments.json: offline, no video, no API."""
    video = Path(args.video)
    workdir = Path(args.workdir) if args.workdir else Path("output") / video.stem
    seg_path = Path(args.from_segments) if args.from_segments else workdir / "segments.json"
    if not seg_path.is_file():
        raise SystemExit(f"segments file not found: {seg_path} (run the full pipeline first, or pass --from-segments)")
    out = Path(args.output) if args.output else workdir / f"{video.stem}.docx"
    out.parent.mkdir(parents=True, exist_ok=True)
    print("Loading slides...")
    slides = load_slides(args.pdf, args.pptx)
    print(f"  {len(slides)} slides; rebuilding from {seg_path}")
    entries = _parse_segments(seg_path, len(slides), need_text=True)
    write_docx(out, video.stem, slides,
               [SpanText(sp.slide, sp.start, clean, raw, sp.end) for sp, raw, clean in entries],
               thumbnails=not args.no_thumbnails, keep_raw=args.keep_raw, timestamps=not args.no_timestamps,
               page_breaks=not args.no_page_breaks, group_by_slide=args.group_by_slide)
    print(f"Wrote {out}")
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    check_inputs(args)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    if args.rebuild:
        return rebuild(args)
    try:
        check_ffmpeg()
        cfg = load_config()
    except ConfigError as exc:
        raise SystemExit(str(exc)) from None
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
        print_summary(spans, len(slides))
    else:
        print("Matching video frames to slides...")
        try:
            aspect = probe_aspect(video)
        except RuntimeError as exc:
            log.warning("%s; assuming 16:9", exc)
            aspect = 16 / 9
        try:
            frames = extract_frames(video, args.interval)
            feats = slide_features([s.image for s in slides], aspect)
            numbers = None
            if not args.no_slide_numbers:
                numbers = read_slide_numbers(args.pdf, video, args.interval, len(frames))
            labels, conf = match_frames(frames, feats, args.threshold, numbers)
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from None
        if numbers is not None:
            picture_only, _ = match_frames(frames, feats, args.threshold)
            print(f"  slide number readable in {100 * numbers.mask.any(axis=1).mean():.0f}% of the frames; "
                  f"it changed the slide of {int((labels != picture_only).sum())} frames")
        spans = build_spans(smooth(labels, args.smooth), conf, args.interval)
        print_summary(spans, len(slides))
        try:
            spans = absorb_unmatched(spans)
        except ValueError as exc:
            raise SystemExit(str(exc)) from None

    print("Cutting the audio at the slide changes...")
    try:
        silences = detect_silences(video)
    except RuntimeError as exc:
        log.warning("%s; cutting exactly at the slide changes", exc)
        silences = []
    clips, cut_report = plan_cuts(spans, silences)
    print_cut_summary(cut_report)
    limiter = RateLimiter(cfg.requests_per_minute)  # one quota for the speech-to-text, glossary and cleanup requests
    print("Transcribing...")
    try:
        raws = transcribe_clips(cfg, video, clips, workdir / "clips.json", args.language, limiter=limiter)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from None
    except openai.APIError as exc:
        raise SystemExit(f"speech-to-text failed: {exc}") from None

    if args.no_cleanup:
        cleans = raws
    else:
        print("Cleaning up transcripts...")
        client = make_client(cfg)
        cache = LLMCache(workdir / "llm_cache.json")
        outline = build_outline(slides)
        glossary = build_glossary(client, cfg.glossary_model, slides, cache, limiter=limiter)
        cleans = cleanup_spans(client, cfg.llm_model, glossary, outline, slides, spans, raws, cache, limiter=limiter)

    (workdir / "segments.json").write_text(json.dumps({
        "video": str(video),
        "spans": [{"slide": s.slide, "start": s.start, "end": s.end, "confidence": round(s.confidence, 3),
                   "audio_start": round(k.start, 3), "audio_end": round(k.end, 3), "raw": r, "clean": c}
                  for s, k, r, c in zip(spans, clips, raws, cleans)],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    write_docx(out, video.stem, slides,
               [SpanText(s.slide, s.start, c, r, s.end) for s, r, c in zip(spans, raws, cleans)],
               thumbnails=not args.no_thumbnails, keep_raw=args.keep_raw, timestamps=not args.no_timestamps,
               page_breaks=not args.no_page_breaks, group_by_slide=args.group_by_slide)
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

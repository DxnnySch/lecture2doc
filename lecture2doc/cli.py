#!/usr/bin/env python3
"""Turn a lecture video + its slide PDF into a Word document with one transcript section per slide.

Usage:
    python -m lecture2doc                                   # open the browser UI
    python -m lecture2doc VIDEO.mp4 SLIDES.pdf [-o OUT.docx] [--no-cleanup] [--keep-raw]
    python -m lecture2doc VIDEO.mp4 SLIDES.pdf --rebuild    # redo the .docx from a finished run's
                                                            # segments.json: no ffmpeg, no API calls
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from lecture2doc.pipeline import Job, Options, PipelineError, rebuild, run


def parse_args(argv):
    p = argparse.ArgumentParser(prog="python -m lecture2doc", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("video")
    p.add_argument("pdf")
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


def job_from_args(args) -> Job:
    video = Path(args.video)
    workdir = Path(args.workdir) if args.workdir else Path("output") / video.stem
    output = Path(args.output) if args.output else workdir / f"{video.stem}.docx"
    options = Options(cleanup=not args.no_cleanup, keep_raw=args.keep_raw, thumbnails=not args.no_thumbnails,
                      timestamps=not args.no_timestamps, page_breaks=not args.no_page_breaks,
                      group_by_slide=args.group_by_slide, language=args.language, interval=args.interval,
                      threshold=args.threshold, smooth=args.smooth, slide_numbers=not args.no_slide_numbers)
    return Job(video, Path(args.pdf), workdir, output, options,
               Path(args.from_segments) if args.from_segments else None)


def main(argv=None) -> int:
    import sys

    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv == ["--ui"]:
        from lecture2doc.ui import serve

        return serve()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    args = parse_args(argv)
    try:
        path = (rebuild if args.rebuild else run)(job_from_args(args))
    except PipelineError as exc:
        raise SystemExit(str(exc)) from None
    print(f"Wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

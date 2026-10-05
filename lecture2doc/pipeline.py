"""The lecture -> Word document pipeline, independent of how it is started (command line or browser UI)."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import openai

from lecture2doc.api import RateLimiter, make_client
from lecture2doc.audiocuts import CutReport, detect_silences, plan_cuts
from lecture2doc.cleanup import LLMCache, build_glossary, build_outline, cleanup_spans
from lecture2doc.config import ConfigError, check_ffmpeg, load_config
from lecture2doc.docx_out import SpanText, write_docx
from lecture2doc.modelpick import llm_client
from lecture2doc.pagenumbers import (NumberEvidence, extract_strips, find_number_region, number_templates,
                                     numbers_from_strips)
from lecture2doc.segment import (Span, absorb_unmatched, build_spans, extract_frames, match_frames, probe_aspect,
                                 slide_features, smooth, unmatched_seconds)
from lecture2doc.slides import load_slides, pdf_pages
from lecture2doc.transcribe import transcribe_clips

log = logging.getLogger(__name__)

GLOSSARY_DIR = ".lecture2doc"
GLOSSARY_FILE = "glossary.txt"


class PipelineError(Exception):
    """A problem the user can act on (missing file, bad segments file, document open in Word, ...)."""


class Cancelled(Exception):
    """The user stopped the run."""


# (key, label shown to the user, weight in the overall progress)
STAGES = [
    ("slides", "Reading the slides", 3),
    ("matching", "Finding the slide shown in the video", 25),
    ("cutting", "Cutting the audio at the slide changes", 3),
    ("transcribing", "Transcribing the speech", 40),
    ("cleanup", "Cleaning up the text", 24),
    ("writing", "Writing the Word document", 3),
]
STAGE_LABELS = {key: label for key, label, _ in STAGES}
STAGE_WEIGHTS = {key: weight for key, _, weight in STAGES}


class Reporter:
    """Receives progress. `stage()` says which step is running and how far it is, `log()` carries remarks for the
    user. Subclasses override `emit` and `message`; `check` lets a UI stop a run between steps."""

    def __init__(self):
        self.active = [key for key, _, _ in STAGES]

    def plan(self, keys) -> None:
        """Which stages this run has (a run without cleanup or with given segments skips some)."""
        self.active = [k for k in self.active if k in keys]

    def stage(self, key: str, fraction: float | None = None, detail: str = "") -> None:
        self.check()
        done = sum(STAGE_WEIGHTS[k] for k in self.active[: self.active.index(key)]) if key in self.active else 0
        total = sum(STAGE_WEIGHTS[k] for k in self.active) or 1
        part = STAGE_WEIGHTS[key] * (fraction or 0.0) if key in self.active else 0
        self.emit(key, STAGE_LABELS[key], min(1.0, (done + part) / total), fraction, detail)

    def log(self, text: str) -> None:
        self.message(text)

    def finish(self) -> None:
        self.emit("done", "Done", 1.0, 1.0, "")

    def check(self) -> None:
        """Raise Cancelled when the user asked to stop."""

    def emit(self, key: str, label: str, overall: float, fraction: float | None, detail: str) -> None:
        pass

    def message(self, text: str) -> None:
        pass


class ConsoleReporter(Reporter):
    def __init__(self):
        super().__init__()
        self._last = None

    def emit(self, key, label, overall, fraction, detail):
        if key == "done":
            return
        if key != self._last:
            print(f"{label}...", flush=True)
            self._last = key
        if detail:
            print(f"  {detail}", flush=True)

    def message(self, text):
        print(f"  {text}", flush=True)


@dataclass
class Options:
    cleanup: bool = True
    keep_raw: bool = False
    thumbnails: bool = True
    timestamps: bool = True
    page_breaks: bool = True
    group_by_slide: bool = False
    language: str | None = None
    interval: float = 1.0
    threshold: float = 0.6
    smooth: int = 5
    slide_numbers: bool = True


@dataclass
class Job:
    video: Path
    pdf: Path
    workdir: Path
    output: Path
    options: Options = field(default_factory=Options)
    from_segments: Path | None = None

    @property
    def title(self) -> str:
        return self.video.stem


# --- glossary ---------------------------------------------------------------------------------------------------

def glossary_path(folder) -> Path:
    """One glossary per folder of slide PDFs: the terms and abbreviations of a course are used across its lectures."""
    return Path(folder) / GLOSSARY_DIR / GLOSSARY_FILE


def read_glossary(folder) -> str | None:
    """The saved (possibly hand-edited) glossary of a slides folder, one term per line; None if there is none."""
    try:
        return glossary_path(folder).read_text(encoding="utf-8")
    except OSError:
        return None


def save_glossary(folder, text: str) -> None:
    path = glossary_path(folder)
    path.parent.mkdir(parents=True, exist_ok=True)
    terms = [line.strip() for line in text.splitlines() if line.strip()]
    path.write_text("\n".join(terms) + ("\n" if terms else ""), encoding="utf-8")


def folder_slides(folder) -> list[SimpleNamespace]:
    """The text of every slide of every PDF in the folder (what the glossary is built from).

    A line that was already seen on an earlier slide (footers, repeated headings, agendas) is left out: the glossary
    only needs every term once, and a folder of decks is large."""
    slides, seen = [], set()
    for path in sorted(Path(folder).glob("*.pdf"), key=lambda p: p.name.lower()):
        try:
            pages = pdf_pages(path)
        except Exception as exc:  # an unreadable PDF must not block the others
            log.warning("skipping %s for the glossary (%s)", path.name, exc)
            continue
        for number, (title, text) in enumerate(pages, 1):
            lines = []
            for line in (raw.strip() for raw in text.splitlines()):
                if len(line) > 2 and line not in seen:  # page numbers and bullets alone say nothing
                    seen.add(line)
                    lines.append(line)
            slides.append(SimpleNamespace(number=number, title=f"{path.stem}: {title or 'untitled'}",
                                          text="\n".join(lines), deck=path.name))
    return slides


def make_glossary(folder, client=None, cfg=None, cache=None, limiter=None, on_progress=None) -> str:
    """Build the glossary from the slides of all PDFs in the folder and save it (that file is the cache).
    `on_progress(decks_done, decks_total, parts_done, parts_total)` reports how many of the PDFs have been covered
    and how many of the AI requests are finished."""
    if cfg is None:
        try:
            cfg = load_config()  # no ffmpeg needed for this
        except ConfigError as exc:
            raise PipelineError(str(exc)) from None
    slides = folder_slides(folder)
    if not slides:
        raise PipelineError(f"no readable PDF found in {folder}")
    total = len({s.deck for s in slides})
    if on_progress is not None:
        on_progress(0, total, 0, 0)
    text = build_glossary(client or _llm(cfg), cfg.glossary_model, slides, cache or LLMCache(None),
                          limiter=limiter or RateLimiter(cfg.requests_per_minute),
                          on_progress=(lambda done, n, decks: on_progress(decks, total, done, n))
                          if on_progress else None)
    save_glossary(folder, text)
    return text


def _llm(cfg, say=lambda text: None):
    """The client for the AI text requests; with the automatic model choice its speed ranking is measured first."""
    try:
        return llm_client(cfg, make_client(cfg), say)
    except (ValueError, RuntimeError) as exc:
        raise PipelineError(str(exc)) from None
    except openai.APIError as exc:
        raise PipelineError(f"the AI service is not reachable: {exc}") from None


def _config():
    try:
        check_ffmpeg()
        return load_config()
    except ConfigError as exc:
        raise PipelineError(str(exc)) from None


# --- segments file ----------------------------------------------------------------------------------------------

def parse_segments(path, n_slides: int, need_text: bool = False) -> list[tuple[Span, str, str]]:
    """Validated (span, raw, clean) triples sorted by start; raw/clean are only checked when need_text."""
    name = f"segments file {path}"
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise PipelineError(f"cannot read {name}: {exc}") from None
    except json.JSONDecodeError as exc:
        raise PipelineError(f"{name} is invalid JSON: {exc}") from None
    raw = data.get("spans") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        raise PipelineError(f"{name} must be an object with a 'spans' list")
    entries = []
    for i, d in enumerate(raw, 1):
        if not isinstance(d, dict):
            raise PipelineError(f"{name}: span {i} is not an object")
        for key in ("slide", "start", "end"):
            if key not in d:
                raise PipelineError(f"{name}: span {i} is missing '{key}'")
        try:
            slide, start, end = int(d["slide"]), float(d["start"]), float(d["end"])
            conf = float(d.get("confidence", 1.0))
        except (TypeError, ValueError):
            raise PipelineError(f"{name}: span {i} has a non-numeric slide/start/end/confidence") from None
        if end <= start:
            raise PipelineError(f"{name}: span {i} has end <= start ({end} <= {start})")
        if need_text:
            for key in ("raw", "clean"):
                if not isinstance(d.get(key), str):
                    raise PipelineError(f"{name}: span {i} needs a text field '{key}' (written by a full run)")
        entries.append((Span(slide, start, end, conf), d.get("raw", ""), d.get("clean", "")))
    bad = sorted({sp.slide for sp, _, _ in entries if not 1 <= sp.slide <= n_slides})
    if bad:
        raise PipelineError(f"{name} refers to slides {bad}, but the deck has slides 1-{n_slides}")
    return sorted(entries, key=lambda e: e[0].start)


def load_spans(path, n_slides: int) -> list[Span]:
    return [span for span, _, _ in parse_segments(path, n_slides)]


# --- the saved slide matching -----------------------------------------------------------------------------------

def _matching_meta(job: Job) -> dict:
    o = job.options
    return {"video": job.video.name, "size": job.video.stat().st_size, "pdf": job.pdf.name,
            "pdf_size": job.pdf.stat().st_size,
            "settings": [o.interval, o.threshold, o.smooth, o.slide_numbers]}


def load_matching(job: Job) -> list[Span] | None:
    """The slide/time spans of an earlier run on this very video and PDF with the same settings, else None."""
    try:
        data = json.loads((job.workdir / "matching.json").read_text(encoding="utf-8"))
        if data["meta"] != _matching_meta(job):
            return None
        return [Span(int(d["slide"]), float(d["start"]), float(d["end"]), float(d["confidence"]))
                for d in data["spans"]]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_matching(job: Job, spans: list[Span]) -> None:
    data = {"meta": _matching_meta(job),
            "spans": [{"slide": s.slide, "start": s.start, "end": s.end, "confidence": s.confidence} for s in spans]}
    try:
        (job.workdir / "matching.json").write_text(json.dumps(data), encoding="utf-8")
    except OSError as exc:  # only a speed-up
        log.warning("could not save the slide matching (%s); continuing without saving", exc)


# --- helpers ----------------------------------------------------------------------------------------------------

def match_summary(spans: list[Span], n_slides: int) -> list[str]:
    """Spans must be the ones before absorb_unmatched (unmatched = slide None)."""
    per_slide: dict[int, float] = {}
    for s in spans:
        if s.slide is not None:
            per_slide[s.slide] = per_slide.get(s.slide, 0.0) + (s.end - s.start)
    lines = [f"slide {n:>2}:  {per_slide[n]:.0f}s matched" for n in sorted(per_slide)]
    total = sum(s.end - s.start for s in spans) or 1.0
    unmatched = unmatched_seconds(spans)
    lines.append(f"unmatched: {unmatched:.0f}s of {total:.0f}s ({100 * unmatched / total:.0f}%)")
    missing = [n for n in range(1, n_slides + 1) if n not in per_slide]
    if missing:
        lines.append(f"slides never shown: {missing}")
    return lines


def read_slide_numbers(pdf, video, interval: float, n_frames: int, say) -> NumberEvidence | None:
    """Which pages the number in the corner of each frame allows; None if the deck has no usable page numbers."""
    region = find_number_region(pdf)
    if region is None:
        say("no page numbers found in the PDF; matching whole slides only")
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


def cut_summary(report: CutReport) -> list[str]:
    total = report.in_silence + report.at_video_change
    lines = []
    if total and report.in_silence:
        gaps = sorted(report.gap_lengths)
        lines.append(f"{report.in_silence} of {total} slide changes cut in the silent gap between the recordings "
                     f"(gap length median {gaps[len(gaps) // 2]:.1f}s, shortest {gaps[0]:.1f}s)")
    if report.at_video_change:
        lines.append(f"{report.at_video_change} slide changes without a silent gap: cut exactly at the detected "
                     f"change (a word right at such a change may land on the neighbouring slide)")
    return lines


def save_document(job: Job, slides, span_texts) -> None:
    o = job.options
    try:
        job.output.parent.mkdir(parents=True, exist_ok=True)
        write_docx(job.output, job.title, slides, span_texts, thumbnails=o.thumbnails, keep_raw=o.keep_raw,
                   timestamps=o.timestamps, page_breaks=o.page_breaks, group_by_slide=o.group_by_slide)
    except PermissionError:
        raise PipelineError(f"cannot write {job.output}: is it open in Word? Close it and run again (everything "
                            f"else is saved, nothing is transcribed again)") from None


def check_inputs(job: Job, rebuild: bool = False) -> None:
    checks = [("PDF", job.pdf)]
    if not rebuild:  # a rebuild never touches the video
        checks.insert(0, ("video", job.video))
    if job.from_segments:
        checks.append(("segments file", job.from_segments))
    for label, path in checks:
        if not Path(path).is_file():
            raise PipelineError(f"{label} not found: {path}")


# --- the runs ---------------------------------------------------------------------------------------------------

def rebuild(job: Job, reporter: Reporter | None = None) -> Path:
    """Re-render the .docx from a finished run's segments.json: offline, no video, no API."""
    reporter = reporter or ConsoleReporter()
    check_inputs(job, rebuild=True)
    seg_path = job.from_segments or job.workdir / "segments.json"
    if not seg_path.is_file():
        raise PipelineError(f"segments file not found: {seg_path} (run the full pipeline first)")
    reporter.plan(["slides", "writing"])
    reporter.stage("slides")
    slides = load_slides(job.pdf)
    reporter.log(f"{len(slides)} slides; rebuilding from {seg_path}")
    entries = parse_segments(seg_path, len(slides), need_text=True)
    reporter.stage("writing")
    save_document(job, slides, [SpanText(sp.slide, sp.start, clean, raw, sp.end) for sp, raw, clean in entries])
    reporter.finish()
    return job.output


def run(job: Job, reporter: Reporter | None = None) -> Path:
    reporter = reporter or ConsoleReporter()
    opts = job.options
    check_inputs(job)
    cfg = _config()  # also checks ffmpeg
    job.workdir.mkdir(parents=True, exist_ok=True)
    stages = ["slides", "matching", "cutting", "transcribing", "cleanup", "writing"]
    saved = None if job.from_segments else load_matching(job)
    if job.from_segments or saved is not None:
        stages.remove("matching")
    if not opts.cleanup:
        stages.remove("cleanup")
    reporter.plan(stages)

    reporter.stage("slides")
    slides = load_slides(job.pdf)
    reporter.log(f"{len(slides)} slides")

    if job.from_segments:
        spans = load_spans(job.from_segments, len(slides))
        for line in match_summary(spans, len(slides)):
            reporter.log(line)
    elif saved is not None:
        spans = saved
        reporter.log("using the saved slide matching of the earlier run")
    else:
        reporter.stage("matching", 0.0, "reading the video frames (the slowest step, minutes for a long lecture)")
        try:
            aspect = probe_aspect(job.video)
        except RuntimeError as exc:
            log.warning("%s; assuming 16:9", exc)
            aspect = 16 / 9
        try:
            frames = extract_frames(job.video, opts.interval)
            reporter.stage("matching", 0.5, "comparing the frames with the slides")
            feats = slide_features([s.image for s in slides], aspect)
            numbers = None
            if opts.slide_numbers:
                numbers = read_slide_numbers(job.pdf, job.video, opts.interval, len(frames), reporter.log)
            reporter.stage("matching", 0.9, "deciding which slide is shown when")
            labels, conf = match_frames(frames, feats, opts.threshold, numbers)
        except RuntimeError as exc:
            raise PipelineError(str(exc)) from None
        if numbers is not None:
            picture_only, _ = match_frames(frames, feats, opts.threshold)
            reporter.log(f"slide number readable in {100 * numbers.mask.any(axis=1).mean():.0f}% of the frames; "
                         f"it changed the slide of {int((labels != picture_only).sum())} frames")
        spans = build_spans(smooth(labels, opts.smooth), conf, opts.interval)
        for line in match_summary(spans, len(slides)):
            reporter.log(line)
        try:
            spans = absorb_unmatched(spans)
        except ValueError as exc:
            raise PipelineError(str(exc)) from None
        save_matching(job, spans)

    reporter.stage("cutting")
    try:
        silences = detect_silences(job.video)
    except RuntimeError as exc:
        log.warning("%s; cutting exactly at the slide changes", exc)
        silences = []
    clips, cut_report = plan_cuts(spans, silences)
    for line in cut_summary(cut_report):
        reporter.log(line)
    limiter = RateLimiter(cfg.requests_per_minute)  # one quota for the speech-to-text, glossary and cleanup requests

    reporter.stage("transcribing", 0.0)

    def stt_progress(done: int, total: int) -> None:
        reporter.stage("transcribing", done / total if total else 1.0, f"clip {done} of {total}")

    try:
        raws = transcribe_clips(cfg, job.video, clips, job.workdir / "clips.json", opts.language, limiter=limiter,
                                on_progress=stt_progress)
    except RuntimeError as exc:
        raise PipelineError(str(exc)) from None
    except openai.APIError as exc:
        raise PipelineError(f"speech-to-text failed: {exc}") from None

    if not opts.cleanup:
        cleans = raws
    else:
        reporter.stage("cleanup", 0.0, "choosing the fastest AI model")
        client = _llm(cfg, reporter.log)
        reporter.stage("cleanup", 0.0, "preparing the glossary")
        cache = LLMCache(job.workdir / "llm_cache.json")
        glossary = read_glossary(job.pdf.parent)
        if glossary is None:  # the first run builds it; edit it (in the UI or the file) and run again
            glossary = make_glossary(job.pdf.parent, client, cfg, cache, limiter)
        else:
            reporter.log("using the saved glossary")
        outline = build_outline(slides)

        def clean_progress(done: int, total: int) -> None:
            reporter.stage("cleanup", 0.05 + 0.95 * (done / total if total else 1.0), f"part {done} of {total}")

        cleans = cleanup_spans(client, cfg.llm_model, glossary, outline, slides, spans, raws, cache,
                               limiter=limiter, on_progress=clean_progress)

    if opts.cleanup and hasattr(client, "summary"):
        reporter.log("AI requests per model: " + client.summary())

    (job.workdir / "segments.json").write_text(json.dumps({
        "video": str(job.video),
        "spans": [{"slide": s.slide, "start": s.start, "end": s.end, "confidence": round(s.confidence, 3),
                   "audio_start": round(k.start, 3), "audio_end": round(k.end, 3), "raw": r, "clean": c}
                  for s, k, r, c in zip(spans, clips, raws, cleans)],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    reporter.stage("writing")
    save_document(job, slides, [SpanText(s.slide, s.start, c, r, s.end) for s, r, c in zip(spans, raws, cleans)])
    reporter.finish()
    return job.output

"""Where to cut the audio between slides.

The lecture audio is recorded slide by slide: at every slide change there is a silent gap between two recordings
(median 2.5 s against 0.2 s for ordinary pauses). Cutting the audio inside that gap and transcribing each slide's
audio separately puts every spoken word on exactly one slide, without relying on word timestamps (which the speech
to text service only estimates)."""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field

SILENCE_DB = -50       # below this level the audio counts as silent
MIN_SILENCE = 0.1      # seconds: shorter silences are not reported at all
MIN_GAP = 0.5          # seconds: only silences at least this long can be a gap between two recordings
WINDOW = 3.0           # seconds around a slide change in which its gap is looked for
MIN_CLIP = 0.5         # seconds: no clip may be shorter than this


@dataclass
class Clip:
    """The audio of one stretch on one slide: seconds into the video, and how many of them are not silent."""
    start: float
    end: float
    speech: float


@dataclass
class CutReport:
    in_silence: int = 0                 # slide changes cut in the middle of a silent gap
    at_video_change: int = 0            # slide changes without such a gap: cut exactly at the detected change
    gap_lengths: list[float] = field(default_factory=list)


def _duration(media) -> float:
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(media)]
    try:
        return float(subprocess.run(cmd, capture_output=True, check=True).stdout.decode().strip())
    except (subprocess.CalledProcessError, OSError, ValueError) as exc:
        raise RuntimeError(f"ffprobe could not read the duration of {media}: {exc}") from exc


def detect_silences(media, noise_db: float = SILENCE_DB, min_len: float = MIN_SILENCE) -> list[tuple[float, float]]:
    """(start, end) in seconds of every silence in the audio of `media`, in order."""
    cmd = ["ffmpeg", "-v", "info", "-i", str(media), "-vn", "-af", f"silencedetect=noise={noise_db}dB:d={min_len}",
           "-f", "null", "-"]
    result = subprocess.run(cmd, capture_output=True)
    text = result.stderr.decode("utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"silence detection failed: {text[-500:]}")
    silences, start = [], None
    for kind, value in re.findall(r"silence_(start|end): (-?[\d.]+)", text):
        if kind == "start":
            start = max(0.0, float(value))
        elif start is not None:
            silences.append((start, float(value)))
            start = None
    if start is not None:  # the recording ends in silence
        silences.append((start, _duration(media)))
    return silences


def _speech_seconds(start: float, end: float, silences) -> float:
    silent = sum(max(0.0, min(end, e) - max(start, s)) for s, e in silences)
    return max(0.0, (end - start) - silent)


def plan_cuts(spans, silences, window: float = WINDOW, min_gap: float = MIN_GAP,
              min_clip: float = MIN_CLIP) -> tuple[list[Clip], CutReport]:
    """One clip per span. Each slide change is cut in the middle of the longest silent gap within `window` seconds
    of it; without such a gap, or if that would squeeze a neighbouring clip, at the detected slide change."""
    report = CutReport()
    boundaries = [s.end for s in spans[:-1]]
    first, last = spans[0].start, spans[-1].end
    cuts: list[float] = []
    for k, boundary in enumerate(boundaries):
        lower = (cuts[-1] if cuts else first) + min_clip
        upper = (boundaries[k + 1] if k + 1 < len(boundaries) else last) - min_clip
        near = [(s, e) for s, e in silences
                if e - s >= min_gap and e >= boundary - window and s <= boundary + window
                and lower <= (s + e) / 2 <= upper]  # a gap that is already used up, or too close to the next change, is no candidate
        gap = max(near, key=lambda g: (g[1] - g[0], -abs((g[0] + g[1]) / 2 - boundary)), default=None)
        if gap is not None:
            cuts.append((gap[0] + gap[1]) / 2)
            report.in_silence += 1
            report.gap_lengths.append(gap[1] - gap[0])
        else:
            cuts.append(max(boundary, lower))
            report.at_video_change += 1
    edges = [first, *cuts, last]
    clips = [Clip(a, b, _speech_seconds(a, b, silences)) for a, b in zip(edges, edges[1:])]
    return clips, report

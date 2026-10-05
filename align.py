"""Move the text cut between two slides onto a sentence boundary.

A lecturer usually clicks to the next slide a moment before or after finishing a sentence, so cutting the transcript
at the exact click time puts the last words of a sentence on the wrong slide. For every cut that falls inside a
sentence we move it to the nearer edge of that sentence (within a tolerance), keeping the slides' on-screen times
untouched: only the words-to-slide assignment changes.
"""
from __future__ import annotations

from dataclasses import replace

SENTENCE_END = (".", "?", "!")
CLOSERS = "\"'”’»)]}"
ABBREVIATIONS = {
    "e.g.", "i.e.", "etc.", "vs.", "approx.", "dr.", "mr.", "mrs.", "ms.", "prof.", "fig.", "no.", "nr.", "ca.",
    "z.b.", "d.h.", "u.a.", "bzw.", "usw.", "vgl.", "evtl.", "inkl.",
}


def _ends_sentence(word) -> bool:
    token = word.w.rstrip(CLOSERS)
    return token.endswith(SENTENCE_END) and token.lower() not in ABBREVIATIONS


def _pause_cut(before, after) -> float:
    """A cut position in the pause between two neighbouring words (right after `before` if they touch)."""
    if after is None:
        return before.end + 1e-3
    return (before.end + max(after.start, before.end)) / 2


def _candidates(words, terminal, cut: float, max_shift: float) -> list[float]:
    """Cut positions at the two edges of the sentence containing `cut`, nearest first, within max_shift."""
    n = len(words)
    left = sum(1 for w in words if (w.start + w.end) / 2 < cut)  # words assigned to the left slide
    if left == 0 or left == n or terminal[left - 1]:
        return []  # nothing on one side, or the cut already sits between two sentences
    first = left - 1
    while first > 0 and not terminal[first - 1]:
        first -= 1
    last = left
    while last < n and not terminal[last]:
        last += 1
    found = []
    if last < n:  # forward: finish the sentence on this slide
        found.append((words[last].end - cut, 0, _pause_cut(words[last], words[last + 1] if last + 1 < n else None)))
    if first > 0:  # backward: the sentence belongs to the next slide
        found.append((cut - words[first].start, 1, _pause_cut(words[first - 1], words[first])))
    return [c for dist, _, c in sorted(found) if dist <= max_shift]


def snap_to_sentences(words, spans, max_shift: float = 4.0, min_span: float = 1.0):
    """Spans (contiguous, ordered) with every internal cut moved to a sentence boundary where one is near enough.

    Returned spans keep slide and confidence; only the inner `end`/`start` values change, and both sides of a cut
    stay equal. A move is skipped if it would leave a span shorter than `min_span` seconds."""
    if max_shift <= 0 or len(spans) < 2 or not words:
        return spans
    terminal = [_ends_sentence(w) for w in words]
    cuts = [s.end for s in spans[:-1]]
    new_cuts: list[float] = []
    for k, cut in enumerate(cuts):
        lower = (new_cuts[-1] if new_cuts else spans[0].start) + min_span
        upper = (cuts[k + 1] if k + 1 < len(cuts) else spans[-1].end) - min_span
        valid = [c for c in _candidates(words, terminal, cut, max_shift) if lower <= c <= upper]
        new_cuts.append(valid[0] if valid else cut)
    if new_cuts == cuts:
        return spans
    out = []
    for k, span in enumerate(spans):
        start = new_cuts[k - 1] if k > 0 else span.start
        end = new_cuts[k] if k < len(new_cuts) else span.end
        out.append(replace(span, start=start, end=end))
    return out

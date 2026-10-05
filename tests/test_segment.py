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

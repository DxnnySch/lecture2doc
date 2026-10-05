import json
import re

import pytest
from docx import Document

import lecture2doc
from helpers import make_pdf, make_pptx, make_video
from slides import render_pdf
from transcribe import Word


def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("SCADS_API_KEY", "dummy")
    pdf = make_pdf(tmp_path / "s.pdf", 3)
    pptx = make_pptx(tmp_path / "s.pptx", [("Alpha", "a", False), ("Beta", "b", False), ("Gamma", "c", False)])
    images, _ = render_pdf(pdf)
    video = make_video(images, [0] * 4 + [1] * 4 + [0] * 4, tmp_path / "lec.mp4")
    words = [Word("hello", 1.0, 1.5), Word("world", 5.0, 5.5), Word("again", 9.0, 9.5)]
    monkeypatch.setattr(lecture2doc, "transcribe_words", lambda *a, **k: words)
    return video, pdf, pptx


def test_end_to_end_no_cleanup(tmp_path, monkeypatch):
    video, pdf, pptx = setup(tmp_path, monkeypatch)
    out = tmp_path / "out" / "lec.docx"
    assert lecture2doc.main([str(video), str(pdf), str(pptx), "-o", str(out), "--workdir", str(tmp_path / "w"),
                             "--no-cleanup"]) == 0
    texts = [(p.style.name, p.text) for p in Document(out).paragraphs]
    assert ("Heading 1", "Slide 1 – Alpha") in texts and ("Heading 1", "Slide 3 – Gamma") in texts
    assert ("Normal", "[00:00] hello") in texts
    # the revisit start time may differ by +-1 s from 8
    assert any(style == "Normal" and re.fullmatch(r"\[\d\d:\d\d\] again", t) for style, t in texts)
    assert ("Normal", "world") in texts
    assert ("Normal", "(no speech)") in texts  # slide 3 never shown
    seg = json.loads((tmp_path / "w" / "segments.json").read_text(encoding="utf-8"))
    assert [s["slide"] for s in seg["spans"]] == [1, 2, 1]
    assert "dummy" not in json.dumps(seg)


def test_from_segments_skips_matching(tmp_path, monkeypatch):
    video, pdf, pptx = setup(tmp_path, monkeypatch)
    seg = tmp_path / "seg.json"
    seg.write_text(json.dumps({"spans": [{"slide": 3, "start": 0, "end": 12, "confidence": 1.0}]}))
    monkeypatch.setattr(lecture2doc, "extract_frames", lambda *a, **k: (_ for _ in ()).throw(AssertionError("matched")))
    out = tmp_path / "o.docx"
    lecture2doc.main([str(video), str(pdf), str(pptx), "-o", str(out), "--workdir", str(tmp_path / "w"),
                      "--no-cleanup", "--from-segments", str(seg)])
    texts = [p.text for p in Document(out).paragraphs]
    assert "hello world again" in texts


def test_from_segments_rejects_unknown_slide(tmp_path, monkeypatch):
    video, pdf, pptx = setup(tmp_path, monkeypatch)
    seg = tmp_path / "seg.json"
    seg.write_text(json.dumps({"spans": [{"slide": 99, "start": 0, "end": 12, "confidence": 1.0}]}))
    with pytest.raises(SystemExit, match="99"):
        lecture2doc.main([str(video), str(pdf), str(pptx), "-o", str(tmp_path / "o.docx"),
                          "--workdir", str(tmp_path / "w"), "--no-cleanup", "--from-segments", str(seg)])

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


def run(video, pdf, pptx, tmp_path, *extra, out=None):
    out = out or tmp_path / "o.docx"
    rc = lecture2doc.main([str(video), str(pdf), str(pptx), "-o", str(out), "--workdir", str(tmp_path / "w"), *extra])
    assert rc == 0
    return out


def test_missing_video_fails_fast(tmp_path, monkeypatch):
    video, pdf, pptx = setup(tmp_path, monkeypatch)
    with pytest.raises(SystemExit, match="video not found"):
        run(tmp_path / "nope.mp4", pdf, pptx, tmp_path, "--no-cleanup")


def test_missing_pdf_fails_fast(tmp_path, monkeypatch):
    video, pdf, pptx = setup(tmp_path, monkeypatch)
    with pytest.raises(SystemExit, match="PDF not found"):
        run(video, tmp_path / "nope.pdf", pptx, tmp_path, "--no-cleanup")


def test_missing_segments_file_fails_fast(tmp_path, monkeypatch):
    video, pdf, pptx = setup(tmp_path, monkeypatch)
    with pytest.raises(SystemExit, match="segments"):
        run(video, pdf, pptx, tmp_path, "--no-cleanup", "--from-segments", str(tmp_path / "nope.json"))


def test_from_segments_unsorted_matches_sorted(tmp_path, monkeypatch):
    video, pdf, pptx = setup(tmp_path, monkeypatch)
    spans = [{"slide": 1, "start": 0, "end": 4, "confidence": 1.0},
             {"slide": 2, "start": 4, "end": 8, "confidence": 1.0},
             {"slide": 3, "start": 8, "end": 12, "confidence": 1.0}]
    results = []
    for name, order in (("sorted", spans), ("unsorted", spans[::-1])):
        seg = tmp_path / f"{name}.json"
        seg.write_text(json.dumps({"spans": order}))
        out = run(video, pdf, pptx, tmp_path, "--no-cleanup", "--from-segments", str(seg),
                  out=tmp_path / f"{name}.docx")
        results.append([(p.style.name, p.text) for p in Document(out).paragraphs])
    assert results[0] == results[1]
    assert [("Normal", w) for w in ("hello", "world", "again")] == [x for x in results[1] if x[1] in ("hello", "world", "again")]


@pytest.mark.parametrize("content, message", [
    ("{not json", "invalid JSON"),
    (json.dumps({"nospans": []}), "spans"),
    (json.dumps({"spans": "x"}), "spans"),
    (json.dumps({"spans": [{"slide": 1, "start": 0}]}), "end"),
    (json.dumps({"spans": [{"slide": 1, "start": "a", "end": 3}]}), "numeric"),
    (json.dumps({"spans": [{"slide": 1, "start": 5, "end": 5}]}), "end"),
    (json.dumps({"spans": [{"slide": 1, "start": 5, "end": 2}]}), "end"),
])
def test_from_segments_malformed(tmp_path, monkeypatch, content, message):
    video, pdf, pptx = setup(tmp_path, monkeypatch)
    seg = tmp_path / "seg.json"
    seg.write_text(content)
    with pytest.raises(SystemExit, match=message):
        run(video, pdf, pptx, tmp_path, "--no-cleanup", "--from-segments", str(seg))


def test_summary_per_slide(tmp_path, monkeypatch, capsys):
    video, pdf, pptx = setup(tmp_path, monkeypatch)
    run(video, pdf, pptx, tmp_path, "--no-cleanup")
    out = capsys.readouterr().out
    assert "slide  1:" in out and "slide  2:" in out
    assert "slide  3:" not in out
    assert "slides never shown: [3]" in out
    assert "unmatched" in out


def test_summary_for_from_segments(tmp_path, monkeypatch, capsys):
    video, pdf, pptx = setup(tmp_path, monkeypatch)
    seg = tmp_path / "seg.json"
    seg.write_text(json.dumps({"spans": [{"slide": 3, "start": 0, "end": 12, "confidence": 1.0}]}))
    run(video, pdf, pptx, tmp_path, "--no-cleanup", "--from-segments", str(seg))
    out = capsys.readouterr().out
    assert "slide  3:  12s matched" in out
    assert "slides never shown: [1, 2]" in out


def test_transcribe_runtime_error_exits(tmp_path, monkeypatch):
    video, pdf, pptx = setup(tmp_path, monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("no audio stream")

    monkeypatch.setattr(lecture2doc, "transcribe_words", boom)
    with pytest.raises(SystemExit, match="no audio stream"):
        run(video, pdf, pptx, tmp_path, "--no-cleanup")


def test_cleanup_wiring(tmp_path, monkeypatch):
    video, pdf, pptx = setup(tmp_path, monkeypatch)
    monkeypatch.setenv("SCADS_LLM_MODEL", "llm-x")
    monkeypatch.setenv("SCADS_GLOSSARY_MODEL", "glos-y")
    dummy = object()
    calls = {}
    monkeypatch.setattr(lecture2doc, "make_client", lambda cfg: dummy)

    def fake_glossary(client, model, slides, cache):
        calls["glossary"] = (client, model)
        return "GLOSS"

    def fake_cleanup(client, model, glossary, outline, slides, spans, raws, cache):
        calls["cleanup"] = (client, model, glossary)
        return [f"CLEAN<{r}>" for r in raws]

    monkeypatch.setattr(lecture2doc, "build_glossary", fake_glossary)
    monkeypatch.setattr(lecture2doc, "cleanup_spans", fake_cleanup)
    out = run(video, pdf, pptx, tmp_path)
    assert calls["glossary"] == (dummy, "glos-y")
    assert calls["cleanup"] == (dummy, "llm-x", "GLOSS")
    texts = [p.text for p in Document(out).paragraphs]
    assert "CLEAN<world>" in texts and "world" not in texts
    seg = json.loads((tmp_path / "w" / "segments.json").read_text(encoding="utf-8"))
    assert seg["spans"][1]["raw"] == "world"
    assert seg["spans"][1]["clean"] == "CLEAN<world>"


def test_keep_raw(tmp_path, monkeypatch):
    video, pdf, pptx = setup(tmp_path, monkeypatch)
    out = run(video, pdf, pptx, tmp_path, "--no-cleanup", "--keep-raw")
    assert "Raw: world" in [p.text for p in Document(out).paragraphs]


def test_thumbnails_toggle(tmp_path, monkeypatch):
    video, pdf, pptx = setup(tmp_path, monkeypatch)
    with_thumbs = run(video, pdf, pptx, tmp_path, "--no-cleanup", out=tmp_path / "a.docx")
    without = run(video, pdf, pptx, tmp_path, "--no-cleanup", "--no-thumbnails", out=tmp_path / "b.docx")
    assert len(Document(with_thumbs).inline_shapes) >= 1
    assert len(Document(without).inline_shapes) == 0


def test_probe_failure_falls_back_to_16_9_with_warning(tmp_path, monkeypatch, caplog):
    video, pdf, pptx = setup(tmp_path, monkeypatch)

    def bad_probe(v):
        raise RuntimeError("ffprobe could not read the video size")

    monkeypatch.setattr(lecture2doc, "probe_aspect", bad_probe)
    run(video, pdf, pptx, tmp_path, "--no-cleanup")
    assert "assuming 16:9" in caplog.text

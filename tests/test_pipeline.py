import json
import re

import pytest
from docx import Document

from lecture2doc import cli as lecture2doc
from lecture2doc.api import RateLimiter
from helpers import animation_state, make_pdf, make_pptx, make_video, make_wav
from lecture2doc.slides import render_pdf


def fake_stt(texts):
    """Stands in for the speech to text: the i-th clip gets texts[i]; the clips it was asked for are recorded."""
    def stt(cfg, video, clips, *args, **kwargs):
        stt.clips = clips
        return [texts[i] if i < len(texts) else "" for i in range(len(clips))]
    return stt


def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("SCADS_API_KEY", "dummy")
    pdf = make_pdf(tmp_path / "s.pdf", 3)
    pptx = make_pptx(tmp_path / "s.pptx", [("Alpha", "a", False), ("Beta", "b", False), ("Gamma", "c", False)])
    images, _ = render_pdf(pdf)
    video = make_video(images, [0] * 4 + [1] * 4 + [0] * 4, tmp_path / "lec.mp4")
    monkeypatch.setattr(lecture2doc, "transcribe_clips", fake_stt(["hello", "world", "again"]))
    return video, pdf, pptx


def test_end_to_end_no_cleanup(tmp_path, monkeypatch):
    video, pdf, pptx = setup(tmp_path, monkeypatch)
    out = tmp_path / "out" / "lec.docx"
    assert lecture2doc.main([str(video), str(pdf), str(pptx), "-o", str(out), "--workdir", str(tmp_path / "w"),
                             "--no-cleanup"]) == 0
    texts = [(p.style.name, p.text) for p in Document(out).paragraphs]
    # one page per stretch of the lecture, in lecture order: Alpha, Beta, Alpha again; Gamma was never shown
    assert [t for style, t in texts if style == "Heading 1"] == ["Slide 1 – Alpha", "Slide 2 – Beta", "Slide 1 – Alpha"]
    assert [t for style, t in texts if style == "Normal" and t] == ["hello", "world", "again"]
    assert ("Normal", "(no speech)") not in texts
    seg = json.loads((tmp_path / "w" / "segments.json").read_text(encoding="utf-8"))
    assert [s["slide"] for s in seg["spans"]] == [1, 2, 1]
    assert "dummy" not in json.dumps(seg)


def test_end_to_end_group_by_slide(tmp_path, monkeypatch):
    video, pdf, pptx = setup(tmp_path, monkeypatch)
    out = run(video, pdf, pptx, tmp_path, "--no-cleanup", "--group-by-slide")
    texts = [(p.style.name, p.text) for p in Document(out).paragraphs]
    assert ("Heading 1", "Slide 1 – Alpha") in texts and ("Heading 1", "Slide 3 – Gamma") in texts
    assert ("Normal", "[00:00] hello") in texts
    # the revisit start time may differ by +-1 s from 8
    assert any(style == "Normal" and re.fullmatch(r"\[\d\d:\d\d\] again", t) for style, t in texts)
    assert ("Normal", "world") in texts
    assert ("Normal", "(no speech)") in texts  # slide 3 never shown


def test_from_segments_skips_matching(tmp_path, monkeypatch):
    video, pdf, pptx = setup(tmp_path, monkeypatch)
    seg = tmp_path / "seg.json"
    seg.write_text(json.dumps({"spans": [{"slide": 3, "start": 0, "end": 12, "confidence": 1.0}]}))
    monkeypatch.setattr(lecture2doc, "extract_frames", lambda *a, **k: (_ for _ in ()).throw(AssertionError("matched")))
    out = tmp_path / "o.docx"
    lecture2doc.main([str(video), str(pdf), str(pptx), "-o", str(out), "--workdir", str(tmp_path / "w"),
                      "--no-cleanup", "--from-segments", str(seg)])
    texts = [p.text for p in Document(out).paragraphs]
    assert "hello" in texts  # one span, one clip


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

    monkeypatch.setattr(lecture2doc, "transcribe_clips", boom)
    with pytest.raises(SystemExit, match="no audio stream"):
        run(video, pdf, pptx, tmp_path, "--no-cleanup")


def test_cleanup_wiring(tmp_path, monkeypatch):
    video, pdf, pptx = setup(tmp_path, monkeypatch)
    monkeypatch.setenv("SCADS_LLM_MODEL", "llm-x")
    monkeypatch.setenv("SCADS_GLOSSARY_MODEL", "glos-y")
    monkeypatch.setenv("SCADS_REQUESTS_PER_MINUTE", "7")
    dummy = object()
    calls = {}
    monkeypatch.setattr(lecture2doc, "make_client", lambda cfg: dummy)

    def fake_glossary(client, model, slides, cache, limiter=None):
        calls["glossary"] = (client, model)
        calls["glossary_limiter"] = limiter
        return "GLOSS"

    def fake_cleanup(client, model, glossary, outline, slides, spans, raws, cache, limiter=None):
        calls["cleanup"] = (client, model, glossary)
        calls["cleanup_limiter"] = limiter
        return [f"CLEAN<{r}>" for r in raws]

    monkeypatch.setattr(lecture2doc, "build_glossary", fake_glossary)
    monkeypatch.setattr(lecture2doc, "cleanup_spans", fake_cleanup)
    out = run(video, pdf, pptx, tmp_path)
    assert calls["glossary"] == (dummy, "glos-y")
    assert calls["cleanup"] == (dummy, "llm-x", "GLOSS")
    assert isinstance(calls["cleanup_limiter"], RateLimiter) and calls["cleanup_limiter"].max_calls == 7
    assert calls["glossary_limiter"] is calls["cleanup_limiter"]  # one shared quota for both stages
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


def test_frame_extraction_error_exits(tmp_path, monkeypatch):
    video, pdf, pptx = setup(tmp_path, monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("no video frames extracted from lec.mp4")

    monkeypatch.setattr(lecture2doc, "extract_frames", boom)
    with pytest.raises(SystemExit, match="no video frames extracted"):
        run(video, pdf, pptx, tmp_path, "--no-cleanup")


def test_transcribe_api_error_exits_without_traceback_or_key(tmp_path, monkeypatch):
    import httpx2
    import openai

    video, pdf, pptx = setup(tmp_path, monkeypatch)

    def denied(*a, **k):
        raise openai.AuthenticationError(
            "invalid credentials",
            response=httpx2.Response(401, request=httpx2.Request("POST", "http://x")), body=None)

    monkeypatch.setattr(lecture2doc, "transcribe_clips", denied)
    with pytest.raises(SystemExit) as exc_info:
        run(video, pdf, pptx, tmp_path, "--no-cleanup")
    msg = str(exc_info.value)
    assert "speech-to-text failed" in msg
    assert "invalid credentials" in msg
    assert "dummy" not in msg


def subtitle_lines(path):
    return [p.text for p in Document(path).paragraphs if p.style.name == "Subtitle"]


def non_subtitle(path):
    return [(p.style.name, p.text) for p in Document(path).paragraphs if p.style.name != "Subtitle"]


def test_normal_run_adds_time_subtitles(tmp_path, monkeypatch):
    video, pdf, pptx = setup(tmp_path, monkeypatch)
    out = run(video, pdf, pptx, tmp_path, "--no-cleanup")
    lines = subtitle_lines(out)
    assert len(lines) == 3  # one per page: slide 1, slide 2, slide 1 again; slide 3 never shown
    assert all(re.fullmatch(r"\d\d:\d\d – \d\d:\d\d", line) for line in lines)
    grouped = subtitle_lines(run(video, pdf, pptx, tmp_path, "--no-cleanup", "--group-by-slide",
                                 out=tmp_path / "grouped.docx"))
    assert len(grouped) == 2  # slide 1 (two visits) and slide 2
    assert re.fullmatch(r"\d\d:\d\d – \d\d:\d\d · \d\d:\d\d – \d\d:\d\d", grouped[0])
    assert re.fullmatch(r"\d\d:\d\d – \d\d:\d\d", grouped[1])
    assert subtitle_lines(run(video, pdf, pptx, tmp_path, "--no-cleanup", "--no-timestamps",
                              out=tmp_path / "plain.docx")) == []


def forbid_heavy_work(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("heavy work during --rebuild")
    for name in ("transcribe_clips", "detect_silences", "extract_frames", "match_frames", "probe_aspect", "load_config",
                 "check_ffmpeg", "make_client", "build_glossary", "cleanup_spans"):
        monkeypatch.setattr(lecture2doc, name, boom)


def test_rebuild_adds_subtitles_offline_without_video_or_key(tmp_path, monkeypatch):
    video, pdf, pptx = setup(tmp_path, monkeypatch)
    old = run(video, pdf, pptx, tmp_path, "--no-cleanup", "--no-timestamps", out=tmp_path / "old.docx")
    assert subtitle_lines(old) == []
    video.unlink()  # the video is not needed any more
    forbid_heavy_work(monkeypatch)
    new = tmp_path / "new.docx"
    rc = lecture2doc.main([str(video), str(pdf), str(pptx), "-o", str(new), "--workdir", str(tmp_path / "w"),
                           "--rebuild"])
    assert rc == 0
    assert len(subtitle_lines(new)) == 3
    assert non_subtitle(new) == non_subtitle(old)  # identical text, only the time lines were added


def test_rebuild_uses_cleaned_text_and_keeps_segments_file_untouched(tmp_path, monkeypatch):
    video, pdf, pptx = setup(tmp_path, monkeypatch)
    run(video, pdf, pptx, tmp_path, "--no-cleanup")
    segfile = tmp_path / "w" / "segments.json"
    data = json.loads(segfile.read_text(encoding="utf-8"))
    data["spans"][1]["clean"] = "World, corrected."
    segfile.write_text(json.dumps(data), encoding="utf-8")
    before = segfile.read_text(encoding="utf-8")
    forbid_heavy_work(monkeypatch)
    out = tmp_path / "r.docx"
    lecture2doc.main([str(video), str(pdf), str(pptx), "-o", str(out), "--workdir", str(tmp_path / "w"),
                      "--rebuild", "--keep-raw"])
    texts = [p.text for p in Document(out).paragraphs]
    assert "World, corrected." in texts and "Raw: world" in texts
    assert segfile.read_text(encoding="utf-8") == before


def test_rebuild_from_explicit_segments_file(tmp_path, monkeypatch):
    video, pdf, pptx = setup(tmp_path, monkeypatch)
    seg = tmp_path / "elsewhere.json"
    seg.write_text(json.dumps({"spans": [{"slide": 2, "start": 5, "end": 65, "raw": "r", "clean": "Cleaned."}]}),
                   encoding="utf-8")
    forbid_heavy_work(monkeypatch)
    out = tmp_path / "e.docx"
    lecture2doc.main([str(video), str(pdf), str(pptx), "-o", str(out), "--workdir", str(tmp_path / "w2"),
                      "--rebuild", "--from-segments", str(seg)])
    assert subtitle_lines(out) == ["00:05 – 01:05"]


@pytest.mark.parametrize("content, message", [
    (None, "segments file"),                                                   # no file at all
    ({"spans": [{"slide": 1, "start": 0, "end": 5, "raw": "r"}]}, "'clean'"),  # old/hand-made file
    ({"spans": [{"slide": 1, "start": 0, "end": 5, "clean": 3, "raw": "r"}]}, "'clean'"),
])
def test_rebuild_requires_a_complete_segments_file(tmp_path, monkeypatch, content, message):
    video, pdf, pptx = setup(tmp_path, monkeypatch)
    if content is not None:
        (tmp_path / "w").mkdir()
        (tmp_path / "w" / "segments.json").write_text(json.dumps(content), encoding="utf-8")
    forbid_heavy_work(monkeypatch)
    with pytest.raises(SystemExit, match=message):
        lecture2doc.main([str(video), str(pdf), str(pptx), "-o", str(tmp_path / "x.docx"),
                          "--workdir", str(tmp_path / "w"), "--rebuild"])


def test_page_breaks_by_default_and_no_page_breaks_option(tmp_path, monkeypatch):
    video, pdf, pptx = setup(tmp_path, monkeypatch)

    def flags(path):
        return [p.paragraph_format.page_break_before for p in Document(path).paragraphs if p.style.name == "Heading 1"]

    default = run(video, pdf, pptx, tmp_path, "--no-cleanup")
    assert flags(default) == [None, True, True]
    compact = run(video, pdf, pptx, tmp_path, "--no-cleanup", "--no-page-breaks", out=tmp_path / "compact.docx")
    assert flags(compact) == [None, None, None]
    forbid_heavy_work(monkeypatch)  # --rebuild honours the option too, offline
    rebuilt = tmp_path / "rebuilt.docx"
    lecture2doc.main([str(video), str(pdf), str(pptx), "-o", str(rebuilt), "--workdir", str(tmp_path / "w"),
                      "--rebuild", "--no-page-breaks"])
    assert flags(rebuilt) == [None, None, None]


def silent_gap_setup(tmp_path, monkeypatch):
    """The slides change at 4 s and 8 s, in the middle of the silent gaps 3-5 s and 7-9 s of the recording."""
    monkeypatch.setenv("SCADS_API_KEY", "dummy")
    pdf = make_pdf(tmp_path / "s.pdf", 3)
    pptx = make_pptx(tmp_path / "s.pptx", [("Alpha", "a", False), ("Beta", "b", False), ("Gamma", "c", False)])
    images, _ = render_pdf(pdf)
    wav = make_wav(tmp_path / "a.wav", [("tone", 3.0), ("silence", 2.0), ("tone", 2.0), ("silence", 2.0),
                                        ("tone", 3.0)])
    video = make_video(images, [0] * 4 + [1] * 4 + [0] * 4, tmp_path / "gaps.mp4", audio=wav)
    stt = fake_stt(["hello", "world", "again"])
    monkeypatch.setattr(lecture2doc, "transcribe_clips", stt)
    return video, pdf, pptx, stt


def test_audio_is_cut_in_the_silent_gap_at_every_slide_change(tmp_path, monkeypatch, capsys):
    video, pdf, pptx, stt = silent_gap_setup(tmp_path, monkeypatch)
    out = run(video, pdf, pptx, tmp_path, "--no-cleanup")
    # cut in the middle of the gaps 3-5 s and 7-9 s (at 4 s and 8 s); each clip keeps 0.25 s of the silence
    assert [t for c in stt.clips for t in (c.start, c.end)] == pytest.approx([0.0, 3.25, 4.75, 7.25, 8.75, 12.0], abs=0.1)
    assert [round(c.speech) for c in stt.clips] == [3, 2, 3]
    assert "2 of 2 slide changes cut in the silent gap" in capsys.readouterr().out
    seg = json.loads((tmp_path / "w" / "segments.json").read_text(encoding="utf-8"))
    assert [t for s in seg["spans"] for t in (s["audio_start"], s["audio_end"])] == pytest.approx(
        [0.0, 3.25, 4.75, 7.25, 8.75, 12.0], abs=0.1)
    assert [s["raw"] for s in seg["spans"]] == ["hello", "world", "again"]
    assert [p.text for p in Document(out).paragraphs if p.style.name == "Normal" and p.text] == [
        "hello", "world", "again"]


def test_without_a_silent_gap_the_cut_is_exactly_at_the_slide_change(tmp_path, monkeypatch, capsys):
    video, pdf, pptx = setup(tmp_path, monkeypatch)  # a continuous tone: nothing to cut in
    run(video, pdf, pptx, tmp_path, "--no-cleanup")
    out = capsys.readouterr().out
    assert "2 slide changes without a silent gap" in out and "cut in the silent gap" not in out
    seg = json.loads((tmp_path / "w" / "segments.json").read_text(encoding="utf-8"))["spans"]
    assert all(s["audio_start"] == pytest.approx(s["start"], abs=1e-3) and
               s["audio_end"] == pytest.approx(s["end"], abs=1e-3) for s in seg)


def test_silence_detection_failure_degrades_to_cutting_at_the_slide_changes(tmp_path, monkeypatch, caplog):
    video, pdf, pptx = setup(tmp_path, monkeypatch)

    def broken(media):
        raise RuntimeError("silence detection failed")

    monkeypatch.setattr(lecture2doc, "detect_silences", broken)
    run(video, pdf, pptx, tmp_path, "--no-cleanup")
    assert "cutting exactly at the slide changes" in caplog.text


def numbered_setup(tmp_path, monkeypatch):
    """A numbered deck; slides 2 and 4 are only shown half built (just the footer with the page number)."""
    monkeypatch.setenv("SCADS_API_KEY", "dummy")
    pdf = make_pdf(tmp_path / "n.pdf", 4, numbers=True)
    pptx = make_pptx(tmp_path / "n.pptx", [(f"Topic {i}", "x", False) for i in range(1, 5)])
    images, _ = render_pdf(pdf, width=960)
    order = [0] * 4 + [animation_state(images[1])] * 4 + [2] * 4 + [animation_state(images[3])] * 4
    video = make_video(images, order, tmp_path / "num.mp4", size=(960, 540))
    monkeypatch.setattr(lecture2doc, "transcribe_clips", fake_stt(["one", "two", "three", "four"]))
    return video, pdf, pptx


def segment_slides(tmp_path):
    seg = json.loads((tmp_path / "w" / "segments.json").read_text(encoding="utf-8"))
    return [s["slide"] for s in seg["spans"]]


def test_slide_number_in_the_corner_identifies_half_built_slides(tmp_path, monkeypatch, capsys):
    video, pdf, pptx = numbered_setup(tmp_path, monkeypatch)
    run(video, pdf, pptx, tmp_path, "--no-cleanup")
    assert segment_slides(tmp_path) == [1, 2, 3, 4]
    out = capsys.readouterr().out
    assert "slide number readable in" in out and "it changed the slide of 8 frames" in out


def test_no_slide_numbers_option_uses_the_picture_alone(tmp_path, monkeypatch, capsys):
    video, pdf, pptx = numbered_setup(tmp_path, monkeypatch)
    run(video, pdf, pptx, tmp_path, "--no-cleanup", "--no-slide-numbers")
    assert segment_slides(tmp_path) != [1, 2, 3, 4]  # the half-built slides are lost without the number
    assert "slide number readable" not in capsys.readouterr().out


def test_a_deck_without_page_numbers_still_works_and_says_so(tmp_path, monkeypatch, capsys):
    video, pdf, pptx = setup(tmp_path, monkeypatch)  # the plain deck has no numbers
    run(video, pdf, pptx, tmp_path, "--no-cleanup")
    assert segment_slides(tmp_path) == [1, 2, 1]
    assert "no page numbers found in the PDF" in capsys.readouterr().out


def test_unreadable_numbers_fall_back_to_the_picture_with_a_warning(tmp_path, monkeypatch, caplog):
    video, pdf, pptx = numbered_setup(tmp_path, monkeypatch)

    def broken(*a, **k):
        raise RuntimeError("strip pass failed")

    monkeypatch.setattr(lecture2doc, "extract_strips", broken)
    run(video, pdf, pptx, tmp_path, "--no-cleanup")
    assert "cannot read the slide numbers" in caplog.text
    assert segment_slides(tmp_path)  # it still produced a document


def test_group_by_slide_option_also_applies_when_rebuilding(tmp_path, monkeypatch):
    video, pdf, pptx = setup(tmp_path, monkeypatch)
    run(video, pdf, pptx, tmp_path, "--no-cleanup")
    forbid_heavy_work(monkeypatch)
    out = tmp_path / "grouped.docx"
    lecture2doc.main([str(video), str(pdf), str(pptx), "-o", str(out), "--workdir", str(tmp_path / "w"),
                      "--rebuild", "--group-by-slide"])
    headings = [p.text for p in Document(out).paragraphs if p.style.name == "Heading 1"]
    assert headings == ["Slide 1 – Alpha", "Slide 2 – Beta", "Slide 3 – Gamma"]  # every slide once, in slide order
    default = tmp_path / "default.docx"
    lecture2doc.main([str(video), str(pdf), str(pptx), "-o", str(default), "--workdir", str(tmp_path / "w"),
                      "--rebuild"])
    assert [p.text for p in Document(default).paragraphs if p.style.name == "Heading 1"] == [
        "Slide 1 – Alpha", "Slide 2 – Beta", "Slide 1 – Alpha"]


def test_a_document_that_is_open_in_word_is_a_clear_error_and_nothing_is_lost(tmp_path, monkeypatch):
    video, pdf, pptx = setup(tmp_path, monkeypatch)

    def locked(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(lecture2doc, "write_docx", locked)
    with pytest.raises(SystemExit, match="open in Word.*--rebuild"):
        run(video, pdf, pptx, tmp_path, "--no-cleanup")
    assert (tmp_path / "w" / "segments.json").exists()  # the results were saved before the document was written
    monkeypatch.undo()
    monkeypatch.setenv("SCADS_API_KEY", "dummy")
    out = tmp_path / "later.docx"  # --rebuild then finishes the job offline
    forbid_heavy_work(monkeypatch)
    lecture2doc.main([str(video), str(pdf), str(pptx), "-o", str(out), "--workdir", str(tmp_path / "w"), "--rebuild"])
    assert out.exists()


def test_rebuild_into_a_locked_document_is_a_clear_error_too(tmp_path, monkeypatch):
    video, pdf, pptx = setup(tmp_path, monkeypatch)
    run(video, pdf, pptx, tmp_path, "--no-cleanup")

    def locked(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(lecture2doc, "write_docx", locked)
    with pytest.raises(SystemExit, match="open in Word"):
        lecture2doc.main([str(video), str(pdf), str(pptx), "-o", str(tmp_path / "x.docx"),
                          "--workdir", str(tmp_path / "w"), "--rebuild"])

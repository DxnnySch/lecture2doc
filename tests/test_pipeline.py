import json
import re

import pytest
from docx import Document

from lecture2doc import cli as lecture2doc
from lecture2doc import pipeline
from lecture2doc.api import RateLimiter
from helpers import animation_state, make_pdf, make_video, make_wav
from lecture2doc.slides import render_pdf


def fake_stt(texts):
    """Stands in for the speech to text: the i-th clip gets texts[i]; the clips it was asked for are recorded."""
    def stt(cfg, video, clips, *args, **kwargs):
        stt.clips = clips
        return [texts[i] if i < len(texts) else "" for i in range(len(clips))]
    return stt


def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("SCADS_API_KEY", "dummy")
    pdf = make_pdf(tmp_path / "s.pdf", 3, titles=["Alpha", "Beta", "Gamma"])
    images, _ = render_pdf(pdf)
    video = make_video(images, [0] * 4 + [1] * 4 + [0] * 4, tmp_path / "lec.mp4")
    monkeypatch.setattr(pipeline, "transcribe_clips", fake_stt(["hello", "world", "again"]))
    return video, pdf


def test_end_to_end_no_cleanup(tmp_path, monkeypatch):
    video, pdf = setup(tmp_path, monkeypatch)
    out = tmp_path / "out" / "lec.docx"
    assert lecture2doc.main([str(video), str(pdf), "-o", str(out), "--workdir", str(tmp_path / "w"),
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
    video, pdf = setup(tmp_path, monkeypatch)
    out = run(video, pdf, tmp_path, "--no-cleanup", "--group-by-slide")
    texts = [(p.style.name, p.text) for p in Document(out).paragraphs]
    assert ("Heading 1", "Slide 1 – Alpha") in texts and ("Heading 1", "Slide 3 – Gamma") in texts
    assert ("Normal", "[00:00] hello") in texts
    # the revisit start time may differ by +-1 s from 8
    assert any(style == "Normal" and re.fullmatch(r"\[\d\d:\d\d\] again", t) for style, t in texts)
    assert ("Normal", "world") in texts
    assert ("Normal", "(no speech)") in texts  # slide 3 never shown


def test_from_segments_skips_matching(tmp_path, monkeypatch):
    video, pdf = setup(tmp_path, monkeypatch)
    seg = tmp_path / "seg.json"
    seg.write_text(json.dumps({"spans": [{"slide": 3, "start": 0, "end": 12, "confidence": 1.0}]}))
    monkeypatch.setattr(pipeline, "extract_frames", lambda *a, **k: (_ for _ in ()).throw(AssertionError("matched")))
    out = tmp_path / "o.docx"
    lecture2doc.main([str(video), str(pdf), "-o", str(out), "--workdir", str(tmp_path / "w"),
                      "--no-cleanup", "--from-segments", str(seg)])
    texts = [p.text for p in Document(out).paragraphs]
    assert "hello" in texts  # one span, one clip


def test_from_segments_rejects_unknown_slide(tmp_path, monkeypatch):
    video, pdf = setup(tmp_path, monkeypatch)
    seg = tmp_path / "seg.json"
    seg.write_text(json.dumps({"spans": [{"slide": 99, "start": 0, "end": 12, "confidence": 1.0}]}))
    with pytest.raises(SystemExit, match="99"):
        lecture2doc.main([str(video), str(pdf), "-o", str(tmp_path / "o.docx"),
                          "--workdir", str(tmp_path / "w"), "--no-cleanup", "--from-segments", str(seg)])


def run(video, pdf, tmp_path, *extra, out=None):
    out = out or tmp_path / "o.docx"
    rc = lecture2doc.main([str(video), str(pdf), "-o", str(out), "--workdir", str(tmp_path / "w"), *extra])
    assert rc == 0
    return out


def test_missing_video_fails_fast(tmp_path, monkeypatch):
    video, pdf = setup(tmp_path, monkeypatch)
    with pytest.raises(SystemExit, match="video not found"):
        run(tmp_path / "nope.mp4", pdf, tmp_path, "--no-cleanup")


def test_missing_pdf_fails_fast(tmp_path, monkeypatch):
    video, pdf = setup(tmp_path, monkeypatch)
    with pytest.raises(SystemExit, match="PDF not found"):
        run(video, tmp_path / "nope.pdf", tmp_path, "--no-cleanup")


def test_missing_segments_file_fails_fast(tmp_path, monkeypatch):
    video, pdf = setup(tmp_path, monkeypatch)
    with pytest.raises(SystemExit, match="segments"):
        run(video, pdf, tmp_path, "--no-cleanup", "--from-segments", str(tmp_path / "nope.json"))


def test_from_segments_unsorted_matches_sorted(tmp_path, monkeypatch):
    video, pdf = setup(tmp_path, monkeypatch)
    spans = [{"slide": 1, "start": 0, "end": 4, "confidence": 1.0},
             {"slide": 2, "start": 4, "end": 8, "confidence": 1.0},
             {"slide": 3, "start": 8, "end": 12, "confidence": 1.0}]
    results = []
    for name, order in (("sorted", spans), ("unsorted", spans[::-1])):
        seg = tmp_path / f"{name}.json"
        seg.write_text(json.dumps({"spans": order}))
        out = run(video, pdf, tmp_path, "--no-cleanup", "--from-segments", str(seg),
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
    video, pdf = setup(tmp_path, monkeypatch)
    seg = tmp_path / "seg.json"
    seg.write_text(content)
    with pytest.raises(SystemExit, match=message):
        run(video, pdf, tmp_path, "--no-cleanup", "--from-segments", str(seg))


def test_summary_per_slide(tmp_path, monkeypatch, capsys):
    video, pdf = setup(tmp_path, monkeypatch)
    run(video, pdf, tmp_path, "--no-cleanup")
    out = capsys.readouterr().out
    assert "slide  1:" in out and "slide  2:" in out
    assert "slide  3:" not in out
    assert "slides never shown: [3]" in out
    assert "unmatched" in out


def test_summary_for_from_segments(tmp_path, monkeypatch, capsys):
    video, pdf = setup(tmp_path, monkeypatch)
    seg = tmp_path / "seg.json"
    seg.write_text(json.dumps({"spans": [{"slide": 3, "start": 0, "end": 12, "confidence": 1.0}]}))
    run(video, pdf, tmp_path, "--no-cleanup", "--from-segments", str(seg))
    out = capsys.readouterr().out
    assert "slide  3:  12s matched" in out
    assert "slides never shown: [1, 2]" in out


def test_transcribe_runtime_error_exits(tmp_path, monkeypatch):
    video, pdf = setup(tmp_path, monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("no audio stream")

    monkeypatch.setattr(pipeline, "transcribe_clips", boom)
    with pytest.raises(SystemExit, match="no audio stream"):
        run(video, pdf, tmp_path, "--no-cleanup")


def test_cleanup_wiring(tmp_path, monkeypatch):
    video, pdf = setup(tmp_path, monkeypatch)
    monkeypatch.setenv("SCADS_LLM_MODEL", "llm-x")
    monkeypatch.setenv("SCADS_GLOSSARY_MODEL", "glos-y")
    monkeypatch.setenv("SCADS_REQUESTS_PER_MINUTE", "7")
    dummy = object()
    calls = {}
    monkeypatch.setattr(pipeline, "make_client", lambda cfg: dummy)

    def fake_glossary(client, model, slides, cache, limiter=None, on_progress=None):
        calls["glossary"] = (client, model)
        return "GLOSS"

    def fake_cleanup(client, model, glossary, outline, slides, spans, raws, cache, limiter=None, on_progress=None):
        calls["cleanup"] = (client, model, glossary)
        return [f"CLEAN<{r}>" for r in raws]

    monkeypatch.setattr(pipeline, "build_glossary", fake_glossary)
    monkeypatch.setattr(pipeline, "cleanup_spans", fake_cleanup)
    out = run(video, pdf, tmp_path)
    glossary_client, cleanup_client = calls["glossary"][0], calls["cleanup"][0]
    assert glossary_client.models == ["glos-y"] and glossary_client.client is dummy
    assert cleanup_client.models == ["llm-x"] and cleanup_client.client is dummy  # one model named: only it is used
    assert calls["glossary"][1] == calls["cleanup"][1] == "auto" and calls["cleanup"][2] == "GLOSS"
    assert cleanup_client.limiters["llm-x"].max_calls == 7  # the quota applies per model
    texts = [p.text for p in Document(out).paragraphs]
    assert "CLEAN<world>" in texts and "world" not in texts
    seg = json.loads((tmp_path / "w" / "segments.json").read_text(encoding="utf-8"))
    assert seg["spans"][1]["raw"] == "world"
    assert seg["spans"][1]["clean"] == "CLEAN<world>"


def test_keep_raw(tmp_path, monkeypatch):
    video, pdf = setup(tmp_path, monkeypatch)
    out = run(video, pdf, tmp_path, "--no-cleanup", "--keep-raw")
    assert "Raw: world" in [p.text for p in Document(out).paragraphs]


def test_thumbnails_toggle(tmp_path, monkeypatch):
    video, pdf = setup(tmp_path, monkeypatch)
    with_thumbs = run(video, pdf, tmp_path, "--no-cleanup", out=tmp_path / "a.docx")
    without = run(video, pdf, tmp_path, "--no-cleanup", "--no-thumbnails", out=tmp_path / "b.docx")
    assert len(Document(with_thumbs).inline_shapes) >= 1
    assert len(Document(without).inline_shapes) == 0


def test_probe_failure_falls_back_to_16_9_with_warning(tmp_path, monkeypatch, caplog):
    video, pdf = setup(tmp_path, monkeypatch)

    def bad_probe(v):
        raise RuntimeError("ffprobe could not read the video size")

    monkeypatch.setattr(pipeline, "probe_aspect", bad_probe)
    run(video, pdf, tmp_path, "--no-cleanup")
    assert "assuming 16:9" in caplog.text


def test_frame_extraction_error_exits(tmp_path, monkeypatch):
    video, pdf = setup(tmp_path, monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("no video frames extracted from lec.mp4")

    monkeypatch.setattr(pipeline, "extract_frames", boom)
    with pytest.raises(SystemExit, match="no video frames extracted"):
        run(video, pdf, tmp_path, "--no-cleanup")


def test_transcribe_api_error_exits_without_traceback_or_key(tmp_path, monkeypatch):
    import httpx2
    import openai

    video, pdf = setup(tmp_path, monkeypatch)

    def denied(*a, **k):
        raise openai.AuthenticationError(
            "invalid credentials",
            response=httpx2.Response(401, request=httpx2.Request("POST", "http://x")), body=None)

    monkeypatch.setattr(pipeline, "transcribe_clips", denied)
    with pytest.raises(SystemExit) as exc_info:
        run(video, pdf, tmp_path, "--no-cleanup")
    msg = str(exc_info.value)
    assert "speech-to-text failed" in msg
    assert "invalid credentials" in msg
    assert "dummy" not in msg


def subtitle_lines(path):
    return [p.text for p in Document(path).paragraphs if p.style.name == "Subtitle"]


def non_subtitle(path):
    return [(p.style.name, p.text) for p in Document(path).paragraphs if p.style.name != "Subtitle"]


def test_normal_run_adds_time_subtitles(tmp_path, monkeypatch):
    video, pdf = setup(tmp_path, monkeypatch)
    out = run(video, pdf, tmp_path, "--no-cleanup")
    lines = subtitle_lines(out)
    assert len(lines) == 3  # one per page: slide 1, slide 2, slide 1 again; slide 3 never shown
    assert all(re.fullmatch(r"\d\d:\d\d – \d\d:\d\d", line) for line in lines)
    grouped = subtitle_lines(run(video, pdf, tmp_path, "--no-cleanup", "--group-by-slide",
                                 out=tmp_path / "grouped.docx"))
    assert len(grouped) == 2  # slide 1 (two visits) and slide 2
    assert re.fullmatch(r"\d\d:\d\d – \d\d:\d\d · \d\d:\d\d – \d\d:\d\d", grouped[0])
    assert re.fullmatch(r"\d\d:\d\d – \d\d:\d\d", grouped[1])
    assert subtitle_lines(run(video, pdf, tmp_path, "--no-cleanup", "--no-timestamps",
                              out=tmp_path / "plain.docx")) == []


def forbid_heavy_work(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("heavy work during --rebuild")
    for name in ("transcribe_clips", "detect_silences", "extract_frames", "match_frames", "probe_aspect", "load_config",
                 "check_ffmpeg", "make_client", "build_glossary", "cleanup_spans"):
        monkeypatch.setattr(pipeline, name, boom)


def test_rebuild_adds_subtitles_offline_without_video_or_key(tmp_path, monkeypatch):
    video, pdf = setup(tmp_path, monkeypatch)
    old = run(video, pdf, tmp_path, "--no-cleanup", "--no-timestamps", out=tmp_path / "old.docx")
    assert subtitle_lines(old) == []
    video.unlink()  # the video is not needed any more
    forbid_heavy_work(monkeypatch)
    new = tmp_path / "new.docx"
    rc = lecture2doc.main([str(video), str(pdf), "-o", str(new), "--workdir", str(tmp_path / "w"),
                           "--rebuild"])
    assert rc == 0
    assert len(subtitle_lines(new)) == 3
    assert non_subtitle(new) == non_subtitle(old)  # identical text, only the time lines were added


def test_rebuild_uses_cleaned_text_and_keeps_segments_file_untouched(tmp_path, monkeypatch):
    video, pdf = setup(tmp_path, monkeypatch)
    run(video, pdf, tmp_path, "--no-cleanup")
    segfile = tmp_path / "w" / "segments.json"
    data = json.loads(segfile.read_text(encoding="utf-8"))
    data["spans"][1]["clean"] = "World, corrected."
    segfile.write_text(json.dumps(data), encoding="utf-8")
    before = segfile.read_text(encoding="utf-8")
    forbid_heavy_work(monkeypatch)
    out = tmp_path / "r.docx"
    lecture2doc.main([str(video), str(pdf), "-o", str(out), "--workdir", str(tmp_path / "w"),
                      "--rebuild", "--keep-raw"])
    texts = [p.text for p in Document(out).paragraphs]
    assert "World, corrected." in texts and "Raw: world" in texts
    assert segfile.read_text(encoding="utf-8") == before


def test_rebuild_from_explicit_segments_file(tmp_path, monkeypatch):
    video, pdf = setup(tmp_path, monkeypatch)
    seg = tmp_path / "elsewhere.json"
    seg.write_text(json.dumps({"spans": [{"slide": 2, "start": 5, "end": 65, "raw": "r", "clean": "Cleaned."}]}),
                   encoding="utf-8")
    forbid_heavy_work(monkeypatch)
    out = tmp_path / "e.docx"
    lecture2doc.main([str(video), str(pdf), "-o", str(out), "--workdir", str(tmp_path / "w2"),
                      "--rebuild", "--from-segments", str(seg)])
    assert subtitle_lines(out) == ["00:05 – 01:05"]


@pytest.mark.parametrize("content, message", [
    (None, "segments file"),                                                   # no file at all
    ({"spans": [{"slide": 1, "start": 0, "end": 5, "raw": "r"}]}, "'clean'"),  # old/hand-made file
    ({"spans": [{"slide": 1, "start": 0, "end": 5, "clean": 3, "raw": "r"}]}, "'clean'"),
])
def test_rebuild_requires_a_complete_segments_file(tmp_path, monkeypatch, content, message):
    video, pdf = setup(tmp_path, monkeypatch)
    if content is not None:
        (tmp_path / "w").mkdir()
        (tmp_path / "w" / "segments.json").write_text(json.dumps(content), encoding="utf-8")
    forbid_heavy_work(monkeypatch)
    with pytest.raises(SystemExit, match=message):
        lecture2doc.main([str(video), str(pdf), "-o", str(tmp_path / "x.docx"),
                          "--workdir", str(tmp_path / "w"), "--rebuild"])


def test_page_breaks_by_default_and_no_page_breaks_option(tmp_path, monkeypatch):
    video, pdf = setup(tmp_path, monkeypatch)

    def flags(path):
        return [p.paragraph_format.page_break_before for p in Document(path).paragraphs if p.style.name == "Heading 1"]

    default = run(video, pdf, tmp_path, "--no-cleanup")
    assert flags(default) == [None, True, True]
    compact = run(video, pdf, tmp_path, "--no-cleanup", "--no-page-breaks", out=tmp_path / "compact.docx")
    assert flags(compact) == [None, None, None]
    forbid_heavy_work(monkeypatch)  # --rebuild honours the option too, offline
    rebuilt = tmp_path / "rebuilt.docx"
    lecture2doc.main([str(video), str(pdf), "-o", str(rebuilt), "--workdir", str(tmp_path / "w"),
                      "--rebuild", "--no-page-breaks"])
    assert flags(rebuilt) == [None, None, None]


def silent_gap_setup(tmp_path, monkeypatch):
    """The slides change at 4 s and 8 s, in the middle of the silent gaps 3-5 s and 7-9 s of the recording."""
    monkeypatch.setenv("SCADS_API_KEY", "dummy")
    pdf = make_pdf(tmp_path / "s.pdf", 3, titles=["Alpha", "Beta", "Gamma"])
    images, _ = render_pdf(pdf)
    wav = make_wav(tmp_path / "a.wav", [("tone", 3.0), ("silence", 2.0), ("tone", 2.0), ("silence", 2.0),
                                        ("tone", 3.0)])
    video = make_video(images, [0] * 4 + [1] * 4 + [0] * 4, tmp_path / "gaps.mp4", audio=wav)
    stt = fake_stt(["hello", "world", "again"])
    monkeypatch.setattr(pipeline, "transcribe_clips", stt)
    return video, pdf, stt


def test_audio_is_cut_in_the_silent_gap_at_every_slide_change(tmp_path, monkeypatch, capsys):
    video, pdf, stt = silent_gap_setup(tmp_path, monkeypatch)
    out = run(video, pdf, tmp_path, "--no-cleanup")
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
    video, pdf = setup(tmp_path, monkeypatch)  # a continuous tone: nothing to cut in
    run(video, pdf, tmp_path, "--no-cleanup")
    out = capsys.readouterr().out
    assert "2 slide changes without a silent gap" in out and "cut in the silent gap" not in out
    seg = json.loads((tmp_path / "w" / "segments.json").read_text(encoding="utf-8"))["spans"]
    assert all(s["audio_start"] == pytest.approx(s["start"], abs=1e-3) and
               s["audio_end"] == pytest.approx(s["end"], abs=1e-3) for s in seg)


def test_silence_detection_failure_degrades_to_cutting_at_the_slide_changes(tmp_path, monkeypatch, caplog):
    video, pdf = setup(tmp_path, monkeypatch)

    def broken(media):
        raise RuntimeError("silence detection failed")

    monkeypatch.setattr(pipeline, "detect_silences", broken)
    run(video, pdf, tmp_path, "--no-cleanup")
    assert "cutting exactly at the slide changes" in caplog.text


def numbered_setup(tmp_path, monkeypatch):
    """A numbered deck; slides 2 and 4 are only shown half built (just the footer with the page number)."""
    monkeypatch.setenv("SCADS_API_KEY", "dummy")
    pdf = make_pdf(tmp_path / "n.pdf", 4, numbers=True)
    images, _ = render_pdf(pdf, width=960)
    order = [0] * 4 + [animation_state(images[1])] * 4 + [2] * 4 + [animation_state(images[3])] * 4
    video = make_video(images, order, tmp_path / "num.mp4", size=(960, 540))
    monkeypatch.setattr(pipeline, "transcribe_clips", fake_stt(["one", "two", "three", "four"]))
    return video, pdf


def segment_slides(tmp_path):
    seg = json.loads((tmp_path / "w" / "segments.json").read_text(encoding="utf-8"))
    return [s["slide"] for s in seg["spans"]]


def test_slide_number_in_the_corner_identifies_half_built_slides(tmp_path, monkeypatch, capsys):
    video, pdf = numbered_setup(tmp_path, monkeypatch)
    run(video, pdf, tmp_path, "--no-cleanup")
    assert segment_slides(tmp_path) == [1, 2, 3, 4]
    out = capsys.readouterr().out
    assert "slide number readable in" in out and "it changed the slide of 8 frames" in out


def test_no_slide_numbers_option_uses_the_picture_alone(tmp_path, monkeypatch, capsys):
    video, pdf = numbered_setup(tmp_path, monkeypatch)
    run(video, pdf, tmp_path, "--no-cleanup", "--no-slide-numbers")
    assert segment_slides(tmp_path) != [1, 2, 3, 4]  # the half-built slides are lost without the number
    assert "slide number readable" not in capsys.readouterr().out


def test_a_deck_without_page_numbers_still_works_and_says_so(tmp_path, monkeypatch, capsys):
    video, pdf = setup(tmp_path, monkeypatch)  # the plain deck has no numbers
    run(video, pdf, tmp_path, "--no-cleanup")
    assert segment_slides(tmp_path) == [1, 2, 1]
    assert "no page numbers found in the PDF" in capsys.readouterr().out


def test_unreadable_numbers_fall_back_to_the_picture_with_a_warning(tmp_path, monkeypatch, caplog):
    video, pdf = numbered_setup(tmp_path, monkeypatch)

    def broken(*a, **k):
        raise RuntimeError("strip pass failed")

    monkeypatch.setattr(pipeline, "extract_strips", broken)
    run(video, pdf, tmp_path, "--no-cleanup")
    assert "cannot read the slide numbers" in caplog.text
    assert segment_slides(tmp_path)  # it still produced a document


def test_group_by_slide_option_also_applies_when_rebuilding(tmp_path, monkeypatch):
    video, pdf = setup(tmp_path, monkeypatch)
    run(video, pdf, tmp_path, "--no-cleanup")
    forbid_heavy_work(monkeypatch)
    out = tmp_path / "grouped.docx"
    lecture2doc.main([str(video), str(pdf), "-o", str(out), "--workdir", str(tmp_path / "w"),
                      "--rebuild", "--group-by-slide"])
    headings = [p.text for p in Document(out).paragraphs if p.style.name == "Heading 1"]
    assert headings == ["Slide 1 – Alpha", "Slide 2 – Beta", "Slide 3 – Gamma"]  # every slide once, in slide order
    default = tmp_path / "default.docx"
    lecture2doc.main([str(video), str(pdf), "-o", str(default), "--workdir", str(tmp_path / "w"),
                      "--rebuild"])
    assert [p.text for p in Document(default).paragraphs if p.style.name == "Heading 1"] == [
        "Slide 1 – Alpha", "Slide 2 – Beta", "Slide 1 – Alpha"]


def test_a_document_that_is_open_in_word_is_a_clear_error_and_nothing_is_lost(tmp_path, monkeypatch):
    video, pdf = setup(tmp_path, monkeypatch)

    def locked(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(pipeline, "write_docx", locked)
    with pytest.raises(SystemExit, match="open in Word"):
        run(video, pdf, tmp_path, "--no-cleanup")
    assert (tmp_path / "w" / "segments.json").exists()  # the results were saved before the document was written
    monkeypatch.undo()
    monkeypatch.setenv("SCADS_API_KEY", "dummy")
    out = tmp_path / "later.docx"  # --rebuild then finishes the job offline
    forbid_heavy_work(monkeypatch)
    lecture2doc.main([str(video), str(pdf), "-o", str(out), "--workdir", str(tmp_path / "w"), "--rebuild"])
    assert out.exists()


def test_rebuild_into_a_locked_document_is_a_clear_error_too(tmp_path, monkeypatch):
    video, pdf = setup(tmp_path, monkeypatch)
    run(video, pdf, tmp_path, "--no-cleanup")

    def locked(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(pipeline, "write_docx", locked)
    with pytest.raises(SystemExit, match="open in Word"):
        lecture2doc.main([str(video), str(pdf), "-o", str(tmp_path / "x.docx"),
                          "--workdir", str(tmp_path / "w"), "--rebuild"])


def test_the_first_run_saves_the_glossary_and_later_runs_use_the_edited_one(tmp_path, monkeypatch):
    video, pdf = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(pipeline, "make_client", lambda cfg: object())
    built, used = [], []
    monkeypatch.setattr(pipeline, "build_glossary", lambda *a, **k: built.append(1) or "alpha\nbeta")

    def fake_cleanup(client, model, glossary, outline, slides, spans, raws, cache, limiter=None, on_progress=None):
        used.append(glossary)
        return raws

    monkeypatch.setattr(pipeline, "cleanup_spans", fake_cleanup)
    run(video, pdf, tmp_path)
    assert (tmp_path / ".lecture2doc" / "glossary.txt").read_text(encoding="utf-8") == "alpha\nbeta\n"
    pipeline.save_glossary(pdf.parent, "alpha\n\n  gamma  \n")  # the user edits it
    run(video, pdf, tmp_path)
    assert built == [1] and used == ["alpha\nbeta", "alpha\ngamma\n"]


class Recorder(pipeline.Reporter):
    def __init__(self):
        super().__init__()
        self.events = []

    def emit(self, key, label, overall, fraction, detail):
        self.events.append((key, overall, fraction))


def test_progress_runs_through_the_stages_from_0_to_1_and_never_goes_back(tmp_path, monkeypatch):
    video, pdf = setup(tmp_path, monkeypatch)
    rec = Recorder()
    job = lecture2doc.job_from_args(lecture2doc.parse_args(
        [str(video), str(pdf), "-o", str(tmp_path / "o.docx"), "--workdir", str(tmp_path / "w"), "--no-cleanup"]))
    pipeline.run(job, rec)
    overall = [o for _, o, _ in rec.events]
    assert overall == sorted(overall) and overall[-1] == 1.0 and overall[0] < 0.1
    stages = [k for k, _, _ in rec.events]
    assert [k for i, k in enumerate(stages) if i == 0 or stages[i - 1] != k] == [
        "slides", "matching", "cutting", "transcribing", "writing", "done"]


def test_a_cancelled_run_stops_with_Cancelled(tmp_path, monkeypatch):
    video, pdf = setup(tmp_path, monkeypatch)

    class Stop(Recorder):
        def check(self):
            if self.events and self.events[-1][0] == "cutting":
                raise pipeline.Cancelled()

    job = lecture2doc.job_from_args(lecture2doc.parse_args(
        [str(video), str(pdf), "-o", str(tmp_path / "o.docx"), "--workdir", str(tmp_path / "w"), "--no-cleanup"]))
    with pytest.raises(pipeline.Cancelled):
        pipeline.run(job, Stop())
    assert not (tmp_path / "o.docx").exists()


def test_a_second_run_reuses_the_slide_matching(tmp_path, monkeypatch):
    video, pdf = setup(tmp_path, monkeypatch)
    run(video, pdf, tmp_path, "--no-cleanup")
    assert (tmp_path / "w" / "matching.json").is_file()
    first = (tmp_path / "w" / "segments.json").read_text(encoding="utf-8")

    def boom(*a, **k):
        raise AssertionError("matched again")

    monkeypatch.setattr(pipeline, "extract_frames", boom)
    run(video, pdf, tmp_path, "--no-cleanup")
    assert (tmp_path / "w" / "segments.json").read_text(encoding="utf-8") == first


def test_changed_matching_settings_match_again(tmp_path, monkeypatch):
    video, pdf = setup(tmp_path, monkeypatch)
    run(video, pdf, tmp_path, "--no-cleanup")
    calls = []
    real = pipeline.extract_frames
    monkeypatch.setattr(pipeline, "extract_frames", lambda *a, **k: calls.append(1) or real(*a, **k))
    run(video, pdf, tmp_path, "--no-cleanup", "--threshold", "0.7")
    assert calls == [1]


def test_the_glossary_is_built_from_every_pdf_in_the_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("SCADS_API_KEY", "dummy")
    make_pdf(tmp_path / "a.pdf", 2, titles=["Alpha one", "Alpha two"])
    make_pdf(tmp_path / "b.pdf", 1, titles=["Beta one"])
    (tmp_path / "broken.pdf").write_bytes(b"not a pdf")
    seen = []
    monkeypatch.setattr(pipeline, "make_client", lambda cfg: object())
    monkeypatch.setattr(pipeline, "build_glossary", lambda client, model, slides, cache, limiter=None, on_progress=None:
                        seen.extend(f"{s.title}|{s.number}" for s in slides) or "term")
    assert pipeline.make_glossary(tmp_path) == "term"
    assert seen == ["a: Alpha one|1", "a: Alpha two|2", "b: Beta one|1"]  # the broken file is skipped
    assert pipeline.read_glossary(tmp_path) == "term\n"  # the same glossary for every PDF of the folder


def test_the_glossary_build_reports_how_many_pdfs_are_done(tmp_path, monkeypatch):
    monkeypatch.setenv("SCADS_API_KEY", "dummy")
    for name in ("a", "b", "c"):
        make_pdf(tmp_path / f"{name}.pdf", 1)
    monkeypatch.setattr(pipeline, "make_client", lambda cfg: object())
    calls = []

    class Reply:
        def __init__(self, text):
            self.choices = [type("C", (), {"message": type("M", (), {"content": text})()})()]

    class Client:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    calls.append(1)
                    return Reply("term")

    import lecture2doc.cleanup as cleanup
    real = cleanup.build_glossary
    # 3 PDFs of ~10 characters each, at most 40 characters per request: every PDF needs its own request
    monkeypatch.setattr(pipeline, "build_glossary", lambda *a, **k: real(*a, **{**k, "max_chars": 60}))
    seen = []
    pipeline.make_glossary(tmp_path, client=Client(), on_progress=lambda *args: seen.append(args))
    assert seen[0] == (0, 3, 0, 0) and seen[-1][:2] == (3, 3) and seen[-1][2] == seen[-1][3] > 1
    assert [a[0] for a in seen] == sorted(a[0] for a in seen)
    assert len(calls) > 1


def test_text_that_repeats_across_the_slides_is_sent_to_the_glossary_only_once(tmp_path):
    import pymupdf

    doc = pymupdf.open()
    for number in (1, 2):
        page = doc.new_page(width=960, height=540)
        page.insert_text((40, 100), f"Unique{number}", fontsize=20)
        page.insert_text((40, 500), "Footer text here", fontsize=10)
    doc.save(tmp_path / "a.pdf")
    texts = [s.text for s in pipeline.folder_slides(tmp_path)]
    assert sum("Footer text here" in t for t in texts) == 1 and "Unique2" in texts[1]


def test_make_glossary_refuses_a_folder_without_pdfs(tmp_path, monkeypatch):
    monkeypatch.setenv("SCADS_API_KEY", "dummy")
    with pytest.raises(pipeline.PipelineError, match="no readable PDF"):
        pipeline.make_glossary(tmp_path)


def test_the_models_chosen_for_a_task_win_over_the_env_defaults(tmp_path):
    from lecture2doc.config import load_config

    env = tmp_path / ".env"
    env.write_text("SCADS_API_KEY=k\nSCADS_LLM_MODEL=pinned\n")
    cfg = load_config(env)
    chosen = pipeline.Options(models={"cleanup": ["a", "b"], "glossary": []})
    assert chosen.models_for("cleanup", cfg) == ["a", "b"]
    assert chosen.models_for("glossary", cfg) == ["pinned"]       # nothing chosen: the .env says
    assert chosen.models_for("transcribe", cfg) == ["alias-stt"]
    env.write_text("SCADS_API_KEY=k\nSCADS_LLM_MODELS=x,y\n")
    assert pipeline.Options().models_for("cleanup", load_config(env)) == ["x", "y"]

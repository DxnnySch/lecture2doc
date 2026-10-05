from docx import Document
from PIL import Image

from docx_out import SpanText, fmt_time, paragraphize, write_docx
from slides import Slide


def slides():
    img = Image.new("RGB", (48, 27), "white")
    return [Slide(1, "Intro", "", img), Slide(2, None, "", img), Slide(3, "Outro", "", img)]


def read(path):
    doc = Document(path)
    return doc, [(p.style.name, p.text) for p in doc.paragraphs]


def test_fmt_time():
    assert fmt_time(75) == "01:15" and fmt_time(3725) == "1:02:05"


def test_paragraphize_groups_sentences():
    assert paragraphize("A. B! C? D. E.", per=4) == ["A. B! C? D.", "E."]
    assert paragraphize("no punctuation here") == ["no punctuation here"]


def test_headings_titles_and_no_speech(tmp_path):
    out = tmp_path / "o.docx"
    write_docx(out, "My Lecture", slides(), [SpanText(1, 0, "Hello there.", "hello there")])
    doc, paras = read(out)
    assert ("Title", "My Lecture") in paras
    assert ("Heading 1", "Slide 1 – Intro") in paras
    assert ("Heading 1", "Slide 2") in paras          # untitled -> number only
    assert ("Heading 1", "Slide 3 – Outro") in paras
    assert paras.count(("Normal", "(no speech)")) == 2
    assert ("Normal", "Hello there.") in paras        # single span: no time prefix
    assert len(doc.inline_shapes) == 3


def test_revisited_slide_has_one_heading_and_timed_paragraphs(tmp_path):
    out = tmp_path / "o.docx"
    spans = [SpanText(1, 0, "First visit.", "r"), SpanText(2, 30, "Middle.", "r"),
             SpanText(1, 3700, "Back again.", "r")]
    write_docx(out, "L", slides(), spans)
    _, paras = read(out)
    assert [t for s, t in paras if s == "Heading 1"].count("Slide 1 – Intro") == 1
    assert ("Normal", "[00:00] First visit.") in paras
    assert ("Normal", "[1:01:40] Back again.") in paras
    assert ("Normal", "Middle.") in paras


def test_options_no_thumbnails_and_keep_raw(tmp_path):
    out = tmp_path / "o.docx"
    write_docx(out, "L", slides(), [SpanText(1, 0, "Clean.", "raw words")], thumbnails=False, keep_raw=True)
    doc, paras = read(out)
    assert len(doc.inline_shapes) == 0
    assert ("Normal", "Raw: raw words") in paras

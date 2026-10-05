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
    assert fmt_time(75) == "01:15"
    assert fmt_time(3725) == "1:02:05"
    assert fmt_time(3600) == "1:00:00"


def test_paragraphize_groups_sentences():
    assert paragraphize("A. B! C? D. E.", per=4) == ["A. B! C? D.", "E."]
    assert paragraphize("no punctuation here") == ["no punctuation here"]


def test_paragraphize_long_unpunctuated():
    # 200-word string without punctuation should split into 3 paragraphs: 80, 80, 40 words
    # Use distinct words to make word order verification meaningful
    distinct_words = [f"w{i}" for i in range(200)]
    text = " ".join(distinct_words)
    result = paragraphize(text)
    assert len(result) == 3
    assert len(result[0].split()) == 80
    assert len(result[1].split()) == 80
    assert len(result[2].split()) == 40
    # Verify word order preserved and no words lost
    result_words = []
    for para in result:
        result_words.extend(para.split())
    assert result_words == distinct_words


def test_paragraphize_normal_unchanged():
    # Normal punctuated text should behave as before
    normal = "First sentence. Second sentence. Third sentence."
    result = paragraphize(normal, per=4)
    assert len(result) == 1
    assert result[0] == normal


def test_paragraphize_long_sentence_in_middle():
    # Long sentence should flush the current group, emit chunks, then continue grouping normally
    # 2 normal, 1 long (200 words), 2 normal -> [2 joined], 80, 80, 40, [2 joined]
    long_words = " ".join([f"w{i}" for i in range(200)]) + "."
    text = f"First. Second. {long_words} Sixth. Seventh."
    result = paragraphize(text, per=2)
    assert len(result) == 5
    # First paragraph: First and Second joined (2 normal sentences)
    assert result[0] == "First. Second."
    # Next 3 paragraphs: 80, 80, 40 word chunks
    assert len(result[1].split()) == 80
    assert len(result[2].split()) == 80
    assert len(result[3].split()) == 40
    # Last paragraph: Sixth and Seventh joined (2 normal sentences)
    assert result[4] == "Sixth. Seventh."


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


def test_empty_span_skipped(tmp_path):
    # Span with empty or whitespace-only text should be skipped
    out = tmp_path / "o.docx"
    write_docx(out, "L", slides(), [SpanText(1, 0, "   ", "raw")])
    _, paras = read(out)
    assert paras.count(("Normal", "(no speech)")) == 3  # All 3 slides show "(no speech)"


def test_control_characters_stripped(tmp_path):
    # Control characters should be stripped without raising and text should be preserved
    out = tmp_path / "o.docx"
    title = "Lec\x00ture"
    slide_title = "Slide\x08Title"
    span_text = "Text\x0bwith\x1fcontrol chars."
    raw_text = "Raw\x0ctext"

    spans = [SpanText(1, 0, span_text, raw_text)]
    slides_list = [
        Slide(1, slide_title, "", Image.new("RGB", (48, 27), "white")),
        Slide(2, None, "", Image.new("RGB", (48, 27), "white")),
        Slide(3, "Outro", "", Image.new("RGB", (48, 27), "white"))
    ]

    # Must not raise ValueError and must strip control characters
    write_docx(out, title, slides_list, spans, thumbnails=False, keep_raw=True)
    doc, paras = read(out)

    # Verify no forbidden control characters appear in any paragraph
    forbidden_chars = [chr(i) for i in list(range(0x00, 0x09)) + [0x0b, 0x0c] + list(range(0x0e, 0x20))]
    for style_name, para_text in paras:
        for char in forbidden_chars:
            assert char not in para_text, f"Found forbidden char {repr(char)} in paragraph: {repr(para_text)}"

    # Verify exact expected strings are present
    assert ("Title", "Lecture") in paras, f"Title 'Lecture' not found in paras"
    assert ("Heading 1", "Slide 1 – SlideTitle") in paras, f"Heading not found in paras"
    assert ("Normal", "Textwithcontrol chars.") in paras, f"Span text not found in paras"
    assert ("Normal", "Raw: Rawtext") in paras, f"Raw text not found in paras"

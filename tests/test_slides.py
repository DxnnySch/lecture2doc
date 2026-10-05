import logging

from helpers import make_pdf, make_pptx
from slides import load_slides, read_pptx, render_pdf


def test_render_pdf(tmp_path):
    images, texts = render_pdf(make_pdf(tmp_path / "a.pdf", 3))
    assert len(images) == 3 and images[0].size == (480, 270)
    assert "Title 1" in texts[0]


def test_titles_from_pptx_when_counts_match(tmp_path):
    pdf = make_pdf(tmp_path / "a.pdf", 2)
    pptx = make_pptx(tmp_path / "a.pptx", [("Intro  to\nGDI", "body one", False), (None, "only body", False)])
    slides = load_slides(pdf, pptx)
    assert [s.number for s in slides] == [1, 2]
    assert slides[0].title == "Intro to GDI"
    assert "body one" in slides[0].text
    assert slides[1].title == "only body"


def test_hidden_pptx_slides_are_skipped(tmp_path):
    pptx = make_pptx(tmp_path / "a.pptx", [("A", "x", False), ("Hidden", "y", True), ("C", "z", False)])
    assert [t for t, _ in read_pptx(pptx)] == ["A", "C"]
    pdf = make_pdf(tmp_path / "a.pdf", 2)  # PDF export omits hidden slide
    assert [s.title for s in load_slides(pdf, pptx)] == ["A", "C"]


def test_count_mismatch_falls_back_to_pdf_titles(tmp_path, caplog):
    pdf = make_pdf(tmp_path / "a.pdf", 3)
    pptx = make_pptx(tmp_path / "a.pptx", [("A", "x", False), ("B", "y", False)])
    with caplog.at_level(logging.WARNING):
        slides = load_slides(pdf, pptx)
    assert [s.title for s in slides] == ["Title 1", "Title 2", "Title 3"]
    assert "does not match" in caplog.text


def test_unreadable_pptx_falls_back_to_pdf_titles(tmp_path, caplog):
    pdf = make_pdf(tmp_path / "a.pdf", 3)
    bad = tmp_path / "bad.pptx"
    bad.write_bytes(b"this is not a zip file")
    with caplog.at_level(logging.WARNING):
        slides = load_slides(pdf, bad)
    assert [s.title for s in slides] == ["Title 1", "Title 2", "Title 3"]
    assert "cannot read PPTX" in caplog.text


def test_title_falls_back_to_first_body_line_squashed_and_truncated(tmp_path):
    pdf = make_pdf(tmp_path / "a.pdf", 1)
    long_line = "word " * 40
    pptx = make_pptx(tmp_path / "a.pptx", [(None, "  first   line \nsecond", False)])
    assert load_slides(pdf, pptx)[0].title == "first line"
    pptx = make_pptx(tmp_path / "b.pptx", [(None, long_line, False)])
    title = load_slides(pdf, pptx)[0].title
    assert len(title) == 100 and title.endswith("…")


def test_title_none_when_slide_has_no_text(tmp_path):
    pdf = make_pdf(tmp_path / "a.pdf", 1)
    pptx = make_pptx(tmp_path / "a.pptx", [(None, "", False)])
    assert load_slides(pdf, pptx)[0].title is None


def make_deck_pdf(path, pages):
    """Pages like a real lecture export: the small footer comes FIRST in reading order, the title after it."""
    import pymupdf

    doc = pymupdf.open()
    for title, body in pages:
        page = doc.new_page(width=960, height=540)
        page.insert_text((40, 520), "Lecture: Footer Text – Someone", fontsize=10)
        page.insert_text((900, 520), "7", fontsize=10)
        if title:
            page.insert_text((40, 70), title, fontsize=30)
        for k, line in enumerate(body):
            page.insert_text((60, 150 + 30 * k), line, fontsize=16)
    doc.save(path)
    return path


def broken_pptx(tmp_path):
    p = tmp_path / "broken.pptx"
    p.write_bytes(b"not a zip")
    return p


def test_pdf_title_is_the_largest_top_text_not_the_footer(tmp_path):
    from slides import pdf_titles

    pdf = make_deck_pdf(tmp_path / "d.pdf", [("Techniques for decision support", ["Alternatives", "Criteria"]),
                                             ("Second slide", ["point"])])
    assert pdf_titles(pdf) == ["Techniques for decision support", "Second slide"]


def test_unreadable_pptx_uses_pdf_titles_not_footer(tmp_path, caplog):
    pdf = make_deck_pdf(tmp_path / "d.pdf", [("Real Title One", ["x"]), ("Real Title Two", ["y"])])
    with caplog.at_level(logging.WARNING):
        slides = load_slides(pdf, broken_pptx(tmp_path))
    assert [s.title for s in slides] == ["Real Title One", "Real Title Two"]
    assert "cannot read PPTX" in caplog.text


def test_count_mismatch_uses_pdf_titles_not_footer(tmp_path):
    pdf = make_deck_pdf(tmp_path / "d.pdf", [("Alpha topic", ["x"]), ("Beta topic", ["y"]), ("Gamma topic", ["z"])])
    pptx = make_pptx(tmp_path / "d.pptx", [("A", "x", False), ("B", "y", False)])
    assert [s.title for s in load_slides(pdf, pptx)] == ["Alpha topic", "Beta topic", "Gamma topic"]


def test_wrapped_title_is_joined_and_page_without_title_text_is_none(tmp_path):
    import pymupdf
    from slides import pdf_titles

    doc = pymupdf.open()
    page = doc.new_page(width=960, height=540)
    page.insert_text((40, 520), "Footer only here", fontsize=10)
    page.insert_textbox(pymupdf.Rect(40, 40, 420, 200),
                        "A very long slide title that must wrap over several lines", fontsize=28)
    footer_only = doc.new_page(width=960, height=540)
    footer_only.insert_text((40, 520), "Footer only here", fontsize=10)
    doc.new_page(width=960, height=540)  # completely empty page
    doc.save(tmp_path / "w.pdf")
    assert pdf_titles(tmp_path / "w.pdf") == ["A very long slide title that must wrap over several lines", None, None]


def test_centered_title_slide_and_long_titles_are_truncated(tmp_path):
    import pymupdf
    from slides import pdf_titles

    doc = pymupdf.open()
    page = doc.new_page(width=960, height=540)
    page.insert_text((60, 270), "Geodata Infrastructures", fontsize=44)  # vertically centred title slide
    page.insert_text((60, 330), "Winter term", fontsize=20)
    page.insert_text((40, 520), "Footer", fontsize=10)
    long_page = doc.new_page(width=960, height=540)
    long_page.insert_text((10, 60), "W" * 5 + " " + "long " * 40, fontsize=12)
    doc.save(tmp_path / "c.pdf")
    titles = pdf_titles(tmp_path / "c.pdf")
    assert titles[0] == "Geodata Infrastructures"
    assert len(titles[1]) == 100 and titles[1].endswith("…")

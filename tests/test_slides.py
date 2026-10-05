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
    assert slides[1].title is None


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

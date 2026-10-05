from helpers import make_pdf
from lecture2doc.slides import load_slides, render_pdf


def test_render_pdf(tmp_path):
    images, texts = render_pdf(make_pdf(tmp_path / "a.pdf", 3))
    assert len(images) == 3 and images[0].size == (480, 270)
    assert "Title 1" in texts[0]


def test_load_slides_numbers_pages_and_reads_titles_and_text(tmp_path):
    slides = load_slides(make_pdf(tmp_path / "a.pdf", 2, titles=["Intro", "Method"]))
    assert [s.number for s in slides] == [1, 2]
    assert [s.title for s in slides] == ["Intro", "Method"]
    assert "Intro" in slides[0].text and slides[0].image.size == (480, 270)


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


def test_pdf_title_is_the_largest_top_text_not_the_footer(tmp_path):
    from lecture2doc.slides import pdf_titles

    pdf = make_deck_pdf(tmp_path / "d.pdf", [("Techniques for decision support", ["Alternatives", "Criteria"]),
                                             ("Second slide", ["point"])])
    assert pdf_titles(pdf) == ["Techniques for decision support", "Second slide"]


def test_wrapped_title_is_joined_and_page_without_title_text_is_none(tmp_path):
    import pymupdf
    from lecture2doc.slides import pdf_titles

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
    from lecture2doc.slides import pdf_titles

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

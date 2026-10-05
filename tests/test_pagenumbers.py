import numpy as np
import pymupdf
import pytest

from helpers import animation_state, make_pdf, make_video
from pagenumbers import (NumberEvidence, NumberRegion, extract_strips, find_number_region, frame_crop,
                         number_templates, numbers_from_strips)
from segment import build_spans, extract_frames, match_frames, probe_size, slide_features, smooth
from slides import render_pdf


def numbered_deck(tmp_path, n=4):
    return make_pdf(tmp_path / "n.pdf", n, numbers=True)


def test_region_is_found_from_the_numbers_printed_in_the_pdf(tmp_path):
    region = find_number_region(numbered_deck(tmp_path, 12))
    assert region is not None
    assert region.x0 <= 881 and region.x1 >= 895 and region.y0 <= 498 and region.y1 >= 510  # covers "1".."12"
    assert region.x0 > 800 and region.y0 > 470 and region.x1 - region.x0 < 80 and region.y1 - region.y0 < 40
    assert (region.page_w, region.page_h) == (960, 540)


def test_no_region_without_enough_page_numbers(tmp_path):
    assert find_number_region(make_pdf(tmp_path / "plain.pdf", 6)) is None  # no numbers at all
    assert find_number_region(make_pdf(tmp_path / "one.pdf", 1, numbers=True)) is None  # nothing to tell apart
    doc = pymupdf.open()
    for i in range(6):
        page = doc.new_page(width=960, height=540)
        if i < 2:  # only a third of the pages carry a number
            page.insert_text((881, 508), str(i + 1), fontsize=14)
    doc.save(tmp_path / "few.pdf")
    assert find_number_region(tmp_path / "few.pdf") is None


def test_templates_are_distinct_per_page_and_blank_for_unnumbered_pages(tmp_path):
    pdf = numbered_deck(tmp_path, 4)
    region = find_number_region(pdf)
    templates = number_templates(pdf, region)
    assert templates.shape == (4, 2 * (region.x1 - region.x0) * 2 * (region.y1 - region.y0))
    gram = templates @ templates.T
    assert np.allclose(np.diag(gram), 1.0, atol=1e-4)
    assert (gram - np.eye(4)).max() < 0.95  # the digits tell the pages apart
    blank = make_pdf(tmp_path / "b.pdf", 3)  # no numbers: the corner is empty
    assert not number_templates(blank, region).any()


@pytest.mark.parametrize("slide_size, video_size, region_pts, expected", [
    ((960, 540), (1920, 1080), (868, 490, 912, 518), (1736, 980, 88, 56)),   # same aspect: plain scaling
    ((720, 540), (1920, 1080), (640, 490, 690, 518), (1520, 980, 100, 56)),  # 4:3 slide pillarboxed in 16:9
    ((960, 540), (1440, 1080), (868, 490, 912, 518), (1302, 870, 66, 42)),  # 16:9 slide letterboxed in 4:3
])
def test_frame_crop_follows_the_slide_inside_the_video_frame(slide_size, video_size, region_pts, expected):
    region = NumberRegion(*region_pts, *slide_size)
    assert frame_crop(region, *video_size) == expected


def fake_templates():
    pages = np.random.RandomState(0).rand(4, 300)
    pages[3] = pages[2]  # pages 3 and 4 print the same number (e.g. build steps exported as separate pages)
    centred = pages - pages.mean(axis=1, keepdims=True)
    return pages, centred / np.linalg.norm(centred, axis=1, keepdims=True)


def test_evidence_trusts_clear_matches_only_and_keeps_ties():
    pages, templates = fake_templates()
    noise = np.random.RandomState(1).rand(300)
    strips = np.stack([pages[0], pages[1] + 0.01 * noise, pages[2], np.full(300, 0.5), noise]).reshape(5, 10, 30)
    evidence = numbers_from_strips(strips, templates)
    assert evidence.mask[0].tolist() == [True, False, False, False]
    assert evidence.mask[1].tolist() == [False, True, False, False]
    assert evidence.mask[2].tolist() == [False, False, True, True]  # a tie: both pages stay candidates
    assert not evidence.mask[3].any() and not evidence.mask[4].any()  # blank corner / unrelated picture
    assert evidence.score[0] > 0.99 and evidence.score[3] == 0.0


def unit_rows(rows):
    centred = rows - rows.mean(axis=1, keepdims=True)
    return centred / np.linalg.norm(centred, axis=1, keepdims=True)


def test_number_evidence_overrides_a_misleading_picture_and_the_threshold():
    rng = np.random.RandomState(2)
    feats = unit_rows(rng.rand(4, 50))
    frames = np.stack([feats[0] * 100 + 128, feats[3] * 100 + 128, rng.rand(50) * 255]).astype(np.uint8).reshape(3, 5, 10)
    plain, _ = match_frames(frames, feats, threshold=0.6)
    assert plain[:2].tolist() == [0, 3] and plain[2] == -1
    # the corner says: frame 0 is page 2 (although it looks like page 1), frame 1 is page 3 or 4,
    # frame 2 is page 3 (its picture is unusable)
    mask = np.array([[0, 1, 0, 0], [0, 0, 1, 1], [0, 0, 1, 0]], dtype=bool)
    evidence = NumberEvidence(mask, np.array([0.99, 0.98, 0.97]))
    labels, conf = match_frames(frames, feats, threshold=0.6, numbers=evidence)
    assert labels.tolist() == [1, 3, 2]  # frame 1 breaks the 3-vs-4 tie by its picture; frame 2 ignores the threshold
    assert conf.tolist() == pytest.approx([0.99, 0.98, 0.97])
    no_corner = NumberEvidence(np.zeros((3, 4), bool), np.zeros(3))
    assert match_frames(frames, feats, 0.6, numbers=no_corner)[0].tolist() == plain.tolist()  # falls back


def test_probe_size_and_strip_extraction(tmp_path):
    pdf = numbered_deck(tmp_path)
    images, _ = render_pdf(pdf, width=960)
    video = make_video(images, [0] * 3, tmp_path / "v.mp4", size=(960, 540))
    assert probe_size(video) == (960, 540)
    region = find_number_region(pdf)
    strips = extract_strips(video, region)
    assert strips.shape[1:] == (2 * (region.y1 - region.y0), 2 * (region.x1 - region.x0))
    assert abs(len(strips) - 3) <= 1
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"not a video")
    with pytest.raises(RuntimeError):
        extract_strips(junk, region)


def test_end_to_end_a_half_built_slide_is_identified_by_its_number(tmp_path):
    pdf = numbered_deck(tmp_path, 4)
    images, _ = render_pdf(pdf, width=960)
    frames_of = [0] * 4 + [animation_state(images[1])] * 4 + [2] * 4 + [animation_state(images[3])] * 4
    video = make_video(images, frames_of, tmp_path / "v.mp4", size=(960, 540))
    frames = extract_frames(video)
    feats = slide_features(images)
    plain_labels, _ = match_frames(frames, feats)  # without the number the empty frames are lost
    assert plain_labels[4:8].tolist() != [1] * 4 and plain_labels[12:16].tolist() != [3] * 4
    region = find_number_region(pdf)
    evidence = numbers_from_strips(extract_strips(video, region), number_templates(pdf, region))
    n = min(len(frames), len(evidence.mask))
    labels, conf = match_frames(frames[:n], feats, numbers=NumberEvidence(evidence.mask[:n], evidence.score[:n]))
    spans = build_spans(smooth(labels), conf, 1.0)
    assert [s.slide for s in spans] == [1, 2, 3, 4]
    assert conf.min() > 0.9

import pytest

from align import snap_to_sentences
from segment import Span
from transcribe import Word, assign_words


def words_at(*items):
    return [Word(w, s, e) for w, s, e in items]


def texts(words, spans):
    return assign_words(words, spans)


def test_boundary_inside_a_sentence_moves_forward_to_its_end():
    # the lecturer clicks to the next slide (t=4.0) before finishing "…has to be published."
    words = words_at(("the", 0.0, 0.3), ("choice", 0.4, 0.8), ("has", 1.0, 1.2), ("to", 1.3, 1.5),
                     ("be", 4.1, 4.3), ("published.", 4.4, 5.0), ("After", 5.6, 5.9), ("that", 6.0, 6.3))
    spans = [Span(1, 0, 4.0, 0.9), Span(2, 4.0, 8.0, 0.9)]
    assert texts(words, spans) == ["the choice has to", "be published. After that"]  # the bug
    snapped = snap_to_sentences(words, spans)
    assert texts(words, snapped) == ["the choice has to be published.", "After that"]
    assert [(s.slide, s.confidence) for s in snapped] == [(1, 0.9), (2, 0.9)]
    assert snapped[0].start == 0 and snapped[1].end == 8.0 and snapped[0].end == snapped[1].start
    assert 5.0 < snapped[0].end < 5.6  # in the pause after the sentence


def test_boundary_moves_back_when_the_new_sentence_began_on_the_old_slide():
    # "Normally, we would start." begins at 3.5 but the click only comes at 4.1: closer to its start than its end
    words = words_at(("in", 0.0, 0.2), ("our", 0.3, 0.5), ("course.", 0.6, 1.0), ("Normally,", 3.5, 4.0),
                     ("we", 4.2, 4.4), ("would", 4.5, 4.9), ("start.", 5.0, 5.4))
    spans = [Span(1, 0, 4.1, 0.9), Span(2, 4.1, 9.0, 0.9)]
    assert texts(words, spans) == ["in our course. Normally,", "we would start."]
    assert texts(words, snap_to_sentences(words, spans)) == ["in our course.", "Normally, we would start."]


def test_the_closer_edge_of_the_sentence_wins_and_a_clean_cut_is_left_alone():
    words = words_at(("a.", 0.0, 0.5), ("b", 1.0, 1.5), ("c", 2.0, 2.5), ("d.", 3.0, 3.5), ("e", 9.0, 9.5), ("f.", 9.6, 10.0))
    spans = [Span(1, 0, 2.8, 1), Span(2, 2.8, 12.0, 1)]
    assert texts(words, snap_to_sentences(words, spans)) == ["a. b c d.", "e f."]
    clean = [Span(1, 0, 6.5, 1), Span(2, 6.5, 12.0, 1)]  # falls between two sentences already
    assert snap_to_sentences(words, clean) == clean


def test_sentence_edges_further_away_than_max_shift_are_ignored():
    words = words_at(("a.", 0.0, 1.5), ("w1", 2.0, 2.5), ("w2", 3.0, 3.5), ("w3", 5.0, 5.5), ("w4", 7.0, 7.5),
                     ("end.", 11.0, 12.0), ("z", 13.0, 13.5))
    spans = [Span(1, 0, 6.5, 1), Span(2, 6.5, 14.0, 1)]  # 4.5s after the sentence start, 5.5s before its end
    assert snap_to_sentences(words, spans) == spans                     # default 4s
    assert snap_to_sentences(words, spans, max_shift=2.0) == spans
    assert texts(words, snap_to_sentences(words, spans, max_shift=5.0))[0] == "a."  # back to the sentence start


def test_max_shift_zero_disables_and_degenerate_inputs_are_unchanged():
    words = words_at(("one", 0.0, 0.5), ("two.", 0.6, 1.0), ("three.", 1.2, 1.6))
    spans = [Span(1, 0, 0.8, 1), Span(2, 0.8, 3.0, 1)]
    assert snap_to_sentences(words, spans, max_shift=0) == spans
    assert snap_to_sentences(words, spans, max_shift=-1) == spans
    assert snap_to_sentences(words, spans[:1]) == spans[:1]
    assert snap_to_sentences([], spans) == spans
    # without any sentence punctuation there is nothing to snap to
    assert snap_to_sentences(words_at(("no", 0.0, 0.5), ("punctuation", 0.6, 1.0)), spans) == spans


def test_never_snaps_to_the_start_or_end_of_the_whole_text():
    words = words_at(("a", 0.0, 0.5), ("b", 1.0, 1.5), ("c", 2.0, 2.5), ("d", 3.0, 3.5))  # unterminated sentence
    spans = [Span(1, 0, 1.2, 1), Span(2, 1.2, 4.0, 1)]
    assert snap_to_sentences(words, spans) == spans


def test_abbreviations_are_not_sentence_ends():
    words = words_at(("use", 0.0, 0.3), ("tools,", 0.4, 0.8), ("e.g.", 0.9, 1.3), ("WMS", 1.4, 1.8), ("or", 1.9, 2.1),
                     ("WFS.", 2.2, 2.6), ("Next", 3.0, 3.4))
    spans = [Span(1, 0, 1.5, 1), Span(2, 1.5, 5.0, 1)]
    assert texts(words, snap_to_sentences(words, spans)) == ["use tools, e.g. WMS or WFS.", "Next"]


def test_a_move_that_would_squash_a_neighbour_falls_back_to_the_other_edge():
    # one-second words, three-word sentences; the first two cuts both prefer the same pause
    words = words_at(("a1", 0, 1), ("a2", 1, 2), ("a3.", 2, 3), ("b1", 3, 4), ("b2", 4, 5), ("b3.", 5, 6),
                     ("c1", 6, 7), ("c2", 7, 8), ("c3.", 8, 9))
    spans = [Span(1, 0, 2.0, 1), Span(2, 2.0, 4.0, 1), Span(3, 4.0, 9.0, 1)]
    snapped = snap_to_sentences(words, spans, min_span=1.0)
    assert texts(words, snapped) == ["a1 a2 a3.", "b1 b2 b3.", "c1 c2 c3."]
    for a, b in zip(snapped, snapped[1:]):
        assert a.end == b.start
    assert all(s.end - s.start >= 1.0 for s in snapped)
    assert [s.slide for s in snapped] == [1, 2, 3] and snapped[0].start == 0 and snapped[-1].end == 9.0


def test_spans_with_gaps_from_a_hand_edited_file_do_not_crash():
    words = words_at(("a", 0.0, 0.5), ("b.", 0.6, 1.0), ("c", 4.0, 4.5), ("d.", 4.6, 5.0))
    spans = [Span(1, 0, 0.8, 1), Span(2, 3.0, 6.0, 1)]
    snapped = snap_to_sentences(words, spans)
    assert len(snapped) == 2 and snapped[0].end == snapped[1].start


@pytest.mark.parametrize("word", ["Dr.", "z.B.", "etc.", "approx.", "d.h."])
def test_common_abbreviations_in_english_and_german(word):
    words = words_at(("see", 0.0, 0.3), (word, 0.4, 0.8), ("Meier", 0.9, 1.3), ("said", 1.4, 1.8), ("so.", 1.9, 2.3),
                     ("Next", 3.0, 3.4))
    spans = [Span(1, 0, 0.9, 1), Span(2, 0.9, 5.0, 1)]
    assert texts(words, snap_to_sentences(words, spans))[0] == "see " + word + " Meier said so."

import pytest

from audiocuts import Clip, detect_silences, plan_cuts
from helpers import make_wav
from segment import Span


def test_silences_are_found_with_their_times_including_one_at_the_very_end(tmp_path):
    wav = make_wav(tmp_path / "a.wav", [("tone", 2.0), ("silence", 1.5), ("tone", 2.0), ("silence", 0.2),
                                        ("tone", 1.0), ("silence", 3.0)])
    found = detect_silences(wav)
    assert len(found) == 3
    for (start, end), (want_start, want_end) in zip(found, [(2.0, 3.5), (5.5, 5.7), (6.7, 9.7)]):
        assert start == pytest.approx(want_start, abs=0.06) and end == pytest.approx(want_end, abs=0.06)


def test_a_file_without_silence_has_no_gaps_and_a_broken_file_is_an_error(tmp_path):
    assert detect_silences(make_wav(tmp_path / "t.wav", [("tone", 3.0)])) == []
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"not media")
    with pytest.raises(RuntimeError):
        detect_silences(junk)


def spans(*bounds):
    """Contiguous spans of slides 1, 2, 3, ... with the given outer boundaries, e.g. spans(0, 10, 20)."""
    return [Span(i + 1, a, b, 1.0) for i, (a, b) in enumerate(zip(bounds, bounds[1:]))]


def test_each_slide_change_is_cut_in_its_silent_gap_and_the_clips_are_trimmed_to_the_speech():
    # the change at 10 s lies in the middle of the gap 8.5-11.5; the one at 20 s just after its gap 17.5-19.8
    silences = [(8.5, 11.5), (17.5, 19.8)]
    clips, report = plan_cuts(spans(0, 10, 20, 30), silences)
    # the cut is in the middle of the gap; each clip keeps only 0.25 s of the silence around it
    assert [(c.start, c.end) for c in clips] == [(0, 8.75), (11.25, 17.75), (19.55, 30)]
    assert report.in_silence == 2 and report.at_video_change == 0 and report.gap_lengths == pytest.approx([3.0, 2.3])


def test_the_longest_gap_near_the_change_wins_over_short_natural_pauses():
    silences = [(9.0, 9.6), (10.2, 12.2), (13.0, 13.7)]  # a 0.6 s pause, the real 2.0 s gap, a 0.7 s pause
    clips, _ = plan_cuts(spans(0, 10, 30), silences)
    assert (clips[0].end, clips[1].start) == (pytest.approx(10.45), pytest.approx(11.95))  # the 2.0 s gap 10.2-12.2


def test_gaps_that_are_too_far_or_too_short_are_ignored_and_the_video_change_is_used():
    silences = [(2.0, 2.4), (30.0, 33.0)]  # a 0.4 s pause (below 0.5 s) and a gap 20 s away from the change
    clips, report = plan_cuts(spans(0, 10, 60), silences)
    assert (clips[0].end, clips[1].start) == (10, 10)
    assert report.in_silence == 0 and report.at_video_change == 1 and report.gap_lengths == []


def test_cuts_stay_in_order_when_two_changes_would_pick_the_same_gap():
    # slide 2 is shown for 2 s only; both changes are closest to the one long gap 9-13
    clips, report = plan_cuts(spans(0, 10, 12, 30), [(9.0, 13.0)])
    ends = [c.end for c in clips]
    assert ends == sorted(ends) and len(set(ends)) == len(ends) and ends[-1] == 30
    assert all(c.end - c.start >= 0.5 for c in clips) and clips[1].end == 12  # clip 2: the second change is at 12 s
    assert clips[0].end == pytest.approx(9.25) and clips[2].start == 12
    assert report.in_silence + report.at_video_change == 2


def test_speech_time_of_a_clip_excludes_its_silences():
    # the change at 10 s is cut in the middle of the gap 9-11; clip 1 is 0-10 s, clip 2 is 10-20 s
    clips, _ = plan_cuts(spans(0, 10, 20), [(2.0, 5.0), (9.0, 11.0), (14.0, 14.3)])
    assert clips[0].speech == pytest.approx(10.0 - 3.0 - 1.0)  # minus the pause 2-5 and the half gap 9-10
    assert clips[1].speech == pytest.approx(10.0 - 1.0 - 0.3)  # minus the half gap 10-11 and the pause 14-14.3
    mostly_silent, _ = plan_cuts(spans(0, 10, 20), [(10.5, 20.0), (0.0, 9.0)])  # the longer gap 10.5-20 is cut in half
    assert mostly_silent[0].speech == pytest.approx(1.5) and mostly_silent[1].speech == pytest.approx(0.0)


def test_single_span_and_edges():
    clips, report = plan_cuts(spans(0, 42), [(5.0, 9.0)])
    assert clips == [Clip(0, 42, pytest.approx(38.0))] and report.in_silence == 0 and report.at_video_change == 0
    clips, _ = plan_cuts([Span(1, 3.0, 8.0, 1), Span(2, 8.0, 15.0, 1)], [])
    assert (clips[0].start, clips[-1].end) == (3.0, 15.0)


def test_neighbouring_changes_each_get_their_own_gap_even_if_the_other_gap_is_longer():
    # the changes at 4 s and 8 s are 4 s apart, so the window of the second one also contains the first gap,
    # which happens to be a hair longer
    silences = [(3.0, 5.0002), (7.0061, 8.9949)]
    clips, report = plan_cuts(spans(0, 4, 8, 12), silences)
    assert [c.end for c in clips][:2] == [pytest.approx(3.25), pytest.approx(7.2561)]
    assert [c.start for c in clips][1:] == [pytest.approx(4.7502), pytest.approx(8.7449)]
    assert report.in_silence == 2 and report.at_video_change == 0


def test_a_gap_already_used_by_the_previous_change_is_not_used_twice():
    clips, report = plan_cuts(spans(0, 10, 14, 30), [(9.0, 11.0)])  # only one gap for two changes 4 s apart
    assert report.in_silence == 1 and report.at_video_change == 1
    assert clips[0].end == pytest.approx(9.25) and clips[1].start == pytest.approx(10.75)
    assert clips[1].end == pytest.approx(14) and clips[2].start == pytest.approx(14)


def test_silence_at_the_very_start_and_end_of_the_recording_is_trimmed_too():
    clips, _ = plan_cuts(spans(0, 10, 20), [(0.0, 1.5), (18.5, 20.0)])
    assert clips[0].start == pytest.approx(1.25) and clips[-1].end == pytest.approx(18.75)
    clips, _ = plan_cuts(spans(0, 20), [(0.0, 0.3), (19.9, 20.0)])  # too short to be a gap between recordings
    assert (clips[0].start, clips[0].end) == (0, 20)


def test_trimming_never_leaves_a_clip_shorter_than_the_minimum():
    clips, _ = plan_cuts(spans(0, 10, 20), [(9.0, 11.0), (11.2, 14.0), (14.1, 20.0)])
    assert all(c.end - c.start >= 0.5 for c in clips)

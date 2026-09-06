"""Edit-decision logic tests (pure python)."""

from contentforge.processing.segments import (
    Timeline,
    build_keep_ranges,
    invert_intervals,
    merge_intervals,
    shrink_silences,
)


def test_invert_intervals():
    assert invert_intervals([(2, 4)], 10) == [(0, 2), (4, 10)]
    assert invert_intervals([], 5) == [(0, 5)]
    assert invert_intervals([(0, 1), (9, 10)], 10) == [(1, 9)]
    assert invert_intervals([(1, 3), (2, 5)], 6) == [(0, 1), (5, 6)]


def test_merge_intervals():
    assert merge_intervals([(0, 1), (1.05, 2), (5, 6)], gap=0.1) == [(0, 2), (5, 6)]
    assert merge_intervals([(3, 4), (0, 1)]) == [(0, 1), (3, 4)]


def test_shrink_silences_padding_and_edges():
    sil = [(0.0, 1.0), (4.0, 6.0), (9.5, 10.0)]
    rem = shrink_silences(sil, padding=0.2, min_duration=0.5, duration=10)
    # leading silence trimmed from 0, interior padded on both sides, trailing to end
    assert rem[0] == (0.0, 0.8)
    assert rem[1] == (4.2, 5.8)
    assert rem[2][1] == 10.0
    # short silence ignored
    assert shrink_silences([(2, 2.4)], padding=0.1, min_duration=0.5, duration=5) == []


def test_build_keep_ranges_with_jump_cuts():
    silences = [(2.0, 5.0)]
    low_motion = [(2.5, 4.9)]
    keep = build_keep_ranges(
        10, silences, padding=0.15, min_silence=0.6, low_motion=low_motion, min_gap=1.0
    )
    assert keep[0][0] == 0.0
    assert keep[-1][1] == 10.0
    assert len(keep) == 2
    # dead region should be removed at least from 2.15 to 4.85
    assert keep[0][1] <= 2.15 + 1e-6 and keep[1][0] >= 4.85 - 1e-6


def test_build_keep_ranges_never_empty():
    assert build_keep_ranges(3, [(0, 3)], padding=0, min_silence=0.1) == [(0.0, 3)]


def test_timeline_mapping():
    tl = Timeline([(0, 2), (4, 6), (8, 9)])
    assert tl.output_duration == 5
    assert tl.to_output(1) == 1
    assert tl.to_output(3) is None
    assert tl.to_output(4) == 2
    assert tl.to_output(5.5) == 3.5
    assert tl.to_output(8.5) == 4.5
    assert tl.clamp_to_output(3) == 2  # snapped to end of first keep
    assert tl.clamp_to_output(7) == 4
    assert tl.remap_range(1, 5) == (1, 3)
    assert tl.remap_range(2.5, 3.5) is None  # fully inside a cut
    assert tl.removed_seconds == 4

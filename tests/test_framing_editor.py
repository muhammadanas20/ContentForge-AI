"""Content-aware 9:16 framing and the edit plan built on top of it."""

from __future__ import annotations

import pytest

from contentforge.models.schemas import Region, TextBox
from contentforge.processing.editor import (
    EditConfig,
    RenderSettings,
    card_rect,
    plan_edit,
    source_point_to_output,
    visible_output_region,
)
from contentforge.processing.framing import (
    FramingConfig,
    plan_framing,
    preservation_report,
    score_view,
)

pytestmark = pytest.mark.ffmpeg


# --------------------------------------------------------------- pure scoring
def _boxes(*rects: tuple[float, float, float, float]) -> list[TextBox]:
    return [TextBox(region=Region(*r), confidence=1.0) for r in rects]


def test_a_view_that_slices_text_in_half_scores_worse_than_one_that_does_not():
    text = _boxes((0.10, 0.20, 0.60, 0.06))
    clean = score_view(Region(0.05, 0.10, 0.75, 0.80), text_boxes=text)
    sliced = score_view(Region(0.40, 0.10, 0.55, 0.80), text_boxes=text)
    assert clean.text_cut < sliced.text_cut
    assert clean.total > sliced.total


def test_keeping_text_beats_zooming_into_the_cursor():
    text = _boxes((0.05, 0.10, 0.85, 0.08), (0.05, 0.30, 0.85, 0.30))
    wide = score_view(Region(0.02, 0.05, 0.94, 0.90), text_boxes=text, cursor=(0.9, 0.9))
    tight = score_view(Region(0.75, 0.70, 0.24, 0.28), text_boxes=text, cursor=(0.9, 0.9))
    assert tight.cursor >= wide.cursor  # the tight view does centre the cursor
    assert wide.total > tight.total  # ...but destroying the page is still worse


def test_preservation_report_flags_a_destructive_crop():
    text = _boxes((0.05, 0.10, 0.85, 0.08), (0.05, 0.40, 0.85, 0.20))
    good = preservation_report(Region(0.0, 0.0, 1.0, 1.0), text)
    bad = preservation_report(Region(0.70, 0.0, 0.30, 1.0), text)
    assert good["ok"] and good["kept"] > 0.95
    assert not bad["ok"] and bad["kept"] < 0.55


# ------------------------------------------------------- framing a recording
def test_framing_never_exceeds_the_configured_max_zoom(understanding):
    cfg = FramingConfig(max_zoom=1.8)
    for start in (0.5, 3.0, 6.0, 9.0):
        fp = plan_framing(understanding, start, start + 1.0, cfg)
        assert fp.view.zoom <= cfg.max_zoom + 1e-6
        assert 0.0 <= fp.view.x and fp.view.x + fp.view.w <= 1.0 + 1e-6
        assert 0.0 <= fp.view.y and fp.view.y + fp.view.h <= 1.0 + 1e-6


def test_framing_keeps_the_important_text_visible(understanding):
    fp = plan_framing(understanding, 1.0, 3.0, FramingConfig())
    boxes = understanding.text_boxes_at(2.0, window=1.5)
    report = preservation_report(fp.view.at(0.5), boxes)
    assert report["ok"], report
    assert report["kept"] >= 0.7


def test_a_tight_framing_wins_when_the_action_is_small_and_isolated(understanding):
    """Content-aware means the framing *changes* with what is happening."""
    zooms = []
    for a, b in ((0.5, 2.0), (5.8, 7.0), (7.3, 8.2)):
        zooms.append(plan_framing(understanding, a, b, FramingConfig()).view.zoom)
    assert max(zooms) > min(zooms), f"framing never adapted: {zooms}"


def test_camera_movement_is_smooth(understanding):
    fp = plan_framing(understanding, 2.0, 6.0, FramingConfig())
    keys = fp.view.x_keyframes
    if len(keys) < 3:
        pytest.skip("this shot needs no camera move")
    speeds = [abs(b[1] - a[1]) / max(1e-3, b[0] - a[0]) for a, b in zip(keys, keys[1:])]
    assert max(speeds) <= FramingConfig().max_pan_per_second + 1e-3


# ------------------------------------------------------------------ the plan
@pytest.fixture(scope="module")
def edit(understanding):
    return plan_edit(understanding, config=EditConfig(max_duration=40))


def test_dead_time_is_removed_but_the_result_is_held(edit, tutorial_recording, understanding):
    idle_start, idle_end = tutorial_recording.idle
    kept_in_idle = sum(
        max(0.0, min(s.src_end, idle_end) - max(s.src_start, idle_start)) for s in edit.shots
    )
    assert kept_in_idle < (idle_end - idle_start) * 0.5, "the long dead tail was not cut"
    assert edit.duration < understanding.duration
    assert edit.removed_seconds > 0


def test_the_viral_structure_is_present_and_ordered(edit):
    roles = [s.role for s in edit.shots]
    assert roles[0] == "hook"
    assert roles[-1] == "cta"
    order = {"hook": 0, "setup": 1, "demo": 2, "payoff": 3, "cta": 4}
    ranks = [order[r] for r in roles]
    assert ranks == sorted(ranks), roles
    hook_start, hook_end = edit.structure["hook"]
    assert hook_start == 0.0 and hook_end <= 2.6


def test_shots_are_contiguous_and_reasonably_paced(edit):
    assert edit.shots[0].out_start == 0.0
    for a, b in zip(edit.shots, edit.shots[1:]):
        assert abs(a.out_end - b.out_start) < 1e-6
    assert all(0.4 <= s.out_duration <= 5.5 for s in edit.shots)


def test_clicks_are_remapped_into_output_time(edit, tutorial_recording):
    assert edit.emphasis, "no click emphasis was planned"
    for a in edit.emphasis:
        assert 0.0 <= a.mid <= edit.duration + 0.5


def test_every_shot_is_framed_and_serialisable(edit):
    from contentforge.models.schemas import EditPlan

    assert all(s.view is not None for s in edit.shots)
    back = EditPlan.from_dict(edit.to_dict())
    assert len(back.shots) == len(edit.shots)
    assert back.duration == pytest.approx(edit.duration, abs=1e-3)


# --------------------------------------------------------------- composition
def test_card_geometry_stays_inside_the_canvas():
    rs = RenderSettings()
    rect = card_rect(rs, 1280 * 0.68, 720 * 0.9)
    assert 0 <= rect.x and rect.x + rect.w <= 1.0
    assert 0 <= rect.y and rect.y + rect.h <= 1.0
    assert rect.w > 0.8  # the recording still dominates the frame


def test_a_source_point_maps_into_the_visible_card(edit):
    rs = RenderSettings()
    shot = edit.shots[0]
    view = shot.view.at(0.0)
    centre = (view.x + view.w / 2, view.y + view.h / 2)
    mapped = source_point_to_output(shot, centre, shot.out_start, rs, (960, 540))
    assert mapped is not None
    card = visible_output_region(shot, shot.out_start, rs, (960, 540))
    assert card.contains_point(*mapped, margin=0.02)


def test_points_outside_the_framed_area_are_reported_as_invisible(edit):
    rs = RenderSettings()
    shot = edit.shots[0]
    view = shot.view.at(0.0)
    if view.w > 0.98 and view.h > 0.98:
        pytest.skip("this shot shows the whole frame")
    outside = (min(0.999, view.x + view.w + 0.05), min(0.999, view.y + view.h + 0.05))
    assert source_point_to_output(shot, outside, shot.out_start, rs, (960, 540)) is None

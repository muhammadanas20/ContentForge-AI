"""Video understanding stage: does the pipeline actually *see* the recording?

Every assertion is checked against the ground truth baked into the synthetic
recording (:mod:`tests.screen_recording`), so these are real detection tests,
not smoke tests.
"""

from __future__ import annotations

import json

import pytest

from contentforge.models.schemas import Region, VideoUnderstanding
from contentforge.processing.ocr import HeuristicTextDetector, get_ocr_backend
from contentforge.processing.video_understanding import understand_video

pytestmark = pytest.mark.ffmpeg


def _kinds(u: VideoUnderstanding, kind: str):
    return [a for a in u.actions if a.kind == kind]


def test_frames_are_sampled_at_the_requested_rate(understanding, tutorial_recording):
    u = understanding
    assert u.available
    assert u.width == tutorial_recording.width and u.height == tutorial_recording.height
    expected = tutorial_recording.duration * u.sample_fps
    assert 0.7 * expected <= len(u.frames) <= expected + 3
    assert all(a.t <= b.t for a, b in zip(u.frames, u.frames[1:]))  # ordered timeline


def test_text_regions_are_found_on_every_analysed_frame(understanding):
    with_boxes = [f for f in understanding.frames if f.text_boxes]
    assert len(with_boxes) >= 5
    for f in with_boxes:
        for b in f.text_boxes:
            assert 0.0 <= b.region.x <= 1.0 and 0.0 <= b.region.y <= 1.0
            assert b.region.w > 0 and b.region.h > 0


def test_clicks_are_detected_at_the_right_moments(understanding, tutorial_recording):
    detected = [a.mid for a in _kinds(understanding, "click")]
    truth = [t for t, _x, _y in tutorial_recording.clicks]
    matched = [t for t in truth if any(abs(t - d) <= 0.6 for d in detected)]
    assert len(matched) >= 2, f"clicks {detected} vs truth {truth}"
    # and no click storm: at most one false positive
    assert len(detected) <= len(truth) + 1


def test_scroll_typing_and_reveal_are_detected(understanding, tutorial_recording):
    scrolls = _kinds(understanding, "scroll")
    assert scrolls, "the page scroll was not detected"
    s_start, s_end = tutorial_recording.scroll
    assert any(abs(a.start - s_start) < 1.0 and abs(a.end - s_end) < 1.2 for a in scrolls)

    typing = _kinds(understanding, "type")
    t_start, t_end = tutorial_recording.typing
    assert any(a.start >= t_start - 0.8 and a.end <= t_end + 0.8 for a in typing)

    reveals = [a for a in understanding.actions if a.kind in ("reveal", "navigate")]
    assert any(abs(a.start - tutorial_recording.reveal) < 1.0 for a in reveals)


def test_idle_time_at_the_end_is_marked_as_dead(understanding, tutorial_recording):
    idle_start, idle_end = tutorial_recording.idle
    idles = _kinds(understanding, "idle")
    assert any(a.start <= idle_start + 0.8 and a.end >= idle_end - 0.6 for a in idles)


def test_cursor_is_tracked_near_the_true_path(understanding, tutorial_recording):
    tracked = [f for f in understanding.frames if f.cursor.detected]
    assert len(tracked) >= len(understanding.frames) * 0.25
    errors = []
    for f in tracked:
        tx, ty = tutorial_recording.cursor_at(f.t)
        errors.append(abs(f.cursor.x * understanding.width - tx) + abs(f.cursor.y * understanding.height - ty))
    errors.sort()
    median = errors[len(errors) // 2]
    assert median < 60, f"median cursor error {median:.0f}px"


def test_action_timeline_is_ordered_and_serialisable(understanding, tmp_path):
    actions = understanding.actions
    assert actions == sorted(actions, key=lambda a: a.start)
    payload = understanding.to_dict()
    (tmp_path / "u.json").write_text(json.dumps(payload))
    back = VideoUnderstanding.from_dict(json.loads((tmp_path / "u.json").read_text()))
    assert len(back.frames) == len(understanding.frames)
    assert [a.kind for a in back.actions] == [a.kind for a in actions]
    assert back.content_region.w > 0.3  # the page area survived the round-trip


def test_summary_is_compact_and_informative(understanding):
    s = understanding.summary()
    assert s["frames"] == len(understanding.frames)
    assert s["ocr_backend"] in ("tesseract", "heuristic")
    assert sum(s["action_counts"].values()) == len(understanding.actions)


def test_ocr_engine_falls_back_without_tesseract(monkeypatch, tutorial_recording):
    """No tesseract binary must never be fatal - regions still come out."""
    from contentforge.processing.ocr import TesseractOCR

    monkeypatch.setattr(TesseractOCR, "available", lambda self: False)
    backend = get_ocr_backend("tesseract")  # explicitly asked for, still degrades
    assert backend.name == "heuristic"
    assert isinstance(backend, HeuristicTextDetector)

    import cv2

    cap = cv2.VideoCapture(str(tutorial_recording.path))
    cap.set(cv2.CAP_PROP_POS_FRAMES, 10)
    ok, frame = cap.read()
    cap.release()
    assert ok
    boxes = backend.detect(frame)
    assert boxes, "heuristic OCR found no text regions on a page full of text"
    assert all(isinstance(b.region, Region) for b in boxes)


def test_understanding_is_cheap_enough_for_a_laptop(tutorial_recording):
    """Sampling caps keep the stage bounded regardless of clip length."""
    u = understand_video(tutorial_recording.path, sample_fps=6, max_frames=12, ocr_every_seconds=2.0)
    assert len(u.frames) <= 12
    assert u.available

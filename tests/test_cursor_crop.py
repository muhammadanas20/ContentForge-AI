"""Cursor detection, camera-follow planning and cursor-aware crop rendering."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

import contentforge.pipeline.steps as steps_mod
from contentforge.config.schema import CursorFollowConfig, VideoConfig
from contentforge.db import Database
from contentforge.pipeline import PipelineRunner
from contentforge.processing import (
    CursorTrack,
    CursorTracker,
    VideoEditor,
    analyse_video,
    detect_cursor_track,
    plan_crop,
    plan_cursor_crop,
)
from contentforge.processing.cursor import (
    CursorSample,
    evaluate_keyframes,
    follow_path,
    piecewise_linear_expression,
    simplify_keyframes,
)
from contentforge.utils.ffmpeg import MediaInfo
from tests.screen_recording import _draw_cursor, _draw_page, make_screen_recording

# --------------------------------------------------------------------------- pure unit tests


def _track(xs: list[float], detected: bool = True, dt: float = 0.1, w=1280, h=720) -> CursorTrack:
    tr = CursorTrack(width=w, height=h, duration=len(xs) * dt)
    for i, x in enumerate(xs):
        tr.samples.append(CursorSample(t=i * dt, x=x, y=0.5, detected=detected))
    return tr


def test_track_reliability_and_backfill():
    tr = _track([0.1] * 20, detected=False)
    assert tr.detections == 0 and not tr.is_reliable(1, 0.0)
    tr = _track([0.2] * 20)
    assert tr.coverage == 1.0 and tr.is_reliable(8, 0.15)
    tr = _track([0.2] * 20)
    for s in tr.samples[:15]:
        s.detected = False
    assert not tr.is_reliable(8, 0.15)  # 5 detections
    # backfill copies first detected position onto leading holds
    tr = _track([0.5, 0.5, 0.9, 0.9])
    tr.samples[0].detected = tr.samples[1].detected = False
    tr.backfill()
    assert tr.samples[0].x == 0.9 and tr.samples[1].x == 0.9
    # round trip through state.json representation
    d = tr.to_dict()
    back = CursorTrack.from_dict(d)
    assert back.detections == 2 and back.width == 1280 and back.samples[2].x == 0.9


def test_follow_path_deadzone_smoothing_speed_and_bounds():
    times = [i * 0.1 for i in range(60)]
    window = 0.3
    # 1) cursor wiggles inside the dead-zone -> window never moves
    xs = [0.5 + 0.02 * ((-1) ** i) for i in range(60)]
    p = follow_path(times, xs, window=window, deadzone=0.4, smoothing=0.8, max_speed=1.5)
    assert max(p) - min(p) < 1e-9
    # 2) jump to the far right -> window follows, stays within bounds, rate-limited
    xs = [0.1] * 10 + [0.98] * 50
    p = follow_path(times, xs, window=window, deadzone=0.3, smoothing=0.8, max_speed=0.5)
    assert 0.0 <= min(p) and max(p) <= 1.0 - window + 1e-9
    steps = np.abs(np.diff(p)) / 0.1
    assert steps.max() <= 0.5 + 1e-6  # max_speed respected
    assert p[-1] > 0.65  # eventually keeps 0.98 inside the window
    assert p[-1] + window >= 0.98
    # 3) more smoothing -> slower reaction
    lazy = follow_path(times, xs, window=window, deadzone=0.3, smoothing=0.95, max_speed=5)
    quick = follow_path(times, xs, window=window, deadzone=0.3, smoothing=0.5, max_speed=5)
    assert lazy[15] < quick[15]
    # 4) cursor at the extreme left/right edges clamps to [0, 1-window]
    p = follow_path(times, [0.0] * 60, window=window, start=0.7)
    assert abs(p[-1]) < 1e-9
    p = follow_path(times, [1.0] * 60, window=window, start=0.0)
    assert abs(p[-1] - (1 - window)) < 1e-9
    assert follow_path([], [], window=window) == []


def test_keyframe_simplification_and_expression():
    pts = [(i * 0.1, 100.0) for i in range(50)] + [(5 + i * 0.1, 100 + i * 10.0) for i in range(20)]
    simple = simplify_keyframes(pts, tolerance=0.5)
    assert len(simple) <= 4 and simple[0] == pts[0] and simple[-1] == pts[-1]
    segs = [[(0.0, 100.0), (1.0, 200.0)], [(2.0, 50.0), (3.0, 50.0)]]
    expr = piecewise_linear_expression(segs, var="t")
    assert "gte(t" in expr and "lt(t" in expr
    # python twin: interpolate inside, hold across the gap (1..2) and after the end
    assert evaluate_keyframes(segs, 0.5) == pytest.approx(150.0)
    assert evaluate_keyframes(segs, 1.5) == pytest.approx(200.0)
    assert evaluate_keyframes(segs, 2.5) == pytest.approx(50.0)
    assert evaluate_keyframes(segs, 9.0) == pytest.approx(50.0)
    assert evaluate_keyframes(segs, -1.0) == pytest.approx(100.0)
    assert evaluate_keyframes([], 1.0) == 0.0
    assert piecewise_linear_expression([[(0.0, 7.0)]]) == "7.000"


def test_plan_cursor_crop_fallbacks_and_bounds():
    info = MediaInfo(path=Path("x"), width=1280, height=720, duration=10)
    base = plan_crop(info, 1080, 1920, "smart", center_x=0.5)
    cfg = CursorFollowConfig()
    # unreliable track -> identical static plan
    weak = _track([0.9] * 5)
    assert plan_cursor_crop(base, weak, [(0, 10)], cfg) is base
    # cursor never moves -> static crop but positioned on the cursor
    still = _track([0.9] * 100)
    p = plan_cursor_crop(base, still, [(0, 10)], cfg)
    assert not p.dynamic and p.x == 1280 - base.w
    # moving cursor -> dynamic plan, every keyframe inside [0, sw - w]
    xs = [0.05] * 30 + [0.95] * 40 + [0.5] * 30
    p = plan_cursor_crop(base, _track(xs), [(0, 3), (5, 10)], cfg)
    assert p.dynamic and p.mode == "cursor" and len(p.x_keyframes) == 2
    for seg in p.x_keyframes:
        for t, x in seg:
            assert 0 <= x <= 1280 - base.w
            assert 0 <= t <= 8.0 + 1e-6  # output time
    assert p.x_at(0.0) == pytest.approx(0.0, abs=1.0)
    # a long cut (2 s removed) re-centres on the cursor: no visible pan after the cut
    assert p.x_at(3.0) == pytest.approx(1280 - base.w, abs=1.0)
    # a short cut (0.4 s removed) keeps panning smoothly across the cut
    p2 = plan_cursor_crop(base, _track(xs), [(0, 3), (3.4, 10)], cfg)
    assert p2.x_at(2.99) == pytest.approx(p2.x_at(3.0), abs=base.w * 0.02)
    assert p2.x_at(3.5) > p2.x_at(3.0)  # ... and then catches up
    assert "clip(" in p.ffmpeg and p.ffmpeg.startswith(f"crop={base.w}:{base.h}:x='")
    d = p.to_dict()
    assert d["dynamic"] and d["x_min"] >= 0 and d["x_max"] <= 1280 - base.w
    # source narrower than crop -> static
    tall = MediaInfo(path=Path("x"), width=1080, height=2400)
    tb = plan_crop(tall, 1080, 1920, "smart")
    assert plan_cursor_crop(tb, _track(xs, w=1080, h=2400), [(0, 10)], cfg) is tb


def test_tracker_detects_synthetic_cursor_frames():
    """Feed frames straight into the tracker (no video file): cursor moves, caret blinks."""
    w, h = 1280, 720
    tracker = CursorTracker(w, h)
    truth = []
    for i in range(30):
        x = 200 + i * 25
        y = 300 + (i % 4) * 3
        frame = _draw_page(w, h, 0, caret_on=i % 2 == 0, clicked=None)
        _draw_cursor(frame, x, y)
        tracker.update(frame, i / 10)
        truth.append((x, y))
    tr = tracker.track
    assert tr.detections >= 20
    errs = [abs(s.x * w - truth[i][0]) for i, s in enumerate(tr.samples) if s.detected and i > 0]
    assert np.median(errs) < 25  # centre of bbox vs hotspot at the arrow tip
    # a scrolling page (everything changes) must not produce a bogus detection
    before = tr.detections
    frame = _draw_page(w, h, 300, caret_on=False, clicked=None)
    _draw_cursor(frame, 950, 300)
    tracker.update(frame, 3.1)
    assert tr.detections == before and tr.skipped_frames >= 1
    # static frames -> hold last position, not detected
    s = tracker.update(frame, 3.2)
    assert not s.detected and abs(s.x * w - 950) < 60 or not s.detected


# --------------------------------------------------------------------------- integration


@pytest.fixture(scope="module")
def recording(tmp_path_factory, request):
    from contentforge.utils.ffmpeg import FFmpeg, ffmpeg_available

    if not ffmpeg_available():
        pytest.skip("ffmpeg not available")
    out = tmp_path_factory.mktemp("rec") / "studenttools.pk - pdf to word.mp4"
    return make_screen_recording(out, ffmpeg=FFmpeg())


@pytest.mark.ffmpeg
def test_detect_cursor_track_on_generated_recording(recording):
    tr = detect_cursor_track(recording.path, sample_fps=10)
    assert tr.width == 1280 and tr.height == 720
    assert tr.is_reliable(8, 0.15), (tr.detections, tr.coverage)
    det = [s for s in tr.samples if s.detected]
    ex = [abs(s.x * 1280 - recording.cursor_at(s.t)[0]) for s in det]
    ey = [abs(s.y * 720 - recording.cursor_at(s.t)[1]) for s in det]
    assert np.median(ex) < 20 and np.median(ey) < 30
    assert max(ex) < 60 and max(ey) < 60  # no false positive far from the cursor
    # the page scroll (8.0-8.6 s) must be skipped, not tracked
    assert tr.skipped_frames >= 1
    # one-pass analysis yields the same track through analyse_video
    fa = analyse_video(recording.path, sample_fps=2, cursor_tracker=CursorTracker(1280, 720))
    assert fa.cursor is not None and fa.cursor.detections >= tr.detections - 3
    assert fa.times and fa.motion


@pytest.mark.ffmpeg
def test_cursor_crop_render_keeps_cursor_visible(tmp_path, ffmpeg, recording):
    """Real FFmpeg render with a panning crop, verified frame-by-frame against the source."""
    tr = detect_cursor_track(recording.path, sample_fps=10)
    info = MediaInfo(path=recording.path, width=1280, height=720, duration=recording.duration)
    base = plan_crop(info, 1080, 1920, "smart", center_x=0.5)
    keep = [(0.0, 5.0), (6.5, 12.0)]
    plan = plan_cursor_crop(base, tr, keep, CursorFollowConfig())
    assert plan.dynamic
    cfg = VideoConfig(width=540, height=960, fps=24, crf=30, preset="ultrafast")
    out = tmp_path / "cut.mp4"
    VideoEditor(cfg, ffmpeg).render_cut(
        recording.path, out, keep=keep, crop=plan, zoom_pulses=[], output_duration=10.5
    )
    pi = ffmpeg.probe(out)
    assert pi.width == 540 and pi.height == 960 and 10.0 <= pi.duration <= 11.0

    def to_src(t: float) -> float:
        off = 0.0
        for s, e in keep:
            if t < off + (e - s):
                return s + (t - off)
            off += e - s
        return keep[-1][1]

    cap = cv2.VideoCapture(str(out))
    src = cv2.VideoCapture(str(recording.path))
    inside = total = 0
    prev_x = None
    for t_out in np.arange(0.3, 10.4, 0.35):
        n = int(round(t_out * 24))
        cap.set(cv2.CAP_PROP_POS_FRAMES, n)
        ok, frame = cap.read()
        assert ok
        ts = to_src(n / 24)
        x = plan.x_at(n / 24)
        assert 0 <= x <= 1280 - base.w
        # rendered frame must equal the source cropped at the planned x (tolerance for
        # codecs and for +-1 source frame of 30->24 fps resampling during the page scroll)
        xi = int(round(x))
        diffs = []
        for k in (-1, 0, 1):
            src.set(cv2.CAP_PROP_POS_FRAMES, max(0, int(round(ts * 30)) + k))
            ok, sf = src.read()
            assert ok
            ref = cv2.resize(sf[:, xi : xi + base.w], (540, 960), interpolation=cv2.INTER_AREA)
            diffs.append(float(np.abs(ref.astype(int) - frame.astype(int)).mean()))
        assert min(diffs) < 12, (t_out, diffs)
        gx, _gy = recording.cursor_at(ts)
        total += 1
        if x <= gx <= x + base.w:
            inside += 1
        if prev_x is not None:
            # smooth: never more than max_speed (1.5 widths/s) between probes
            assert abs(x - prev_x) <= 1.5 * 0.35 * 1280 + 1
        prev_x = x
    cap.release()
    src.release()
    # the cursor is inside the crop window in the vast majority of probes
    assert inside / total >= 0.85, (inside, total)


@pytest.mark.ffmpeg
def test_pipeline_uses_cursor_crop_and_falls_back(fast_settings, ffmpeg, recording, monkeypatch):
    from tests.test_pipeline import FakeTranscriber, FakeTTS

    monkeypatch.setattr(steps_mod, "Transcriber", FakeTranscriber)
    monkeypatch.setattr(steps_mod, "get_tts_engine", lambda cfg: FakeTTS(ffmpeg, duration=9.0))
    s = fast_settings
    s.video.crop.mode = "smart"
    s.video.crop.follow_cursor.enabled = True
    db = Database(s.paths.db)
    src = s.paths.input / recording.path.name
    src.write_bytes(recording.path.read_bytes())
    runner = PipelineRunner(s, db, ffmpeg)
    ctx = runner.create_job(src)
    result = runner.run(ctx)
    assert result.status == "archived", result.error
    assert ctx.data["crop"]["mode"] == "cursor" and ctx.data["crop"]["dynamic"]
    assert ctx.data["edit"]["cursor_track"]["samples"]
    steps = db.get_steps(ctx.job_id)
    assert steps["analyse"]["outputs"]["cursor_detections"] >= 8
    video = result.output_dir / f"{ctx.slug}.mp4"
    info = ffmpeg.probe(video)
    assert info.width == 540 and info.height == 960 and info.has_audio
    db.close()

    # fallback: follow disabled -> classic static smart crop, pipeline still fine
    s.video.crop.follow_cursor.enabled = False
    src = s.paths.input / "static - demo.mp4"
    src.write_bytes(recording.path.read_bytes())
    db = Database(s.paths.db)
    runner = PipelineRunner(s, db, ffmpeg)
    ctx2 = runner.create_job(src)
    r2 = runner.run(ctx2)
    assert r2.status == "archived", r2.error
    assert ctx2.data["crop"]["mode"] == "smart" and "dynamic" not in ctx2.data["crop"]
    assert ctx2.data["edit"]["cursor_track"] is None
    db.close()

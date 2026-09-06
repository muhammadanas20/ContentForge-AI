"""**Video understanding** - the stage that makes v0.3 content-aware.

One decode pass over the recording produces a
:class:`~contentforge.models.schemas.VideoUnderstanding`:

* sampled frames (configurable ``sample_fps``) with motion, sharpness and the
  bounding box of what changed;
* OCR / text-region boxes on a slower cadence (``ocr_every_seconds``);
* cursor positions (shared :class:`~contentforge.processing.cursor.CursorTracker`);
* scroll estimation (vertical phase correlation);
* an **action timeline**: clicks, typing, scrolling, navigation, result reveals
  and idle (dead) time;
* the **content region**: where the actual website lives inside the screen
  recording (browser chrome, OS bars and desktop margins excluded).

Everything is CPU-only and bounded: frames are analysed at ``work_width`` px
(default 960) and the number of sampled frames is capped, so a 76 s 1080p
recording is understood in a few seconds on a laptop without a GPU, a large
language model or a heavy CV model.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from contentforge.log import get_logger
from contentforge.models.schemas import (
    ActionEvent,
    CursorPoint,
    FrameObservation,
    Region,
    TextBox,
    VideoUnderstanding,
)
from contentforge.processing.cursor import CursorTracker, detect_click_events
from contentforge.processing.ocr import get_ocr_backend, text_mass

log = get_logger("understanding")


# --------------------------------------------------------------------------- main entry
def understand_video(
    path: str | Path,
    *,
    sample_fps: float = 4.0,
    work_width: int = 960,
    ocr_every_seconds: float = 1.5,
    ocr_engine: str = "auto",
    ocr_lang: str = "eng",
    max_frames: int = 400,
    track_cursor: bool = True,
    cursor_min_px: int = 8,
    cursor_max_px: int = 64,
    website: str = "",
    website_context: str = "",
    scene_change_motion: float = 12.0,
    idle_motion: float = 1.2,
    min_idle_seconds: float = 0.8,
    enrich_vision: bool = False,
    llm_client: Any = None,
) -> VideoUnderstanding:
    """Analyse ``path`` and return everything the pipeline knows about it."""
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"OpenCV could not open {path}")
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    duration = total / fps if fps else 0.0

    # keep the number of analysed frames bounded on long recordings (8 GB laptop)
    if duration > 0 and max_frames > 0:
        sample_fps = min(sample_fps, max(0.5, max_frames / duration))
    step = max(1, int(round(fps / max(0.2, sample_fps))))

    ocr = get_ocr_backend(ocr_engine, lang=ocr_lang)
    tracker = (
        CursorTracker(width, height, work_width=work_width, min_size_px=cursor_min_px, max_size_px=cursor_max_px)
        if track_cursor and width and height
        else None
    )

    u = VideoUnderstanding(
        width=width,
        height=height,
        duration=duration,
        fps=fps,
        sample_fps=fps / step if step else sample_fps,
        ocr_backend=getattr(ocr, "name", "none"),
        website=website,
        website_context=website_context,
    )

    scale = work_width / max(1, width)
    small_size = (work_width, max(1, int(height * scale))) if scale < 1 else (width, height)
    prev_small: np.ndarray | None = None
    last_ocr_t = -1e9
    idx = 0
    while True:
        ok = cap.grab()
        if not ok:
            break
        if idx % step:
            idx += 1
            continue
        ok, frame = cap.retrieve()
        if not ok:
            break
        t = idx / fps if fps else float(idx)
        small = cv2.resize(frame, small_size, interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        obs = FrameObservation(t=t)
        obs.sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())

        if prev_small is not None:
            diff = cv2.absdiff(gray, prev_small)
            obs.motion = float(diff.mean())
            obs.change = _change_region(diff, gray.shape[1], gray.shape[0])
            obs.scroll_dy = _estimate_scroll(prev_small, gray)
            obs.scene_change = obs.motion >= scene_change_motion or (
                obs.change is not None and obs.change.area > 0.45 and obs.motion > 2.5
            )
        prev_small = gray

        if tracker is not None:
            tracker.update(frame, t)
            s = tracker.track.samples[-1] if tracker.track.samples else None
            if s is not None:
                obs.cursor = CursorPoint(t=t, x=s.x, y=s.y, detected=bool(s.detected))

        if t - last_ocr_t >= ocr_every_seconds or obs.scene_change:
            boxes = ocr.detect(small)
            obs.text_boxes = boxes
            obs.text_mass = text_mass(boxes)
            last_ocr_t = t

        u.frames.append(obs)
        idx += 1
    cap.release()

    if tracker is not None:
        tracker.track.backfill()
        _apply_cursor_track(u, tracker)

    if not u.duration and u.frames:
        u.duration = u.frames[-1].t
    _mark_cursor_motion(u)
    u.content_region = detect_content_region(u)
    u.actions = build_action_timeline(
        u, idle_motion=idle_motion, min_idle_seconds=min_idle_seconds
    )
    if enrich_vision:
        enrich_with_vision(u, path, llm=llm_client)
    log.info(
        "Understanding: %d frames @%.1f fps, OCR=%s (%s), %d actions, content=%s",
        len(u.frames),
        u.sample_fps,
        u.ocr_backend,
        "text" if u.has_ocr_text else "boxes-only",
        len(u.actions),
        u.content_region.to_dict(),
    )
    return u


# --------------------------------------------------------------------------- signals
def _change_region(diff: np.ndarray, w: int, h: int, thresh: int = 18) -> Region | None:
    """Bounding box of changed pixels (None when nothing meaningful changed)."""
    _, mask = cv2.threshold(diff, thresh, 255, cv2.THRESH_BINARY)
    if cv2.countNonZero(mask) < max(12, int(0.00005 * w * h)):
        return None
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5)))
    ys, xs = np.nonzero(mask)
    if xs.size == 0:
        return None
    x0, x1 = int(xs.min()), int(xs.max())
    y0, y1 = int(ys.min()), int(ys.max())
    return Region.from_px(x0, y0, max(1, x1 - x0), max(1, y1 - y0), w, h, kind="change", score=1.0)


def _estimate_scroll(prev_gray: np.ndarray, gray: np.ndarray) -> float:
    """Vertical shift between two frames as a fraction of height (+ = content moved up)."""
    try:
        a = np.float32(prev_gray)
        b = np.float32(gray)
        (_dx, dy), response = cv2.phaseCorrelate(a, b)
    except cv2.error:  # pragma: no cover - defensive
        return 0.0
    if response < 0.08:
        return 0.0
    return float(-dy) / max(1, gray.shape[0])


def _apply_cursor_track(u: VideoUnderstanding, tracker: CursorTracker) -> None:
    """Copy backfilled cursor samples onto the frame observations."""
    samples = tracker.track.samples
    if not samples:
        return
    by_time = {round(s.t, 3): s for s in samples}
    for f in u.frames:
        s = by_time.get(round(f.t, 3))
        if s is None:
            s = min(samples, key=lambda c: abs(c.t - f.t))
        f.cursor = CursorPoint(t=f.t, x=s.x, y=s.y, detected=bool(s.detected))


def _mark_cursor_motion(u: VideoUnderstanding, move_threshold: float = 0.006) -> None:
    prev: CursorPoint | None = None
    for f in u.frames:
        c = f.cursor
        if c is None:
            continue
        if prev is not None:
            c.moving = (abs(c.x - prev.x) + abs(c.y - prev.y)) > move_threshold
        prev = c


# --------------------------------------------------------------------------- content region
def detect_content_region(u: VideoUnderstanding, quantile: float = 0.02) -> Region:
    """Where the *website* lives inside the recording.

    Screen recordings contain OS panels, browser chrome and (often) desktop
    margins.  The content region is the box that holds the text/UI the viewer
    cares about: the robust bounding box of all detected text over time, grown
    slightly and unioned with the region where changes happen.
    """
    xs0: list[float] = []
    ys0: list[float] = []
    xs1: list[float] = []
    ys1: list[float] = []
    for f in u.frames:
        for b in f.text_boxes:
            if b.region.area > 0.5:  # a full-frame blob is not a text line
                continue
            xs0.append(b.region.x)
            ys0.append(b.region.y)
            xs1.append(b.region.x2)
            ys1.append(b.region.y2)
    changes = [f.change for f in u.frames if f.change is not None and f.change.area < 0.9]
    if not xs0 and not changes:
        return Region(0, 0, 1, 1, kind="content", score=0.2)

    def q(vals: list[float], p: float, default: float) -> float:
        return float(np.quantile(vals, p)) if vals else default

    x0 = q(xs0, quantile, 0.0)
    y0 = q(ys0, quantile, 0.0)
    x1 = q(xs1, 1 - quantile, 1.0)
    y1 = q(ys1, 1 - quantile, 1.0)
    if changes:
        cx0 = float(np.quantile([c.x for c in changes], 0.05))
        cy0 = float(np.quantile([c.y for c in changes], 0.05))
        cx1 = float(np.quantile([c.x2 for c in changes], 0.95))
        cy1 = float(np.quantile([c.y2 for c in changes], 0.95))
        x0, y0 = min(x0, cx0), min(y0, cy0)
        x1, y1 = max(x1, cx1), max(y1, cy1)
    pad = 0.01
    x0 = max(0.0, x0 - pad)
    y0 = max(0.0, y0 - pad)
    x1 = min(1.0, x1 + pad)
    y1 = min(1.0, y1 + pad)
    if x1 - x0 < 0.25 or y1 - y0 < 0.25:  # implausible - keep the whole frame
        return Region(0, 0, 1, 1, kind="content", score=0.3)
    return Region(x0, y0, x1 - x0, y1 - y0, kind="content", score=0.9)


# --------------------------------------------------------------------------- action timeline
def build_action_timeline(
    u: VideoUnderstanding,
    *,
    idle_motion: float = 1.2,
    min_idle_seconds: float = 0.8,
    scroll_threshold: float = 0.012,
) -> list[ActionEvent]:
    """Turn per-frame signals into a human-readable list of actions."""
    events: list[ActionEvent] = []
    dt = 1.0 / max(0.2, u.sample_fps)

    # ---- clicks (cursor stationary + local change) -------------------------
    events.extend(detect_click_events(u, dt))

    # ---- scrolling ---------------------------------------------------------
    run_start: float | None = None
    run_dy = 0.0
    for f in u.frames:
        if abs(f.scroll_dy) >= scroll_threshold:
            run_start = f.t if run_start is None else run_start
            run_dy += f.scroll_dy
        elif run_start is not None:
            if f.t - run_start >= dt:
                events.append(
                    ActionEvent(
                        run_start,
                        f.t,
                        "scroll",
                        region=u.content_region,
                        label="scrolls the page " + ("down" if run_dy > 0 else "up"),
                        confidence=min(1.0, abs(run_dy) * 4),
                    )
                )
            run_start, run_dy = None, 0.0
    if run_start is not None and u.duration - run_start >= dt:
        events.append(
            ActionEvent(
                run_start,
                u.duration,
                "scroll",
                region=u.content_region,
                label="scrolls the page",
                confidence=0.5,
            )
        )

    # ---- typing (repeated small, wide changes inside one field) ------------
    runs: list[list[FrameObservation]] = []
    current: list[FrameObservation] = []
    for f in u.frames:
        ch = f.change
        typing_like = (
            ch is not None
            and 0.00005 < ch.area < 0.012
            and ch.h < 0.10  # one line of a form field, not a panel
            and 0.005 < f.motion < 6.0
        )
        if typing_like and ch is not None:
            if current:
                union = current[0].change
                for prev in current[1:]:
                    union = union.union(prev.change)  # type: ignore[union-attr]
                union = union.union(ch)  # type: ignore[union-attr]
                near_gap = f.t - current[-1].t <= 3 * dt
                # text keeps its baseline; a drifting baseline means the mouse
                baseline = float(np.median([p.change.y + p.change.h / 2 for p in current if p.change]))
                on_baseline = abs((ch.y + ch.h / 2) - baseline) <= 0.012
                if not (near_gap and on_baseline and union.area < 0.06):
                    runs.append(current)
                    current = []
            current.append(f)
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    for run in runs:
        if len(run) < 4 or run[-1].t - run[0].t < 0.8:
            continue
        region = run[0].change
        for f in run[1:]:
            region = region.union(f.change)  # type: ignore[union-attr]
        if region is None or region.w <= region.h:
            continue  # characters accumulate along a line
        # A pointer sliding across the page produces the same small changes as
        # typing. Two things tell them apart: text sits on a fixed baseline,
        # and the mouse is parked while someone types.
        centres_y = [f.change.y + f.change.h / 2 for f in run if f.change]
        if float(np.std(centres_y)) > 0.008:
            continue
        # while someone types the mouse is parked; a pointer trail travels
        track = [(f.t, f.cursor.x, f.cursor.y) for f in run if f.cursor.detected]
        if len(track) >= 3:
            path = sum(
                abs(b[1] - a[1]) + abs(b[2] - a[2]) for a, b in zip(track, track[1:])
            )
            span = max(1e-3, track[-1][0] - track[0][0])
            if path / span > 0.18:
                continue  # that was the mouse pointer, not a keyboard
        events.append(
            ActionEvent(
                run[0].t,
                run[-1].t + dt,
                "type",
                region=region.clamped(),  # type: ignore[union-attr]
                label=u.text_at_region(region, run[-1].t),  # type: ignore[arg-type]
                confidence=0.55,
            )
        )

    # ---- navigation / reveal (significant visual change) -------------------
    click_times = [e.mid for e in events if e.kind == "click"]
    motions = [f.motion for f in u.frames if f.motion > 0]
    med = float(np.median(motions)) if motions else 0.0
    scroll_spans = [(e.start, e.end) for e in events if e.kind == "scroll"]
    candidates: list[tuple[float, FrameObservation]] = []
    for f in u.frames:
        if f.change is None:
            continue
        if any(a - dt <= f.t <= b + dt for a, b in scroll_spans):
            continue  # scrolling changes everything; it is already an event
        significance = f.change.area * max(0.0, f.motion)
        if f.scene_change or (f.change.area >= 0.02 and f.motion >= max(0.6, med * 1.8)):
            candidates.append((significance, f))
    candidates.sort(key=lambda c: -c[0])
    top = sorted(candidates[:12], key=lambda c: c[1].t)
    for significance, f in top:
        region = f.change or u.content_region
        after_click = any(0.0 <= f.t - ct <= 3.0 for ct in click_times)
        big = region.area >= 0.45 or f.scene_change
        kind = "navigate" if (big and not after_click) else "reveal"
        if events and events[-1].kind in ("navigate", "reveal") and f.t - events[-1].end < 0.5:
            events[-1].end = f.t + dt
            continue
        events.append(
            ActionEvent(
                f.t,
                f.t + max(dt, 0.4),
                kind,
                region=region.clamped(),
                label=u.text_at_region(region, f.t + 0.5),
                confidence=min(1.0, 0.35 + significance * 3),
            )
        )

    # ---- idle / dead time --------------------------------------------------
    idle_start: float | None = None
    for f in u.frames:
        if f.motion <= idle_motion and (f.cursor is None or not f.cursor.moving):
            idle_start = f.t if idle_start is None else idle_start
        else:
            if idle_start is not None and f.t - idle_start >= min_idle_seconds:
                events.append(
                    ActionEvent(idle_start, f.t, "idle", region=None, label="", confidence=0.9)
                )
            idle_start = None
    if idle_start is not None and u.duration - idle_start >= min_idle_seconds:
        events.append(ActionEvent(idle_start, u.duration, "idle", None, "", 0.9))

    events.sort(key=lambda e: (e.start, e.kind))
    return events


# --------------------------------------------------------------------------- summaries
def summarise(u: VideoUnderstanding, max_actions: int = 12) -> dict:
    """Compact, JSON-friendly summary used in manifests and logs."""
    kinds: dict[str, int] = {}
    for a in u.actions:
        kinds[a.kind] = kinds.get(a.kind, 0) + 1
    return {
        "frames": len(u.frames),
        "sample_fps": round(u.sample_fps, 2),
        "ocr_backend": u.ocr_backend,
        "ocr_text": u.has_ocr_text,
        "actions": kinds,
        "content_region": u.content_region.to_dict(),
        "top_text": u.dominant_texts(8),
        "highlights": [a.to_dict() for a in u.actions if a.kind != "idle"][:max_actions],
    }


def important_text_boxes(u: VideoUnderstanding, t: float, window: float = 1.5) -> list[TextBox]:
    """Text boxes near ``t``, largest first - what framing must try to preserve."""
    boxes = u.text_boxes_at(t, window)
    return sorted(boxes, key=lambda b: -(b.region.area * max(0.2, b.confidence)))


# --------------------------------------------------------------------------- AI vision enrichment
def enrich_with_vision(
    u: VideoUnderstanding,
    path: str | Path,
    llm: Any = None,
    max_keyframes: int = 4,
) -> VideoUnderstanding:
    """Optionally query Gemini Vision with keyframes from the recording.

    Enriches the understanding with AI insights, semantic frame descriptions,
    and higher-level context. Gracefully no-ops if no vision-capable LLM is configured.
    """
    if llm is None:
        try:
            from contentforge.ai.llm import LLMClient
            llm = LLMClient()
        except Exception:
            return u

    if not getattr(llm, "supports_vision", False):
        return u

    if not u.frames or u.duration <= 0:
        return u

    timestamps: list[float] = [min(0.5, u.duration * 0.1)]
    for a in u.actions:
        if a.kind in ("click", "type", "reveal", "navigate") and a.confidence >= 0.5:
            if not any(abs(a.mid - t) < 1.5 for t in timestamps):
                timestamps.append(a.mid)
        if len(timestamps) >= max_keyframes - 1:
            break
    end_t = max(0.0, u.duration - 1.0)
    if not any(abs(end_t - t) < 1.5 for t in timestamps):
        timestamps.append(end_t)
    timestamps = sorted(timestamps)[:max_keyframes]

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return u

    fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
    encoded_frames: list[bytes] = []
    actual_ts: list[float] = []

    for t in timestamps:
        frame_idx = int(round(t * fps))
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        ret, frame = cap.read()
        if not ret or frame is None:
            continue
        h, w = frame.shape[:2]
        if w > 960:
            scale = 960 / w
            frame = cv2.resize(frame, (960, max(1, int(h * scale))))
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        if ok:
            encoded_frames.append(buf.tobytes())
            actual_ts.append(t)
    cap.release()

    if not encoded_frames:
        return u

    system_prompt = (
        "You are an expert AI video analyst for short-form video production (Instagram Reels). "
        "Analyze these sequential keyframes from a screen recording of a website or web app. "
        "Respond strictly with valid JSON conforming to this schema:\n"
        "{\n"
        '  "summary": "1-2 sentence description of what this website/tool does and the user journey shown",\n'
        '  "insights": ["3-5 punchy value propositions or key features visible"],\n'
        '  "frame_descriptions": [\n'
        '    {"timestamp": 0.5, "description": "Brief description of this screen state"}\n'
        "  ]\n"
        "}"
    )
    user_prompt = (
        f"Website hint: {u.website or 'Unknown'}\n"
        f"Detected text highlights: {', '.join(u.dominant_texts(6))}\n"
        f"Keyframe timestamps: {actual_ts}"
    )

    try:
        data = llm.complete_vision_json(system_prompt, user_prompt, encoded_frames)
        if isinstance(data, dict):
            if "summary" in data and isinstance(data["summary"], str):
                u.notes.append(f"AI Summary: {data['summary']}")
            if "insights" in data and isinstance(data["insights"], list):
                u.ai_insights.extend(str(x) for x in data["insights"] if x)
            if "frame_descriptions" in data and isinstance(data["frame_descriptions"], list):
                for fd in data["frame_descriptions"]:
                    if isinstance(fd, dict) and "description" in fd:
                        ts = float(fd.get("timestamp", 0.0))
                        desc = str(fd["description"]).strip()
                        target_f = u.frame_at(ts)
                        if target_f and desc:
                            target_f.semantic_description = desc
            log.info("Gemini Vision enriched video understanding with %d insights", len(u.ai_insights))
    except Exception as exc:
        log.warning("Gemini Vision enrichment encountered error: %s", exc)

    return u

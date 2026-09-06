"""Mouse-cursor detection, tracking and camera-follow planning for screen recordings.

Why a dedicated module
----------------------
Screen recordings are usually 16:9 and a Reel is 9:16, so at best ~32 % of the
width survives the crop.  The interesting part of a tutorial is almost always
where the mouse is.  Instead of a single static crop centre this module
produces a *time-varying* horizontal crop position that follows the cursor
smoothly, so the viewer always sees what the presenter is pointing at.

How detection works (no model, no template file needed)
-------------------------------------------------------
Frames are sampled at ``sample_fps`` and downscaled.  Between two consecutive
samples the cursor shows up as a *small, compact* blob in the frame
difference; the blob at the new position looks like a cursor (high local
contrast with both bright and dark pixels), the blob left behind at the old
position looks like whatever content was underneath.  Candidates are scored
by

* size prior          - cursor-like bounding box (8..64 px at source scale),
* appearance          - local contrast and presence of both tones,
* template similarity - normalised cross-correlation with the last accepted
  cursor patch (learned online, so it adapts to arrow / hand / I-beam),
* continuity          - distance from the previous position,
* "left-behind" rule  - a blob sitting exactly on the previous position is
  usually the hole the cursor left, so it is penalised.

Frames with many changed regions (scrolling, page loads, video playback) are
skipped: the cursor cannot be separated reliably there and holding the last
position is the visually correct behaviour anyway.

When nothing is detected the tracker keeps the last position - a stationary
cursor produces no frame difference, which is exactly what "hold" models.
If too few detections are made over the whole clip the caller falls back to
the existing static smart/centre crop.

Camera follow
-------------
:func:`follow_path` turns the raw cursor track into a crop-window position:
a dead-zone (the cursor may roam inside the middle part of the window without
moving it), exponential smoothing and a maximum pan speed remove jitter and
jumps; the window is always clamped to the frame.  The result is simplified to
a handful of keyframes and rendered by FFmpeg's ``crop`` filter through a
piecewise-linear ``x`` expression in *output* time, so cuts, subtitles,
progress bar and audio stay perfectly in sync (nothing else in the pipeline
changes).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from contentforge.log import get_logger

log = get_logger("cursor")

Keyframe = tuple[float, float]  # (time seconds, value)


# --------------------------------------------------------------------------- data
@dataclass
class CursorSample:
    """One tracked cursor position (fractions of the frame, source time)."""

    t: float
    x: float
    y: float
    detected: bool
    score: float = 0.0


@dataclass
class CursorTrack:
    """Cursor positions over a clip, at the tracker's sample rate."""

    samples: list[CursorSample] = field(default_factory=list)
    width: int = 0
    height: int = 0
    duration: float = 0.0
    skipped_frames: int = 0  # frames with too much change to isolate a cursor

    @property
    def detections(self) -> int:
        return sum(1 for s in self.samples if s.detected)

    @property
    def coverage(self) -> float:
        """Fraction of samples with an explicit detection (0..1)."""
        return self.detections / len(self.samples) if self.samples else 0.0

    def is_reliable(self, min_detections: int, min_coverage: float) -> bool:
        """Enough evidence to trust the track for a moving crop?"""
        if not self.samples or self.detections < min_detections:
            return False
        if self.coverage < min_coverage:
            return False
        return True

    def backfill(self) -> None:
        """Copy the first detected position onto the leading undetected samples.

        No detection before the first one means the cursor did not move, so
        the place where it was first *seen* moving is where it was all along.
        """
        first = next((s for s in self.samples if s.detected), None)
        if first is None:
            return
        for s in self.samples:
            if s is first:
                break
            s.x, s.y = first.x, first.y

    def positions(self) -> tuple[list[float], list[float], list[float]]:
        """``(times, xs, ys)`` - convenience for planners/tests."""
        return (
            [s.t for s in self.samples],
            [s.x for s in self.samples],
            [s.y for s in self.samples],
        )

    # --- (de)serialisation for state.json -----------------------------------
    def to_dict(self) -> dict:
        return {
            "width": self.width,
            "height": self.height,
            "duration": round(self.duration, 3),
            "skipped_frames": self.skipped_frames,
            "samples": [
                [round(s.t, 3), round(s.x, 4), round(s.y, 4), int(s.detected)] for s in self.samples
            ],
        }

    @classmethod
    def from_dict(cls, d: dict) -> CursorTrack:
        tr = cls(
            width=int(d.get("width", 0)),
            height=int(d.get("height", 0)),
            duration=float(d.get("duration", 0.0)),
            skipped_frames=int(d.get("skipped_frames", 0)),
        )
        for t, x, y, det in d.get("samples", []):
            tr.samples.append(CursorSample(t=float(t), x=float(x), y=float(y), detected=bool(det)))
        return tr


# --------------------------------------------------------------------------- tracker
class CursorTracker:
    """Frame-by-frame cursor tracker.  Feed BGR frames in order via :meth:`update`.

    Parameters are expressed at *source* resolution and rescaled internally.
    """

    def __init__(
        self,
        width: int,
        height: int,
        *,
        work_width: int = 960,
        min_size_px: int = 8,
        max_size_px: int = 64,
        diff_threshold: int = 28,
        max_candidates: int = 40,
        min_score: float = 0.35,
    ):
        self.width = max(1, width)
        self.height = max(1, height)
        self.scale = min(1.0, work_width / self.width)
        self.work_size = (
            max(1, int(round(self.width * self.scale))),
            max(1, int(round(self.height * self.scale))),
        )
        self.min_size = max(2.0, min_size_px * self.scale)
        self.max_size = max(self.min_size + 1, max_size_px * self.scale)
        self.min_area = max(3.0, self.min_size * self.min_size * 0.15)
        self.max_area = self.max_size * self.max_size
        self.diff_threshold = diff_threshold
        self.max_candidates = max_candidates
        self.min_score = min_score

        self._prev: np.ndarray | None = None
        self._last: tuple[float, float] | None = None  # work-scale pixel position
        self._template: np.ndarray | None = None
        self.track = CursorTrack(width=self.width, height=self.height)

    # -- public ---------------------------------------------------------------
    def update(self, frame_bgr: np.ndarray, t: float) -> CursorSample:
        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        if gray.shape[1] != self.work_size[0] or gray.shape[0] != self.work_size[1]:
            gray = cv2.resize(gray, self.work_size, interpolation=cv2.INTER_AREA)

        found: tuple[float, float, float] | None = None
        if self._prev is not None:
            found = self._detect(gray, self._prev)
        self._prev = gray

        if found is not None:
            px, py, score = found
            self._last = (px, py)
            sample = CursorSample(
                t=t,
                x=_clamp01(px / self.work_size[0]),
                y=_clamp01(py / self.work_size[1]),
                detected=True,
                score=score,
            )
        elif self._last is not None:
            px, py = self._last
            sample = CursorSample(
                t=t,
                x=_clamp01(px / self.work_size[0]),
                y=_clamp01(py / self.work_size[1]),
                detected=False,
            )
        else:
            sample = CursorSample(t=t, x=0.5, y=0.5, detected=False)
        self.track.samples.append(sample)
        self.track.duration = max(self.track.duration, t)
        return sample

    # -- internals ------------------------------------------------------------
    def _detect(self, gray: np.ndarray, prev: np.ndarray) -> tuple[float, float, float] | None:
        diff = cv2.absdiff(gray, prev)
        _, mask = cv2.threshold(diff, self.diff_threshold, 255, cv2.THRESH_BINARY)
        if not mask.any():
            return None
        # merge the cursor outline + body into one blob
        mask = cv2.dilate(mask, np.ones((3, 3), np.uint8), iterations=1)
        n, _labels, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
        if n <= 1:
            return None
        candidates: list[tuple[float, float, float, float]] = []  # (score, cx, cy, size)
        big = 0
        for i in range(1, n):
            x, y, w, h, area = stats[i]
            if w > self.max_size or h > self.max_size:
                big += 1
                continue
            if area < self.min_area or area > self.max_area:
                continue
            if max(w, h) < self.min_size * 0.6:
                continue
            aspect = min(w, h) / max(w, h)
            if aspect < 0.22:  # thin lines: text caret, underline, scrollbar edges
                continue
            score = self._score_patch(gray, x, y, w, h)
            if score <= 0:
                continue
            cx = x + w / 2.0
            cy = y + h / 2.0
            candidates.append((score, cx, cy, float(max(w, h))))
        if not candidates or len(candidates) > self.max_candidates or big > 6:
            # Full-screen change (scroll / page load / video) - cannot isolate a cursor.
            self.track.skipped_frames += (
                1 if (big > 6 or len(candidates) > self.max_candidates) else 0
            )
            return None

        # Continuity + left-behind rule
        ranked: list[tuple[float, float, float, int]] = []
        for idx, (score, cx, cy, _size) in enumerate(candidates):
            s = score
            if self._last is not None:
                d = float(np.hypot(cx - self._last[0], cy - self._last[1]))
                if d < 1.5 and len(candidates) > 1:
                    s *= 0.3  # the hole left behind by the cursor
                elif d < 1.5:
                    s *= 0.6  # something blinking in place (caret) - be sceptical
                # gentle preference for nearby candidates (cursor rarely teleports)
                s *= 1.0 / (1.0 + d / (self.work_size[0] * 0.5))
            ranked.append((s, cx, cy, idx))
        ranked.sort(reverse=True)
        best_s, cx, cy, idx = ranked[0]
        if best_s < self.min_score:
            return None
        self._learn_template(gray, cx, cy, candidates[idx][3])
        return cx, cy, best_s

    def _score_patch(self, gray: np.ndarray, x: int, y: int, w: int, h: int) -> float:
        pad = 2
        x0, y0 = max(0, x - pad), max(0, y - pad)
        x1, y1 = min(gray.shape[1], x + w + pad), min(gray.shape[0], y + h + pad)
        roi = gray[y0:y1, x0:x1]
        if roi.size == 0:
            return 0.0
        contrast = float(int(roi.max()) - int(roi.min())) / 255.0
        if contrast < 0.35:
            return 0.0
        bright = float((roi > 185).mean())
        dark = float((roi < 75).mean())
        two_tone = 1.0 if (bright > 0.04 and dark > 0.04) else 0.45
        score = contrast * two_tone
        if self._template is not None:
            score *= 0.6 + 0.8 * max(0.0, self._template_similarity(gray, x0, y0, x1, y1))
        return score

    def _template_similarity(self, gray: np.ndarray, x0: int, y0: int, x1: int, y1: int) -> float:
        tpl = self._template
        assert tpl is not None
        th, tw = tpl.shape
        # search a slightly larger neighbourhood so the bbox does not need to be exact
        sx0, sy0 = max(0, x0 - 3), max(0, y0 - 3)
        sx1, sy1 = min(gray.shape[1], x1 + 3), min(gray.shape[0], y1 + 3)
        search = gray[sy0:sy1, sx0:sx1]
        if search.shape[0] < th or search.shape[1] < tw:
            return 0.0
        res = cv2.matchTemplate(search, tpl, cv2.TM_CCOEFF_NORMED)
        return float(res.max()) if res.size else 0.0

    def _learn_template(self, gray: np.ndarray, cx: float, cy: float, size: float) -> None:
        half = int(max(3, min(self.max_size, size)) / 2) + 1
        x0, y0 = int(cx) - half, int(cy) - half
        x1, y1 = int(cx) + half + 1, int(cy) + half + 1
        if x0 < 0 or y0 < 0 or x1 > gray.shape[1] or y1 > gray.shape[0]:
            return
        patch = gray[y0:y1, x0:x1]
        if patch.size >= 25 and patch.std() > 20:
            self._template = patch.copy()


def detect_cursor_track(
    path: str | Path,
    *,
    sample_fps: float = 10.0,
    work_width: int = 960,
    min_size_px: int = 8,
    max_size_px: int = 64,
) -> CursorTrack:
    """Run :class:`CursorTracker` over a video file."""
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"OpenCV could not open {path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    step = max(1, int(round(fps / max(0.5, sample_fps))))
    tracker = CursorTracker(
        width, height, work_width=work_width, min_size_px=min_size_px, max_size_px=max_size_px
    )
    idx = 0
    while True:
        if not cap.grab():
            break
        if idx % step == 0:
            ok, frame = cap.retrieve()
            if not ok:
                break
            tracker.update(frame, idx / fps)
        idx += 1
    cap.release()
    track = tracker.track
    track.backfill()
    if total and fps:
        track.duration = max(track.duration, total / fps)
    log.info(
        "Cursor track: %d samples, %d detections (%.0f%% coverage), %d skipped frames",
        len(track.samples),
        track.detections,
        track.coverage * 100,
        track.skipped_frames,
    )
    return track


# --------------------------------------------------------------------------- planning
def follow_path(
    times: list[float],
    targets: list[float],
    *,
    window: float,
    deadzone: float = 0.3,
    smoothing: float = 0.8,
    max_speed: float = 1.5,
    start: float | None = None,
) -> list[float]:
    """Camera-follow: window *left edge* (fraction 0..1-window) that keeps the target visible.

    * ``deadzone`` - fraction of the window in which the target may move without panning.
    * ``smoothing`` - exponential smoothing factor per sample (0 = instant, 0.95 = very lazy).
    * ``max_speed`` - maximum pan speed in frame-widths per second.
    """
    if not times:
        return []
    window = min(1.0, max(0.01, window))
    lo, hi = 0.0, 1.0 - window
    margin = window * (1.0 - min(0.95, max(0.0, deadzone))) / 2.0
    left = _clamp(targets[0] - window / 2.0, lo, hi) if start is None else _clamp(start, lo, hi)
    out: list[float] = []
    prev_t = times[0]
    for t, x in zip(times, targets):
        target = left
        if x < left + margin:
            target = x - margin
        elif x > left + window - margin:
            target = x + margin - window
        target = _clamp(target, lo, hi)
        # Lazy camera inside the dead-zone band, but catch up quickly (half the
        # lag) when the cursor has actually left the visible window.
        alpha = smoothing if left <= x <= left + window else smoothing * smoothing
        desired = alpha * left + (1.0 - alpha) * target
        dt = max(0.0, t - prev_t)
        max_step = max_speed * dt  # first sample: dt == 0 -> the path starts exactly at ``start``
        step = max(-max_step, min(max_step, desired - left))
        left = _clamp(left + step, lo, hi)
        out.append(left)
        prev_t = t
    return out


def simplify_keyframes(points: list[Keyframe], tolerance: float) -> list[Keyframe]:
    """Ramer-Douglas-Peucker on (t, value) so long static stretches collapse to 2 points."""
    if len(points) <= 2:
        return list(points)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        ta, va = points[a]
        tb, vb = points[b]
        span = tb - ta
        worst, worst_i = -1.0, -1
        for i in range(a + 1, b):
            t, v = points[i]
            interp = va + (vb - va) * ((t - ta) / span if span > 0 else 0.0)
            err = abs(v - interp)
            if err > worst:
                worst, worst_i = err, i
        if worst > tolerance:
            keep[worst_i] = True
            stack.append((a, worst_i))
            stack.append((worst_i, b))
    return [p for p, k in zip(points, keep) if k]


def piecewise_linear_expression(
    segments: list[list[Keyframe]], var: str = "t", precision: int = 3
) -> str:
    """FFmpeg expression evaluating piecewise-linear keyframes; holds across segment gaps.

    ``segments`` is a list of keyframe lists in increasing time.  Inside a
    segment values are interpolated linearly, between segments (cuts) the last
    value is held, so a jump cut does not produce a fast pan.
    """
    flat = [kf for seg in segments for kf in seg]
    if not flat:
        return "0"
    if len(flat) == 1:
        return f"{flat[0][1]:.{precision}f}"
    terms: list[str] = []
    t0, v0 = flat[0]
    terms.append(f"lt({var},{t0:.{precision}f})*{v0:.{precision}f}")
    for si, seg in enumerate(segments):
        for i in range(len(seg)):
            ta, va = seg[i]
            if i + 1 < len(seg):
                tb, vb = seg[i + 1]
                if tb <= ta:
                    continue
                if abs(vb - va) < 10 ** (-precision):
                    terms.append(
                        f"gte({var},{ta:.{precision}f})*lt({var},{tb:.{precision}f})*{va:.{precision}f}"
                    )
                else:
                    slope = (vb - va) / (tb - ta)
                    terms.append(
                        f"gte({var},{ta:.{precision}f})*lt({var},{tb:.{precision}f})"
                        f"*({va:.{precision}f}+{slope:.{precision + 2}f}*({var}-{ta:.{precision}f}))"
                    )
            else:
                # hold the last value of this segment until the next segment starts (or forever)
                if si + 1 < len(segments) and segments[si + 1]:
                    tb = segments[si + 1][0][0]
                    if tb > ta:
                        terms.append(
                            f"gte({var},{ta:.{precision}f})*lt({var},{tb:.{precision}f})*{va:.{precision}f}"
                        )
                else:
                    terms.append(f"gte({var},{ta:.{precision}f})*{va:.{precision}f}")
    return "+".join(terms)


def evaluate_keyframes(segments: list[list[Keyframe]], t: float) -> float:
    """Python twin of :func:`piecewise_linear_expression` (used by tests and reporting)."""
    flat = [kf for seg in segments for kf in seg]
    if not flat:
        return 0.0
    if t < flat[0][0]:
        return flat[0][1]
    for si, seg in enumerate(segments):
        for i in range(len(seg)):
            ta, va = seg[i]
            if i + 1 < len(seg):
                tb, vb = seg[i + 1]
                if ta <= t < tb:
                    return va + (vb - va) * (t - ta) / (tb - ta)
            else:
                nxt = (
                    segments[si + 1][0][0] if si + 1 < len(segments) and segments[si + 1] else None
                )
                if t >= ta and (nxt is None or t < nxt):
                    return va
    return flat[-1][1]


# --------------------------------------------------------------------------- helpers
def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def _clamp01(v: float) -> float:
    return _clamp(v, 0.0, 1.0)


# --------------------------------------------------------------------------- clicks (v0.3)
def detect_click_events(
    understanding,
    dt: float = 0.25,
    *,
    min_area: float = 0.0025,
    max_area: float = 0.12,
    max_cursor_speed: float = 0.5,
    max_settle_speed: float = 0.15,
    min_width: float = 0.06,
    debounce: float = 0.6,
) -> list:
    """Infer mouse clicks from the visual signal alone.

    A click in a screen recording is a *small, local* change (button press
    state, focus ring, dropdown) that appears **where the pointer is resting**.
    Three filters keep this honest:

    * ``min_area`` / shape: the pointer's own motion trail is a *narrow, tall*
      sliver (the arrow swept along its path); UI feedback is a *wide* box
      (a button, a field, a menu), so a candidate must be wide or clearly
      wider than it is tall;
    * ``max_area`` rejects page loads, scrolls and navigations;
    * ``max_cursor_speed`` / ``max_settle_speed`` require the pointer to be
      settling on the target - people do not click mid-flight.

    Without a tracked cursor the function returns an empty list instead of
    guessing.  Typed loosely to avoid a circular import with
    :mod:`contentforge.processing.video_understanding`.
    """
    from contentforge.models.schemas import ActionEvent  # local import: avoids cycle

    frames = getattr(understanding, "frames", [])
    events: list[ActionEvent] = []
    if not frames:
        return events
    motions = [f.motion for f in frames if f.motion > 0]
    baseline = float(np.median(motions)) if motions else 0.0
    floor = max(0.05, baseline * 0.08)
    speeds = _smoothed_cursor_speeds(frames)
    last_click = -10.0
    for i, f in enumerate(frames):
        c = f.cursor
        change = f.change
        if c is None or change is None:
            continue
        if not (min_area <= change.area <= max_area):
            continue
        if change.w < min_width and change.w < 2.2 * change.h:
            continue  # narrow sliver -> the pointer's own trail, not a button
        if f.motion < floor:
            continue
        if speeds[i] > max_cursor_speed:
            continue
        settle = speeds[i + 1] if i + 1 < len(speeds) else 0.0
        if settle > max_settle_speed:
            continue
        if not change.expanded(0.04).contains_point(c.x, c.y):
            continue
        if f.t - last_click < debounce:
            continue
        last_click = f.t
        label = ""
        get_text = getattr(understanding, "text_at_region", None)
        if callable(get_text):
            label = get_text(change.expanded(0.02), f.t)
        events.append(
            ActionEvent(
                start=max(0.0, f.t - dt),
                end=f.t + dt,
                kind="click",
                region=change.expanded(0.02).clamped(),
                label=label,
                confidence=min(1.0, 0.5 + min(0.4, change.area * 8)),
            )
        )
    return events


def _smoothed_cursor_speeds(frames) -> list[float]:
    """Per-frame cursor speed (frame widths / s) from median-filtered positions.

    The blob tracker occasionally latches onto a UI change instead of the
    pointer; a 3-sample median filter removes those single-frame jumps so a
    genuine click is not mistaken for a fast pointer movement.
    """
    pts = [(f.t, f.cursor.x if f.cursor else None, f.cursor.y if f.cursor else None) for f in frames]
    xs = [p[1] for p in pts]
    ys = [p[2] for p in pts]

    def med(seq, i):
        window = [v for v in seq[max(0, i - 1) : i + 2] if v is not None]
        return float(np.median(window)) if window else None

    speeds: list[float] = []
    for i in range(len(pts)):
        x, y = med(xs, i), med(ys, i)
        if i == 0 or x is None or y is None:
            speeds.append(0.0)
            continue
        px, py = med(xs, i - 1), med(ys, i - 1)
        if px is None or py is None:
            speeds.append(0.0)
            continue
        dt_ = max(1e-3, pts[i][0] - pts[i - 1][0])
        speeds.append(((x - px) ** 2 + (y - py) ** 2) ** 0.5 / dt_)
    return speeds


def cursor_speed(track: CursorTrack) -> list[float]:
    """Per-sample cursor speed in frame widths / second (0 for the first sample)."""
    out = [0.0]
    for a, b in zip(track.samples, track.samples[1:]):
        dt = max(1e-3, b.t - a.t)
        out.append(((b.x - a.x) ** 2 + (b.y - a.y) ** 2) ** 0.5 / dt)
    return out

"""OpenCV-based frame analysis: motion detection and region-of-interest tracking.

Used for two things:

1. **Jump cuts** - find spans where the screen is static (nothing changes).
2. **Smart vertical crop** - find where the "action" is horizontally so the
   9:16 crop follows the cursor / active panel instead of always centring.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import cv2
import numpy as np

from contentforge.log import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from contentforge.processing.cursor import CursorTrack, CursorTracker

log = get_logger("analysis")


@dataclass
class FrameAnalysis:
    """Per-sample analysis results at ``sample_fps``."""

    times: list[float] = field(default_factory=list)
    motion: list[float] = field(default_factory=list)  # mean abs diff vs previous sample (0-255)
    centers_x: list[float] = field(default_factory=list)  # activity centroid as fraction 0..1
    width: int = 0
    height: int = 0
    duration: float = 0.0
    cursor: CursorTrack | None = None  # filled when a CursorTracker is supplied

    def low_motion_intervals(
        self, threshold: float, min_duration: float
    ) -> list[tuple[float, float]]:
        """Spans where motion stays below ``threshold`` for at least ``min_duration``."""
        out: list[tuple[float, float]] = []
        start: float | None = None
        for t, m in zip(self.times, self.motion):
            if m < threshold:
                if start is None:
                    start = t
            elif start is not None:
                if t - start >= min_duration:
                    out.append((start, t))
                start = None
        if start is not None and self.duration - start >= min_duration:
            out.append((start, self.duration))
        return out

    def smoothed_centers(self, alpha: float = 0.85) -> list[float]:
        """Exponentially smoothed horizontal centre so the crop does not jitter."""
        if not self.centers_x:
            return []
        out = [self.centers_x[0]]
        for c in self.centers_x[1:]:
            out.append(alpha * out[-1] + (1 - alpha) * c)
        return out

    def dominant_center(self, alpha: float = 0.85) -> float:
        """A single representative horizontal centre (median of smoothed track)."""
        sm = self.smoothed_centers(alpha)
        return float(np.median(sm)) if sm else 0.5


def analyse_video(
    path: str | Path,
    sample_fps: float = 2.0,
    downscale_width: int = 320,
    *,
    cursor_tracker: CursorTracker | None = None,
    cursor_sample_fps: float = 10.0,
) -> FrameAnalysis:
    """Sample frames and compute motion + activity centroid.

    Frames are downscaled for speed; a 60 s clip at 2 fps takes well under a
    second on a laptop CPU.  When ``cursor_tracker`` is given the *same* decode
    pass also feeds frames to the tracker at ``cursor_sample_fps`` (one pass
    over the file instead of two - matters on slow laptops).
    """
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"OpenCV could not open {path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    duration = total / fps if fps else 0.0
    step = max(1, int(round(fps / max(0.1, sample_fps))))
    cstep = max(1, int(round(fps / max(0.5, cursor_sample_fps))))
    scale = downscale_width / max(1, width)
    small_size = (downscale_width, max(1, int(height * scale)))

    result = FrameAnalysis(width=width, height=height, duration=duration)
    prev: np.ndarray | None = None
    idx = 0
    last_center = 0.5
    while True:
        ok = cap.grab()
        if not ok:
            break
        want_motion = idx % step == 0
        want_cursor = cursor_tracker is not None and idx % cstep == 0
        if want_motion or want_cursor:
            ok, frame = cap.retrieve()
            if not ok:
                break
            if want_cursor and cursor_tracker is not None:
                cursor_tracker.update(frame, idx / fps)
        if want_motion:
            gray = cv2.cvtColor(
                cv2.resize(frame, small_size, interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY
            )
            gray = cv2.GaussianBlur(gray, (5, 5), 0)
            t = idx / fps
            if prev is None:
                motion = 0.0
                center = _content_center(gray)
            else:
                diff = cv2.absdiff(gray, prev)
                motion = float(diff.mean())
                center = _activity_center(diff, fallback=last_center)
            last_center = center
            result.times.append(t)
            result.motion.append(motion)
            result.centers_x.append(center)
            prev = gray
        idx += 1
    cap.release()
    if not result.duration and result.times:
        result.duration = result.times[-1]
    if cursor_tracker is not None:
        cursor_tracker.track.backfill()
        cursor_tracker.track.duration = max(cursor_tracker.track.duration, result.duration)
        result.cursor = cursor_tracker.track
    log.debug(
        "Analysed %d samples: mean motion %.2f, centre %.2f",
        len(result.times),
        float(np.mean(result.motion)) if result.motion else 0,
        result.dominant_center(),
    )
    return result


def _activity_center(diff: np.ndarray, fallback: float, min_mass: float = 50.0) -> float:
    """Horizontal centroid of changed pixels (fraction of width)."""
    _, mask = cv2.threshold(diff, 18, 255, cv2.THRESH_BINARY)
    m = cv2.moments(mask, binaryImage=True)
    if m["m00"] < min_mass * 255:
        return fallback
    return float(m["m10"] / m["m00"]) / diff.shape[1]


def _content_center(gray: np.ndarray) -> float:
    """Centroid of high-contrast content (edges) - decent guess for the first frame."""
    edges = cv2.Canny(gray, 60, 160)
    m = cv2.moments(edges, binaryImage=True)
    if m["m00"] == 0:
        return 0.5
    return float(m["m10"] / m["m00"]) / gray.shape[1]

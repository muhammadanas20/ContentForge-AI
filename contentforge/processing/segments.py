"""Pure-python edit-decision logic: silence removal, jump cuts and timeline remapping.

Everything here is deterministic and free of I/O so it is easy to unit test.
An *edit list* is a list of ``(start, end)`` keep-ranges in source time,
sorted and non-overlapping. :class:`Timeline` maps source timestamps to the
output timeline after cuts - needed to keep subtitles aligned with the video.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass

Interval = tuple[float, float]


def invert_intervals(intervals: list[Interval], duration: float) -> list[Interval]:
    """Given removed ranges, return the kept ranges within ``[0, duration]``."""
    kept: list[Interval] = []
    cursor = 0.0
    for s, e in sorted(intervals):
        s, e = max(0.0, s), min(duration, e)
        if s > cursor:
            kept.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < duration:
        kept.append((cursor, duration))
    return kept


def merge_intervals(intervals: list[Interval], gap: float = 0.0) -> list[Interval]:
    """Merge overlapping / near (within ``gap``) intervals."""
    out: list[Interval] = []
    for s, e in sorted(intervals):
        if out and s - out[-1][1] <= gap:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def shrink_silences(
    silences: list[Interval], *, padding: float, min_duration: float, duration: float
) -> list[Interval]:
    """Convert detected silences into *removal* ranges.

    Each silence keeps ``padding`` seconds on either side so speech never
    starts abruptly. Silences shorter than ``min_duration`` after padding are
    left alone.
    """
    removals: list[Interval] = []
    for s, e in silences:
        rs, re_ = s + padding, e - padding
        # Leading/trailing silences can be trimmed harder (no speech to protect on one side)
        is_edge = False
        if s <= 0.01:
            rs, is_edge = 0.0, True
        if e >= duration - 0.01:
            re_, is_edge = duration, True
        threshold = min(min_duration, 0.2) if is_edge else min_duration
        if re_ - rs >= threshold:
            removals.append((max(0.0, rs), min(duration, re_)))
    return merge_intervals(removals)


def build_keep_ranges(
    duration: float,
    silences: list[Interval],
    *,
    padding: float,
    min_silence: float,
    low_motion: list[Interval] | None = None,
    min_gap: float = 1.2,
    min_segment: float = 0.25,
) -> list[Interval]:
    """Compute the final keep-list from silence and (optional) low-motion analysis.

    * Silence-only regions are removed (with padding).
    * *Jump cuts*: regions that are silent **and** static for at least ``min_gap``
      are removed more aggressively (no padding) because nothing happens there.
    * Segments shorter than ``min_segment`` are dropped to avoid flicker.
    """
    removals = shrink_silences(
        silences, padding=padding, min_duration=min_silence, duration=duration
    )
    if low_motion:
        dead = []
        for ss, se in silences:
            for ms, me in low_motion:
                s, e = max(ss, ms), min(se, me)
                if e - s >= min_gap:
                    dead.append((s, e))
        removals = merge_intervals(removals + dead)
    keep = [(s, e) for s, e in invert_intervals(removals, duration) if e - s >= min_segment]
    return keep or [(0.0, duration)]


@dataclass
class Timeline:
    """Maps source time -> output time for a keep-list."""

    keep: list[Interval]

    def __post_init__(self) -> None:
        self._src_starts = [s for s, _ in self.keep]
        self._out_starts: list[float] = []
        t = 0.0
        for s, e in self.keep:
            self._out_starts.append(t)
            t += e - s
        self.output_duration = t

    def to_output(self, t: float) -> float | None:
        """Source time -> output time. ``None`` if ``t`` falls in a removed region."""
        i = bisect_right(self._src_starts, t) - 1
        if i < 0:
            return None
        s, e = self.keep[i]
        if t > e + 1e-6:
            return None
        return self._out_starts[i] + (t - s)

    def clamp_to_output(self, t: float) -> float:
        """Like :meth:`to_output` but snaps removed times to the nearest kept edge."""
        mapped = self.to_output(t)
        if mapped is not None:
            return mapped
        i = bisect_right(self._src_starts, t) - 1
        if i < 0:
            return 0.0
        # t is after keep[i].end and before keep[i+1].start -> snap to end of keep[i]
        return self._out_starts[i] + (self.keep[i][1] - self.keep[i][0])

    def remap_range(self, start: float, end: float) -> Interval | None:
        """Map a (start, end) source range; returns ``None`` if fully removed."""
        s = self.clamp_to_output(start)
        e = self.clamp_to_output(end)
        if e - s < 0.05:
            return None
        return (s, e)

    @property
    def removed_seconds(self) -> float:
        src_total = self.keep[-1][1] if self.keep else 0.0
        return max(0.0, src_total - self.output_duration)

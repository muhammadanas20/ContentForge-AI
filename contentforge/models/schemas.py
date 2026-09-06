"""Data structures shared by the v0.3 *video understanding* pipeline.

Everything here is plain ``dataclass`` (no pydantic): these objects are created
thousands of times per job (one per sampled frame), must be cheap on an 8 GB
laptop and are round-tripped through ``state.json`` between resumable steps.

Coordinate convention
---------------------
**All rectangles and points are normalised fractions of the source frame**
(``0.0..1.0``), never pixels.  That keeps the analysis resolution-independent:
frames are sampled at a reduced working width, but the framing planner, the
renderer and the quality gates all reason in the same space and only convert to
pixels at the very last moment (:meth:`Region.to_px`).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# --------------------------------------------------------------------------- geometry


@dataclass
class Region:
    """An axis-aligned rectangle in normalised frame coordinates."""

    x: float = 0.0
    y: float = 0.0
    w: float = 1.0
    h: float = 1.0
    kind: str = ""  # text | ui | action | content | cursor | result ...
    score: float = 1.0

    # ---- derived -----------------------------------------------------------
    @property
    def x2(self) -> float:
        return self.x + self.w

    @property
    def y2(self) -> float:
        return self.y + self.h

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    @property
    def area(self) -> float:
        return max(0.0, self.w) * max(0.0, self.h)

    # ---- ops ---------------------------------------------------------------
    def intersection(self, other: Region) -> Region:
        x = max(self.x, other.x)
        y = max(self.y, other.y)
        x2 = min(self.x2, other.x2)
        y2 = min(self.y2, other.y2)
        if x2 <= x or y2 <= y:
            return Region(x, y, 0.0, 0.0, kind="empty", score=0.0)
        return Region(x, y, x2 - x, y2 - y, kind=self.kind, score=self.score)

    def intersection_area(self, other: Region) -> float:
        return self.intersection(other).area

    def contains_point(self, x: float, y: float, margin: float = 0.0) -> bool:
        return (
            self.x - margin <= x <= self.x2 + margin and self.y - margin <= y <= self.y2 + margin
        )

    def contains(self, other: Region, tol: float = 1e-6) -> bool:
        return (
            other.x >= self.x - tol
            and other.y >= self.y - tol
            and other.x2 <= self.x2 + tol
            and other.y2 <= self.y2 + tol
        )

    def coverage_of(self, other: Region) -> float:
        """Fraction of ``other`` that lies inside ``self`` (0..1)."""
        if other.area <= 0:
            return 0.0
        return self.intersection_area(other) / other.area

    def is_clipped_by(self, view: Region, tol: float = 0.02) -> bool:
        """True when ``view`` shows *part* of this region and cuts the rest off.

        A region fully outside the view is not "clipped" - the viewer simply
        never sees it, which is a framing choice.  A region that is half visible
        is the ugly case (a headline sliced down the middle) and is what the
        framing planner must avoid.
        """
        cov = view.coverage_of(self)
        return tol < cov < 1.0 - tol

    def union(self, other: Region) -> Region:
        x = min(self.x, other.x)
        y = min(self.y, other.y)
        return Region(
            x,
            y,
            max(self.x2, other.x2) - x,
            max(self.y2, other.y2) - y,
            kind=self.kind or other.kind,
            score=max(self.score, other.score),
        )

    def expanded(self, margin: float) -> Region:
        return Region(
            self.x - margin,
            self.y - margin,
            self.w + 2 * margin,
            self.h + 2 * margin,
            self.kind,
            self.score,
        )

    def clamped(self) -> Region:
        x = min(max(0.0, self.x), 1.0)
        y = min(max(0.0, self.y), 1.0)
        return Region(x, y, min(self.w, 1.0 - x), min(self.h, 1.0 - y), self.kind, self.score)

    def to_px(self, width: int, height: int) -> tuple[int, int, int, int]:
        return (
            int(round(self.x * width)),
            int(round(self.y * height)),
            int(round(self.w * width)),
            int(round(self.h * height)),
        )

    @classmethod
    def from_px(
        cls,
        x: float,
        y: float,
        w: float,
        h: float,
        width: int,
        height: int,
        kind: str = "",
        score: float = 1.0,
    ) -> Region:
        width = max(1, width)
        height = max(1, height)
        return cls(x / width, y / height, w / width, h / height, kind, score)

    def to_dict(self) -> dict[str, Any]:
        return {
            "x": round(self.x, 5),
            "y": round(self.y, 5),
            "w": round(self.w, 5),
            "h": round(self.h, 5),
            "kind": self.kind,
            "score": round(self.score, 4),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> Region | None:
        if not d:
            return None
        return cls(
            float(d.get("x", 0.0)),
            float(d.get("y", 0.0)),
            float(d.get("w", 1.0)),
            float(d.get("h", 1.0)),
            str(d.get("kind", "")),
            float(d.get("score", 1.0)),
        )


FULL_FRAME = Region(0.0, 0.0, 1.0, 1.0, kind="frame")


@dataclass
class TextBox:
    """One OCR (or text-region) detection on a sampled frame."""

    region: Region
    text: str = ""
    confidence: float = 0.0
    source: str = "heuristic"  # tesseract | heuristic

    @property
    def has_text(self) -> bool:
        return bool(self.text.strip())

    def to_dict(self) -> dict[str, Any]:
        return {
            "region": self.region.to_dict(),
            "text": self.text,
            "confidence": round(self.confidence, 3),
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TextBox:
        return cls(
            region=Region.from_dict(d.get("region")) or Region(),
            text=str(d.get("text", "")),
            confidence=float(d.get("confidence", 0.0)),
            source=str(d.get("source", "heuristic")),
        )


@dataclass
class CursorPoint:
    t: float
    x: float
    y: float
    detected: bool = False
    moving: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "t": round(self.t, 3),
            "x": round(self.x, 4),
            "y": round(self.y, 4),
            "detected": bool(self.detected),
            "moving": bool(self.moving),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CursorPoint:
        return cls(
            float(d["t"]),
            float(d["x"]),
            float(d["y"]),
            bool(d.get("detected", False)),
            bool(d.get("moving", False)),
        )


# --------------------------------------------------------------------------- understanding


@dataclass
class FrameObservation:
    """What the analyser saw at one sampled timestamp."""

    t: float
    motion: float = 0.0  # mean abs diff vs previous sample (0..255)
    change: Region | None = None  # bounding box of what changed
    scroll_dy: float = 0.0  # vertical scroll estimate, fraction of height / sample
    scene_change: bool = False
    cursor: CursorPoint | None = None
    text_boxes: list[TextBox] = field(default_factory=list)
    text_mass: float = 0.0  # fraction of the frame covered by text-like content
    sharpness: float = 0.0
    semantic_description: str = ""

    @property
    def text(self) -> str:
        return " ".join(b.text for b in self.text_boxes if b.has_text).strip()

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "t": round(self.t, 3),
            "motion": round(self.motion, 3),
            "scroll_dy": round(self.scroll_dy, 4),
            "scene_change": self.scene_change,
            "text_mass": round(self.text_mass, 4),
            "sharpness": round(self.sharpness, 2),
        }
        if self.change is not None:
            d["change"] = self.change.to_dict()
        if self.cursor is not None:
            d["cursor"] = self.cursor.to_dict()
        if self.text_boxes:
            d["text_boxes"] = [b.to_dict() for b in self.text_boxes]
        if self.semantic_description:
            d["semantic_description"] = self.semantic_description
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FrameObservation:
        return cls(
            t=float(d["t"]),
            motion=float(d.get("motion", 0.0)),
            change=Region.from_dict(d.get("change")),
            scroll_dy=float(d.get("scroll_dy", 0.0)),
            scene_change=bool(d.get("scene_change", False)),
            cursor=CursorPoint.from_dict(d["cursor"]) if d.get("cursor") else None,
            text_boxes=[TextBox.from_dict(b) for b in d.get("text_boxes", [])],
            text_mass=float(d.get("text_mass", 0.0)),
            sharpness=float(d.get("sharpness", 0.0)),
            semantic_description=str(d.get("semantic_description", "")),
        )


ACTION_KINDS = ("click", "scroll", "type", "navigate", "reveal", "idle", "move")


@dataclass
class ActionEvent:
    """Something the presenter did, derived from the visual signal only."""

    start: float
    end: float
    kind: str  # one of ACTION_KINDS
    region: Region | None = None
    label: str = ""  # OCR text near the action, when known
    confidence: float = 0.5

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def mid(self) -> float:
        return (self.start + self.end) / 2

    def to_dict(self) -> dict[str, Any]:
        d = {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "kind": self.kind,
            "label": self.label,
            "confidence": round(self.confidence, 3),
        }
        if self.region is not None:
            d["region"] = self.region.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ActionEvent:
        return cls(
            start=float(d["start"]),
            end=float(d["end"]),
            kind=str(d["kind"]),
            region=Region.from_dict(d.get("region")),
            label=str(d.get("label", "")),
            confidence=float(d.get("confidence", 0.5)),
        )


@dataclass
class VideoUnderstanding:
    """Everything the pipeline knows about the recording *before* editing."""

    width: int = 0
    height: int = 0
    duration: float = 0.0
    fps: float = 0.0
    sample_fps: float = 0.0
    ocr_backend: str = "none"
    frames: list[FrameObservation] = field(default_factory=list)
    actions: list[ActionEvent] = field(default_factory=list)
    content_region: Region = field(default_factory=lambda: Region(0, 0, 1, 1, kind="content"))
    website: str = ""
    website_context: str = ""
    notes: list[str] = field(default_factory=list)
    ai_insights: list[str] = field(default_factory=list)

    # ---- convenience -------------------------------------------------------
    @property
    def available(self) -> bool:
        """True when there is enough signal to plan a grounded edit."""
        return bool(self.frames) and self.duration > 0

    @property
    def has_ocr_text(self) -> bool:
        return any(b.has_text for f in self.frames for b in f.text_boxes)

    def frame_at(self, t: float) -> FrameObservation | None:
        if not self.frames:
            return None
        best = min(self.frames, key=lambda f: abs(f.t - t))
        return best

    def frames_between(self, start: float, end: float) -> list[FrameObservation]:
        return [f for f in self.frames if start <= f.t < end]

    def cursor_at(self, t: float) -> CursorPoint | None:
        pts = [f.cursor for f in self.frames if f.cursor is not None]
        if not pts:
            return None
        return min(pts, key=lambda c: abs(c.t - t))

    def text_boxes_at(self, t: float, window: float = 1.0) -> list[TextBox]:
        """Text boxes seen around ``t`` (nearest frame that actually has OCR)."""
        candidates = [f for f in self.frames if abs(f.t - t) <= window and f.text_boxes]
        if not candidates:
            with_text = [f for f in self.frames if f.text_boxes]
            if not with_text:
                return []
            candidates = [min(with_text, key=lambda f: abs(f.t - t))]
        best = min(candidates, key=lambda f: abs(f.t - t))
        return list(best.text_boxes)

    def actions_between(self, start: float, end: float) -> list[ActionEvent]:
        return [a for a in self.actions if a.start < end and a.end > start]

    def important_regions(self, start: float, end: float) -> list[Region]:
        """Regions worth keeping in frame between ``start`` and ``end``."""
        out: list[Region] = []
        for a in self.actions_between(start, end):
            if a.region is not None and a.kind in ("click", "type", "reveal", "navigate"):
                r = Region(a.region.x, a.region.y, a.region.w, a.region.h, "action", 1.0)
                out.append(r)
        mid = (start + end) / 2
        for b in self.text_boxes_at(mid, window=max(1.0, (end - start) / 2)):
            out.append(b.region)
        return out

    def motion_at(self, t: float) -> float:
        f = self.frame_at(t)
        return f.motion if f else 0.0

    def dominant_texts(self, limit: int = 30, min_len: int = 3) -> list[str]:
        """Most frequently observed OCR strings (deduplicated, ordered by weight)."""
        counts: dict[str, float] = {}
        first_seen: dict[str, float] = {}
        for f in self.frames:
            for b in f.text_boxes:
                s = " ".join(b.text.split())
                if len(s) < min_len:
                    continue
                key = s.lower()
                counts[key] = counts.get(key, 0.0) + max(0.2, b.confidence)
                first_seen.setdefault(key, f.t)
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], first_seen.get(kv[0], 0.0)))
        return [k for k, _ in ranked[:limit]]

    def text_at_region(self, region: Region, t: float, window: float = 1.5) -> str:
        """Best OCR string overlapping ``region`` near ``t`` (for action labels)."""
        best_text, best_overlap = "", 0.0
        for b in self.text_boxes_at(t, window):
            if not b.has_text:
                continue
            ov = region.coverage_of(b.region)
            if ov > best_overlap:
                best_overlap, best_text = ov, b.text
        return best_text.strip()

    def summary(self) -> dict[str, Any]:
        """Compact, JSON-friendly description (goes into the job record)."""
        counts: dict[str, int] = {}
        for a in self.actions:
            counts[a.kind] = counts.get(a.kind, 0) + 1
        tracked = sum(1 for f in self.frames if f.cursor.detected)
        out = {
            "frames": len(self.frames),
            "duration": round(self.duration, 2),
            "sample_fps": round(self.sample_fps, 2),
            "ocr_backend": self.ocr_backend,
            "ocr_text": self.has_ocr_text,
            "actions": len(self.actions),
            "action_counts": counts,
            "cursor_frames": tracked,
            "content_region": self.content_region.to_dict(),
            "website": self.website,
        }
        if self.ai_insights:
            out["ai_insights"] = list(self.ai_insights)
        return out

    def to_dict(self, *, include_frames: bool = True) -> dict[str, Any]:
        d: dict[str, Any] = {
            "width": self.width,
            "height": self.height,
            "duration": round(self.duration, 3),
            "fps": round(self.fps, 3),
            "sample_fps": round(self.sample_fps, 3),
            "ocr_backend": self.ocr_backend,
            "content_region": self.content_region.to_dict(),
            "actions": [a.to_dict() for a in self.actions],
            "website": self.website,
            "website_context": self.website_context,
            "notes": self.notes,
        }
        if self.ai_insights:
            d["ai_insights"] = list(self.ai_insights)
        if include_frames:
            d["frames"] = [f.to_dict() for f in self.frames]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> VideoUnderstanding:
        u = cls(
            width=int(d.get("width", 0)),
            height=int(d.get("height", 0)),
            duration=float(d.get("duration", 0.0)),
            fps=float(d.get("fps", 0.0)),
            sample_fps=float(d.get("sample_fps", 0.0)),
            ocr_backend=str(d.get("ocr_backend", "none")),
            website=str(d.get("website", "")),
            website_context=str(d.get("website_context", "")),
            notes=list(d.get("notes", [])),
            ai_insights=list(d.get("ai_insights", [])),
        )
        u.content_region = Region.from_dict(d.get("content_region")) or Region(
            0, 0, 1, 1, kind="content"
        )
        u.frames = [FrameObservation.from_dict(f) for f in d.get("frames", [])]
        u.actions = [ActionEvent.from_dict(a) for a in d.get("actions", [])]
        return u


# --------------------------------------------------------------------------- script


SEGMENT_ROLES = ("hook", "setup", "demo", "payoff", "cta")


@dataclass
class ScriptSegment:
    """One narration beat, bound to a real visual moment of the edit."""

    start: float  # output-timeline seconds
    end: float
    text: str
    role: str = "demo"
    visual_action: str = ""
    focus_region: Region | None = None
    evidence: list[str] = field(default_factory=list)  # OCR strings / action labels used
    source_start: float = 0.0  # matching moment in the ORIGINAL recording
    source_end: float = 0.0

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def word_count(self) -> int:
        return len(self.text.split())

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "text": self.text,
            "role": self.role,
            "visual_action": self.visual_action,
            "focus_region": self.focus_region.to_dict() if self.focus_region else None,
            "evidence": self.evidence,
            "source_start": round(self.source_start, 3),
            "source_end": round(self.source_end, 3),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ScriptSegment:
        return cls(
            start=float(d["start"]),
            end=float(d["end"]),
            text=str(d.get("text", "")),
            role=str(d.get("role", "demo")),
            visual_action=str(d.get("visual_action", "")),
            focus_region=Region.from_dict(d.get("focus_region")),
            evidence=list(d.get("evidence", [])),
            source_start=float(d.get("source_start", 0.0)),
            source_end=float(d.get("source_end", 0.0)),
        )


@dataclass
class GroundedScript:
    """A script whose every beat maps to a visual segment of the edit."""

    title: str = ""
    segments: list[ScriptSegment] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    website: str = ""
    source: str = "grounded-rule-based"
    grounded: bool = True
    degraded: bool = False  # True when visual understanding was unavailable
    notes: list[str] = field(default_factory=list)

    # ---- text views --------------------------------------------------------
    @property
    def narration(self) -> str:
        return " ".join(s.text.strip() for s in self.segments if s.text.strip())

    @property
    def word_count(self) -> int:
        return len(self.narration.split())

    @property
    def duration(self) -> float:
        return max((s.end for s in self.segments), default=0.0)

    def by_role(self, role: str) -> list[ScriptSegment]:
        return [s for s in self.segments if s.role == role]

    @property
    def hook(self) -> str:
        segs = self.by_role("hook")
        return segs[0].text if segs else (self.segments[0].text if self.segments else "")

    @property
    def cta(self) -> str:
        segs = self.by_role("cta")
        return segs[-1].text if segs else ""

    @property
    def body(self) -> list[str]:
        return [s.text for s in self.segments if s.role in ("setup", "demo", "payoff")]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["segments"] = [s.to_dict() for s in self.segments]
        d["narration"] = self.narration
        d["word_count"] = self.word_count
        d["hook"] = self.hook
        d["cta"] = self.cta
        d["body"] = self.body
        return d

    def cta_text(self) -> str:
        for seg in self.segments:
            if seg.role == "cta":
                return seg.text
        return ""

    def to_legacy_dict(self) -> dict[str, Any]:
        """v0.2 ``Script``-shaped dict so social/packaging keep working."""
        hook = next((s.text for s in self.segments if s.role == "hook"), "")
        body = [s.text for s in self.segments if s.role in ("setup", "demo", "payoff")]
        return {
            "hook": hook,
            "body": body,
            "cta": self.cta_text(),
            "title": self.title,
            "keywords": self.keywords,
            "source": self.source,
            "hook_style": "grounded",
            "website": self.website,
            "estimated_seconds": round(self.duration, 2),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> GroundedScript:
        return cls(
            title=str(d.get("title", "")),
            segments=[ScriptSegment.from_dict(s) for s in d.get("segments", [])],
            keywords=list(d.get("keywords", [])),
            website=str(d.get("website", "")),
            source=str(d.get("source", "grounded-rule-based")),
            grounded=bool(d.get("grounded", True)),
            degraded=bool(d.get("degraded", False)),
            notes=list(d.get("notes", [])),
        )

    def to_markdown(self) -> str:
        lines = [f"# {self.title}", ""]
        if self.website:
            lines += [f"Website: **{self.website}**", ""]
        lines += ["| # | time | role | narration | visual |", "|---|---|---|---|---|"]
        for i, s in enumerate(self.segments, 1):
            lines.append(
                f"| {i} | {s.start:.1f}-{s.end:.1f}s | {s.role} | {s.text} | {s.visual_action} |"
            )
        lines += [
            "",
            f"_{self.word_count} words, {self.duration:.1f}s, source: {self.source}"
            + (", degraded (no visual understanding)" if self.degraded else "")
            + "_",
        ]
        if self.notes:
            lines += ["", "Grounding notes:"] + [f"- {n}" for n in self.notes]
        return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- edit plan


@dataclass
class ViewRect:
    """The part of the source frame shown by a shot (normalised).

    ``x_keyframes``/``y_keyframes`` hold ``(output_time, value)`` pairs relative
    to the *shot* start, letting the camera drift smoothly inside a shot.
    """

    x: float
    y: float
    w: float
    h: float
    x_keyframes: list[tuple[float, float]] = field(default_factory=list)
    y_keyframes: list[tuple[float, float]] = field(default_factory=list)

    @property
    def region(self) -> Region:
        return Region(self.x, self.y, self.w, self.h, kind="view")

    @property
    def zoom(self) -> float:
        """How much the source is magnified horizontally (1.0 = full width)."""
        return 1.0 / max(1e-6, self.w)

    @property
    def moves(self) -> bool:
        return len(self.x_keyframes) > 1 or len(self.y_keyframes) > 1

    def at(self, t: float) -> Region:
        x = _interp(self.x_keyframes, t, self.x)
        y = _interp(self.y_keyframes, t, self.y)
        return Region(x, y, self.w, self.h, kind="view")

    def to_dict(self) -> dict[str, Any]:
        return {
            "x": round(self.x, 5),
            "y": round(self.y, 5),
            "w": round(self.w, 5),
            "h": round(self.h, 5),
            "x_keyframes": [[round(t, 3), round(v, 5)] for t, v in self.x_keyframes],
            "y_keyframes": [[round(t, 3), round(v, 5)] for t, v in self.y_keyframes],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ViewRect:
        return cls(
            float(d["x"]),
            float(d["y"]),
            float(d["w"]),
            float(d["h"]),
            [(float(t), float(v)) for t, v in d.get("x_keyframes", [])],
            [(float(t), float(v)) for t, v in d.get("y_keyframes", [])],
        )


def _interp(keyframes: list[tuple[float, float]], t: float, default: float) -> float:
    if not keyframes:
        return default
    if t <= keyframes[0][0]:
        return keyframes[0][1]
    for (t0, v0), (t1, v1) in zip(keyframes, keyframes[1:]):
        if t0 <= t <= t1:
            if t1 <= t0:
                return v1
            u = (t - t0) / (t1 - t0)
            return v0 + (v1 - v0) * u
    return keyframes[-1][1]


@dataclass
class Shot:
    """One rendered piece of the Reel, in both source and output time."""

    index: int
    src_start: float
    src_end: float
    out_start: float
    view: ViewRect
    layout: str = "fill"  # fill = 9:16 crop, canvas = scaled inside a designed canvas
    zoom_from: float = 1.0
    zoom_to: float = 1.0
    role: str = "demo"  # hook | setup | demo | payoff | cta
    action_kind: str = ""
    label: str = ""
    speed: float = 1.0  # >1 = played faster than real time

    @property
    def src_duration(self) -> float:
        return max(0.0, self.src_end - self.src_start)

    @property
    def out_duration(self) -> float:
        return self.src_duration / max(0.1, self.speed)

    @property
    def out_end(self) -> float:
        return self.out_start + self.out_duration

    @property
    def moves(self) -> bool:
        return self.view.moves or abs(self.zoom_to - self.zoom_from) > 1e-3

    def view_at_output(self, t: float) -> Region:
        """View rect at absolute output time ``t``."""
        return self.view.at(max(0.0, t - self.out_start))

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "src_start": round(self.src_start, 3),
            "src_end": round(self.src_end, 3),
            "out_start": round(self.out_start, 3),
            "out_duration": round(self.out_duration, 3),
            "view": self.view.to_dict(),
            "layout": self.layout,
            "zoom_from": round(self.zoom_from, 4),
            "zoom_to": round(self.zoom_to, 4),
            "role": self.role,
            "action_kind": self.action_kind,
            "label": self.label,
            "speed": round(self.speed, 4),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Shot:
        return cls(
            index=int(d["index"]),
            src_start=float(d["src_start"]),
            src_end=float(d["src_end"]),
            out_start=float(d["out_start"]),
            view=ViewRect.from_dict(d["view"]),
            layout=str(d.get("layout", "fill")),
            zoom_from=float(d.get("zoom_from", 1.0)),
            zoom_to=float(d.get("zoom_to", 1.0)),
            role=str(d.get("role", "demo")),
            action_kind=str(d.get("action_kind", "")),
            label=str(d.get("label", "")),
            speed=float(d.get("speed", 1.0)),
        )


@dataclass
class EditPlan:
    """The full editing decision list produced by the smart editor."""

    shots: list[Shot] = field(default_factory=list)
    source_duration: float = 0.0
    removed_seconds: float = 0.0
    structure: dict[str, tuple[float, float]] = field(default_factory=dict)
    emphasis: list[ActionEvent] = field(default_factory=list)  # clicks etc. in OUTPUT time
    notes: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return max((s.out_end for s in self.shots), default=0.0)

    def shot_at(self, t: float) -> Shot | None:
        for s in self.shots:
            if s.out_start <= t < s.out_end:
                return s
        return self.shots[-1] if self.shots else None

    def view_at(self, t: float) -> Region:
        s = self.shot_at(t)
        return s.view_at_output(t) if s else FULL_FRAME

    def source_time(self, t: float) -> float:
        """Map an output time back to the original recording."""
        s = self.shot_at(t)
        if s is None:
            return t
        return s.src_start + (t - s.out_start) * s.speed

    def role_at(self, t: float) -> str:
        s = self.shot_at(t)
        return s.role if s else "demo"

    @property
    def speed(self) -> float:
        """Average playback speed of the edit (1.0 = real time)."""
        if not self.shots:
            return 1.0
        total_out = sum(s.out_duration for s in self.shots)
        total_src = sum(s.src_duration for s in self.shots)
        return (total_src / total_out) if total_out > 0 else 1.0

    def summary(self) -> dict[str, Any]:
        roles: dict[str, int] = {}
        layouts: dict[str, int] = {}
        for s in self.shots:
            roles[s.role] = roles.get(s.role, 0) + 1
            layouts[s.layout] = layouts.get(s.layout, 0) + 1
        zooms = [1.0 / max(1e-6, s.view.w) for s in self.shots]
        return {
            "shots": len(self.shots),
            "duration": round(self.duration, 2),
            "source_duration": round(self.source_duration, 2),
            "speed": round(self.speed, 3),
            "roles": roles,
            "layouts": layouts,
            "max_zoom": round(max(zooms), 2) if zooms else 1.0,
            "structure": {k: [round(a, 2), round(b, 2)] for k, (a, b) in self.structure.items()},
            "emphasis": len(self.emphasis),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "shots": [s.to_dict() for s in self.shots],
            "source_duration": round(self.source_duration, 3),
            "output_duration": round(self.duration, 3),
            "removed_seconds": round(self.removed_seconds, 3),
            "structure": {k: [round(a, 3), round(b, 3)] for k, (a, b) in self.structure.items()},
            "emphasis": [a.to_dict() for a in self.emphasis],
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> EditPlan:
        return cls(
            shots=[Shot.from_dict(s) for s in d.get("shots", [])],
            source_duration=float(d.get("source_duration", 0.0)),
            removed_seconds=float(d.get("removed_seconds", 0.0)),
            structure={
                k: (float(v[0]), float(v[1])) for k, v in (d.get("structure") or {}).items()
            },
            emphasis=[ActionEvent.from_dict(a) for a in d.get("emphasis", [])],
            notes=list(d.get("notes", [])),
        )


# --------------------------------------------------------------------------- quality


@dataclass
class QualityCheck:
    name: str
    passed: bool
    detail: str = ""
    severity: str = "error"  # error = block packaging, warn = log only
    value: Any = None

    @property
    def blocking(self) -> bool:
        return not self.passed and self.severity == "error"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "detail": self.detail,
            "severity": self.severity,
            "value": self.value,
        }


@dataclass
class QualityReport:
    checks: list[QualityCheck] = field(default_factory=list)

    def add(
        self,
        name: str,
        passed: bool,
        detail: str = "",
        *,
        severity: str = "error",
        value: Any = None,
    ) -> QualityCheck:
        c = QualityCheck(name, bool(passed), detail, severity, value)
        self.checks.append(c)
        return c

    @property
    def passed(self) -> bool:
        return not any(c.blocking for c in self.checks)

    @property
    def failures(self) -> list[QualityCheck]:
        return [c for c in self.checks if not c.passed]

    @property
    def blocking_failures(self) -> list[QualityCheck]:
        return [c for c in self.checks if c.blocking]

    @property
    def errors(self) -> list[QualityCheck]:
        return self.blocking_failures

    @property
    def warnings(self) -> list[QualityCheck]:
        return [c for c in self.checks if not c.passed and c.severity != "error"]

    def summary(self) -> str:
        ok = sum(1 for c in self.checks if c.passed)
        return f"{ok}/{len(self.checks)} quality gates passed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "summary": self.summary(),
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "checks": [c.to_dict() for c in self.checks],
        }

    def to_markdown(self) -> str:
        lines = [
            "# Quality report",
            "",
            f"**{'PASSED' if self.passed else 'FAILED'}** - {self.summary()} "
            f"({len(self.errors)} blocking, {len(self.warnings)} warning)",
            "",
            "| check | result | detail |",
            "|---|---|---|",
        ]
        for c in self.checks:
            mark = "pass" if c.passed else ("FAIL" if c.severity == "error" else "warn")
            lines.append(f"| {c.name} | {mark} | {c.detail} |")
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Re-exports for v0.4 creative intelligence dataclasses
# ---------------------------------------------------------------------------
def _lazy_reexports():
    try:
        from contentforge.creative.director import CreativePlan
    except ImportError:
        CreativePlan = None  # type: ignore
    try:
        from contentforge.pipeline.creative_qa import EditingScore
    except ImportError:
        EditingScore = None  # type: ignore
    return CreativePlan, EditingScore


CreativePlan, EditingScore = _lazy_reexports()

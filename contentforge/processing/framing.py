"""Content-aware 9:16 composition planning.

v0.2 cropped a fixed 9:16 window around the mouse pointer.  On a real website
that throws away most of the page and regularly slices headings, buttons and
result panels in half.

v0.3 *scores* candidate framings instead.  For a time range the planner
collects everything the understanding stage saw - OCR/text boxes, UI and action
regions, the cursor, the detected content region - and searches for the view
rectangle that maximises

    keep text  -  cut text  +  cursor  +  action  +  content  -  zoom

subject to a hard **maximum zoom**.  Two layouts are considered:

``fill``
    a true 9:16 window inside the source (full-bleed Reel).

``canvas``
    the recording (or a wide part of it) scaled down and placed inside a
    designed 9:16 Reel canvas.  This is what wins when a tight crop would
    destroy the context - a wide dashboard, a results table, a full page view.

The planner is pure and deterministic: same inputs -> same plan, which is what
the regression tests assert.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from contentforge.log import get_logger
from contentforge.models.schemas import Region, TextBox, ViewRect

log = get_logger("framing")


# --------------------------------------------------------------------------- config
@dataclass
class FramingWeights:
    """Relative importance of the framing signals (sane, tuned defaults)."""

    text_kept: float = 1.0  # important text/UI that stays fully visible
    text_cut: float = 2.0  # penalty for slicing text/UI in half
    cursor: float = 0.50  # ONE signal among many - never the only one
    action: float = 0.90  # the region the user is interacting with
    prominence: float = 0.60  # how large that region ends up in the framing
    content: float = 0.35  # the detected website/content area
    legibility: float = 1.20  # mobile readability: how large the pixels end up
    zoom: float = 0.25  # gentle pull towards the wider framing


@dataclass
class FramingConfig:
    target_width: int = 1080
    target_height: int = 1920
    max_zoom: float = 3.6  # source_width / view_width, hard cap
    min_canvas_scale: float = 0.55  # canvas card never smaller than this share of the canvas width
    canvas_bias: float = 0.15  # margin the canvas layout must beat full-bleed by
    focus_falloff: float = 0.20  # how quickly text importance decays away from the action
    focus_boost: float = 4.0  # importance multiplier for text right at the focus
    allow_canvas: bool = True
    x_grid: int = 28
    y_grid: int = 9
    smoothing: float = 0.72  # EMA on the pan path
    max_pan_per_second: float = 0.18  # frame widths / s
    deadzone: float = 0.06  # ignore tiny corrections
    samples_per_shot: int = 7
    weights: FramingWeights = field(default_factory=FramingWeights)
    min_text_keep: float = 0.55

    @property
    def target_aspect(self) -> float:
        return self.target_width / max(1, self.target_height)


@dataclass
class ViewScore:
    total: float = 0.0
    text_kept: float = 0.0
    text_cut: float = 0.0
    cursor: float = 0.0
    action: float = 0.0
    prominence: float = 0.0
    content: float = 0.0
    legibility: float = 0.0
    zoom: float = 1.0

    def to_dict(self) -> dict:
        return {
            "total": round(self.total, 4),
            "text_kept": round(self.text_kept, 4),
            "text_cut": round(self.text_cut, 4),
            "cursor": round(self.cursor, 4),
            "action": round(self.action, 4),
            "prominence": round(self.prominence, 4),
            "content": round(self.content, 4),
            "legibility": round(self.legibility, 4),
            "zoom": round(self.zoom, 3),
        }


@dataclass
class FramingPlan:
    view: ViewRect
    layout: str  # fill | canvas
    score: ViewScore
    metrics: dict = field(default_factory=dict)

    @property
    def zoom(self) -> float:
        return self.view.zoom

    def to_dict(self) -> dict:
        return {
            "view": self.view.to_dict(),
            "layout": self.layout,
            "score": self.score.to_dict(),
            "metrics": self.metrics,
        }


# --------------------------------------------------------------------------- scoring
def score_view(
    view: Region,
    *,
    text_boxes: list[TextBox] | None = None,
    action_regions: list[Region] | None = None,
    cursor: tuple[float, float] | None = None,
    content_region: Region | None = None,
    weights: FramingWeights | None = None,
    source_aspect: float = 16 / 9,
    source_width: int = 1920,
    target_width: int = 1080,
) -> ViewScore:
    """Score one candidate view rectangle. Higher is better.

    ``text_boxes`` may carry *importance* in ``confidence`` (the planner boosts
    boxes near the current action), so "keep the important text" and "do not cut
    text in half" are both measured against what actually matters right now.
    """
    w = weights or FramingWeights()
    boxes = text_boxes or []
    actions = action_regions or []
    s = ViewScore()
    s.zoom = 1.0 / max(1e-6, view.w)

    total_weight = sum(max(0.15, b.confidence) * max(b.region.area, 1e-6) for b in boxes)
    if total_weight > 0:
        kept = 0.0
        cut = 0.0
        for b in boxes:
            weight = max(0.15, b.confidence) * max(b.region.area, 1e-6)
            cov = view.coverage_of(b.region)
            kept += weight * cov
            if 0.02 < cov < 0.98:
                # A line sliced in half is the worst case: it reads as broken.
                # Text that is fully outside the frame is merely absent, and is
                # already accounted for by ``text_kept``.
                cut += weight * (4.0 * cov * (1.0 - cov)) ** 0.7
        s.text_kept = kept / total_weight
        s.text_cut = cut / total_weight

    if actions:
        aw = sum(max(0.2, r.score) for r in actions)
        s.action = (
            sum(max(0.2, r.score) * view.coverage_of(r) for r in actions) / aw if aw else 0.0
        )
        # prominence: an action that fills a decent part of the frame reads on a
        # phone; the same button lost in a full desktop screenshot does not.
        inside = sum(view.intersection_area(r) for r in actions)
        s.prominence = min(1.0, (inside / max(1e-6, view.area)) / 0.08)

    if cursor is not None:
        margin = 0.04
        inner = Region(
            view.x + view.w * margin,
            view.y + view.h * margin,
            view.w * (1 - 2 * margin),
            view.h * (1 - 2 * margin),
        )
        if inner.contains_point(*cursor):
            s.cursor = 1.0
        elif view.contains_point(*cursor):
            s.cursor = 0.6
        else:
            # distance falloff so "just outside" is better than "far away"
            dx = max(view.x - cursor[0], cursor[0] - view.x2, 0.0)
            dy = max(view.y - cursor[1], cursor[1] - view.y2, 0.0)
            s.cursor = max(0.0, 1.0 - (dx + dy) * 4.0) * 0.4

    if content_region is not None and content_region.area > 0:
        s.content = view.coverage_of(content_region)

    # legibility: how big the framed pixels end up on a 1080-wide phone screen.
    # A full 16:9 frame letterboxed into 9:16 is ~0.56x (small text); a 9:16 crop
    # of the same recording is ~1.8x (comfortable).
    scale = target_width / max(1e-6, view.w * max(1, source_width))
    s.legibility = min(1.0, max(0.0, (scale - 0.5) / (1.25 - 0.5)))

    zoom_norm = max(0.0, (s.zoom - 1.0)) / max(1e-6, source_aspect * 2)
    s.total = (
        w.text_kept * s.text_kept
        - w.text_cut * s.text_cut
        + w.cursor * s.cursor
        + w.action * s.action
        + w.prominence * s.prominence
        + w.content * s.content
        + w.legibility * s.legibility
        - w.zoom * zoom_norm
    )
    return s


# --------------------------------------------------------------------------- candidates
def fill_view_size(source_w: int, source_h: int, cfg: FramingConfig, height_frac: float = 1.0) -> tuple[float, float]:
    """Normalised (w, h) of a target-aspect window covering ``height_frac`` of the frame."""
    height_frac = min(1.0, max(0.2, height_frac))
    h_px = source_h * height_frac
    w_px = h_px * cfg.target_aspect
    if w_px > source_w:  # source narrower than the target aspect
        w_px = source_w
        h_px = w_px / cfg.target_aspect
    return w_px / source_w, h_px / source_h


def canvas_view_size(
    source_w: int, source_h: int, cfg: FramingConfig, width_frac: float, aspect: float | None = None
) -> tuple[float, float]:
    """Normalised (w, h) of a wide 'canvas card' view.

    ``aspect`` is the pixel aspect ratio (w/h) of the card; defaults to the
    source aspect so nothing is cropped at all.
    """
    width_frac = min(1.0, max(0.25, width_frac))
    w_px = source_w * width_frac
    a = aspect or (source_w / max(1, source_h))
    h_px = min(source_h, w_px / a)
    return w_px / source_w, h_px / source_h


def candidate_sizes(source_w: int, source_h: int, cfg: FramingConfig) -> list[tuple[str, float, float]]:
    """(layout, w, h) candidates ordered from widest to tightest, max-zoom enforced."""
    out: list[tuple[str, float, float]] = []
    if cfg.allow_canvas:
        for frac, aspect in (
            (1.0, None),
            (0.82, None),
            (0.68, 4 / 3),
            (0.55, 4 / 3),
            (0.45, 1.0),
            (0.36, 1.0),
        ):
            w, h = canvas_view_size(source_w, source_h, cfg, frac, aspect)
            out.append(("canvas", w, h))
    for hf in (1.0, 0.88, 0.75):
        w, h = fill_view_size(source_w, source_h, cfg, hf)
        out.append(("fill", w, h))
    # hard max-zoom gate (zoom == source_width / view_width)
    allowed = [(layout, w, h) for layout, w, h in out if (1.0 / max(1e-6, w)) <= cfg.max_zoom + 1e-6]
    if not allowed:
        widest = max(out, key=lambda c: c[1])
        log.warning(
            "Every framing candidate exceeds max_zoom=%.2f; using the widest (zoom %.2f)",
            cfg.max_zoom,
            1.0 / widest[1],
        )
        allowed = [widest]
    # de-duplicate near-identical sizes
    uniq: list[tuple[str, float, float]] = []
    for c in allowed:
        if not any(c[0] == u[0] and abs(c[1] - u[1]) < 0.01 and abs(c[2] - u[2]) < 0.01 for u in uniq):
            uniq.append(c)
    return uniq


def best_position(
    size: tuple[float, float],
    *,
    text_boxes: list[TextBox],
    action_regions: list[Region],
    cursor: tuple[float, float] | None,
    content_region: Region | None,
    cfg: FramingConfig,
    source_aspect: float,
    source_width: int = 1920,
    weights: FramingWeights | None = None,
) -> tuple[Region, ViewScore]:
    """Grid-search the best (x, y) for a view of the given size."""
    vw, vh = size
    max_x = max(0.0, 1.0 - vw)
    max_y = max(0.0, 1.0 - vh)
    nx = 1 if max_x < 1e-6 else max(3, cfg.x_grid)
    ny = 1 if max_y < 1e-6 else max(3, cfg.y_grid)
    best: tuple[Region, ViewScore] | None = None
    for i in range(nx):
        x = max_x * (i / (nx - 1)) if nx > 1 else 0.0
        for j in range(ny):
            y = max_y * (j / (ny - 1)) if ny > 1 else 0.0
            view = Region(x, y, vw, vh, kind="view")
            sc = score_view(
                view,
                text_boxes=text_boxes,
                action_regions=action_regions,
                cursor=cursor,
                content_region=content_region,
                weights=weights or cfg.weights,
                source_aspect=source_aspect,
                source_width=source_width,
                target_width=cfg.target_width,
            )
            if best is None or sc.total > best[1].total:
                best = (view, sc)
    assert best is not None
    return best


# --------------------------------------------------------------------------- planning
def plan_framing(
    understanding,
    start: float,
    end: float,
    cfg: FramingConfig | None = None,
    *,
    prefer_layout: str | None = None,
    focus_regions: list[Region] | None = None,
) -> FramingPlan:
    """Plan the composition for one shot (``start``..``end`` in source time)."""
    cfg = cfg or FramingConfig()
    sw = int(getattr(understanding, "width", 0) or 1920)
    sh = int(getattr(understanding, "height", 0) or 1080)
    source_aspect = sw / max(1, sh)
    duration = max(0.05, end - start)
    n = max(2, min(cfg.samples_per_shot, int(duration * 2) + 2))
    times = [start + duration * i / (n - 1) for i in range(n)]

    per_time: list[dict] = []
    for t in times:
        boxes = list(understanding.text_boxes_at(t, window=max(1.0, duration / 2)))
        actions = list(focus_regions or []) + [
            r for r in understanding.important_regions(max(start, t - 0.6), min(end, t + 0.6))
            if r.kind == "action"
        ]
        cur = understanding.cursor_at(t)
        cursor = (cur.x, cur.y) if cur is not None and cur.detected else None
        boxes = weight_by_focus(boxes, actions, cursor, cfg)
        per_time.append({"t": t, "boxes": boxes, "actions": actions, "cursor": cursor})

    content = getattr(understanding, "content_region", None)
    candidates = candidate_sizes(sw, sh, cfg)
    if prefer_layout in ("fill", "canvas"):
        filtered = [c for c in candidates if c[0] == prefer_layout]
        candidates = filtered or candidates

    # ---- 1. choose the size/layout that wins on average over the shot
    scored: list[tuple[float, str, float, float, float]] = []
    for layout, vw, vh in candidates:
        totals = 0.0
        kept_totals = 0.0
        has_boxes = 0
        for s in per_time:
            _, sc = best_position(
                (vw, vh),
                text_boxes=s["boxes"],
                action_regions=s["actions"],
                cursor=s["cursor"],
                content_region=content,
                cfg=cfg,
                source_aspect=source_aspect,
                source_width=sw,
            )
            totals += sc.total
            if s["boxes"]:
                kept_totals += sc.text_kept
                has_boxes += 1
        avg = totals / len(per_time)
        if layout == "canvas":
            avg -= cfg.canvas_bias  # fill (full-bleed) is the default look
        avg_kept = (kept_totals / has_boxes) if has_boxes else 1.0
        scored.append((avg, layout, vw, vh, avg_kept))

    # Prioritize candidates meeting the quality gate threshold for text preservation
    meeting_threshold = [c for c in scored if c[4] >= cfg.min_text_keep]
    if meeting_threshold:
        meeting_threshold.sort(key=lambda c: -c[0])
        _, layout, vw, vh, _ = meeting_threshold[0]
    else:
        # If no candidate meets the threshold, fall back to the one that preserves the most text
        scored.sort(key=lambda c: (-c[4], -c[0]))
        _, layout, vw, vh, _ = scored[0]

    # ---- 2. best position at every sample time, then smooth into a camera path
    raw: list[tuple[float, float, float, ViewScore]] = []
    for s in per_time:
        view, sc = best_position(
            (vw, vh),
            text_boxes=s["boxes"],
            action_regions=s["actions"],
            cursor=s["cursor"],
            content_region=content,
            cfg=cfg,
            source_aspect=source_aspect,
            source_width=sw,
        )
        raw.append((s["t"], view.x, view.y, sc))

    xs = smooth_path([(t, x) for t, x, _y, _s in raw], cfg)
    ys = smooth_path([(t, y) for t, _x, y, _s in raw], cfg)
    x0 = xs[0][1]
    y0 = ys[0][1]
    view = ViewRect(
        x=x0,
        y=y0,
        w=vw,
        h=vh,
        x_keyframes=[(round(t - start, 3), round(v, 5)) for t, v in xs],
        y_keyframes=[(round(t - start, 3), round(v, 5)) for t, v in ys],
    )
    if not _moves(view.x_keyframes):
        view.x_keyframes = []
    if not _moves(view.y_keyframes):
        view.y_keyframes = []

    mid = len(raw) // 2
    score = raw[mid][3]
    metrics = {
        "candidates": [
            {"layout": c[1], "w": round(c[2], 4), "score": round(c[0], 4)} for c in scored
        ],
        "samples": len(raw),
        "moves": view.moves,
        "zoom": round(view.zoom, 3),
    }
    log.debug(
        "Framing %.1f-%.1fs -> %s zoom %.2f score %.3f (text kept %.0f%%, cut %.0f%%)",
        start,
        end,
        layout,
        view.zoom,
        score.total,
        score.text_kept * 100,
        score.text_cut * 100,
    )
    return FramingPlan(view=view, layout=layout, score=score, metrics=metrics)


def weight_by_focus(
    boxes: list[TextBox],
    action_regions: list[Region],
    cursor: tuple[float, float] | None,
    cfg: FramingConfig,
) -> list[TextBox]:
    """Boost the importance of text near what the presenter is doing.

    Without this every label on a wide desktop page counts the same and the
    planner can only ever choose "show the whole page, tiny".  With it, the text
    around the current click/typing/cursor dominates, so a tighter framing that
    keeps *that* text intact wins - while text far away still contributes, which
    is what stops the camera from zooming into a meaningless corner.
    """
    if not boxes:
        return boxes
    focus_points: list[tuple[float, float]] = [(r.cx, r.cy) for r in action_regions]
    if cursor is not None:
        focus_points.append(cursor)
    if not focus_points:
        return boxes
    out: list[TextBox] = []
    for b in boxes:
        d = min(
            ((b.region.cx - fx) ** 2 + (b.region.cy - fy) ** 2) ** 0.5 for fx, fy in focus_points
        )
        boost = 1.0 + cfg.focus_boost * pow(2.718281828, -d / max(1e-3, cfg.focus_falloff))
        out.append(
            TextBox(
                region=b.region,
                text=b.text,
                confidence=max(0.05, b.confidence) * boost,
                source=b.source,
            )
        )
    return out


def smooth_path(points: list[tuple[float, float]], cfg: FramingConfig) -> list[tuple[float, float]]:
    """Dead-zone + EMA + max-speed limiter, so the camera glides instead of snapping."""
    if not points:
        return []
    out: list[tuple[float, float]] = [(points[0][0], points[0][1])]
    cur = points[0][1]
    for (t_prev, _), (t, target) in zip(points, points[1:]):
        dt = max(1e-3, t - t_prev)
        if abs(target - cur) < cfg.deadzone:
            target = cur
        nxt = cfg.smoothing * cur + (1 - cfg.smoothing) * target
        max_step = cfg.max_pan_per_second * dt
        nxt = max(cur - max_step, min(cur + max_step, nxt))
        cur = nxt
        out.append((t, cur))
    return out


def _moves(keyframes: list[tuple[float, float]], tol: float = 0.002) -> bool:
    if len(keyframes) < 2:
        return False
    values = [v for _, v in keyframes]
    return (max(values) - min(values)) > tol


# --------------------------------------------------------------------------- verification
def preservation_report(
    view: Region, text_boxes: list[TextBox], *, min_keep: float = 0.55
) -> dict:
    """How much of the important text this view preserves (used by quality gates)."""
    if not text_boxes:
        return {"kept": 1.0, "cut": 0.0, "worst_cut": 0.0, "boxes": 0, "ok": True}
    total = sum(max(0.15, b.confidence) * max(b.region.area, 1e-6) for b in text_boxes)
    kept = 0.0
    cut = 0.0
    worst = 0.0
    for b in text_boxes:
        weight = max(0.15, b.confidence) * max(b.region.area, 1e-6)
        cov = view.coverage_of(b.region)
        kept += weight * cov
        if 0.02 < cov < 0.98:
            cut += weight
            worst = max(worst, 1.0 - cov)
    return {
        "kept": round(kept / total, 4),
        "cut": round(cut / total, 4),
        "worst_cut": round(worst, 4),
        "boxes": len(text_boxes),
        "ok": (kept / total) >= min_keep,
    }

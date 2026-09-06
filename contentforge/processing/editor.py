"""Smart editor: turns understanding into an edit plan, then renders it.

Planning (:func:`plan_edit`)
    dead-time removal, action-based cuts, the viral structure
    (hook / setup / demo / payoff / CTA), per-shot content-aware framing,
    subtle dynamic zooms and the click/emphasis list.

Rendering (:class:`SmartEditor`)
    one ffmpeg pass per shot - crop with a smooth, keyframed camera path,
    optional zoom ramp, either full-bleed 9:16 (``fill``) or the designed Reel
    canvas (``canvas``) - then a stream-copy concat.  Rendering per shot keeps
    each filter graph small (important on a laptop), lets every shot have its
    own zoom/composition and makes the whole thing resumable.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

from contentforge.log import get_logger
from contentforge.models.schemas import ActionEvent, EditPlan, Region, Shot, VideoUnderstanding
from contentforge.processing.cursor import piecewise_linear_expression
from contentforge.processing.framing import FramingConfig, plan_framing
from contentforge.processing.segments import merge_intervals
from contentforge.utils.ffmpeg import FFmpeg

log = get_logger("editor")

Interval = tuple[float, float]


# --------------------------------------------------------------------------- config
@dataclass
class EditConfig:
    max_duration: float = 75.0
    min_duration: float = 12.0
    min_shot: float = 1.0
    max_shot: float = 5.0
    remove_dead_time: bool = True
    dead_padding: float = 0.15
    min_dead_gap: float = 0.9
    max_speedup: float = 1.3
    hook_seconds: float = 2.0
    setup_seconds: float = 3.0
    payoff_fraction: float = 0.80
    cta_seconds: float = 1.8
    dynamic_zoom: bool = True
    zoom_max: float = 1.07  # subtle: 7 % push-in, never a punch
    transitions: bool = True
    transition_duration: float = 0.12
    result_hold: float = 0.6  # extra seconds held on the result reveal


# --------------------------------------------------------------------------- planning
def plan_edit(
    u: VideoUnderstanding,
    *,
    config: EditConfig | None = None,
    framing: FramingConfig | None = None,
    silences: list[Interval] | None = None,
    has_audio: bool = True,
) -> EditPlan:
    """Build the full editing decision list for a recording."""
    cfg = config or EditConfig()
    fcfg = framing or FramingConfig()
    duration = float(u.duration or 0.0)
    plan = EditPlan(source_duration=duration)
    if duration <= 0:
        return plan

    keep = _keep_ranges(u, cfg, silences if has_audio else None)
    kept_total = sum(b - a for a, b in keep)
    plan.removed_seconds = max(0.0, duration - kept_total)

    keep, speed = _fit_budget(keep, u, cfg)
    boundaries = _cut_points(u, keep, cfg)
    shots = _split_for_structure(_build_shots(u, keep, boundaries, cfg, speed), cfg)
    if not shots:
        shots = [
            Shot(
                index=0,
                src_start=0.0,
                src_end=min(duration, cfg.max_duration),
                out_start=0.0,
                view=None,  # type: ignore[arg-type]
            )
        ]

    _assign_roles(shots, u, cfg)
    _plan_views(shots, u, fcfg, cfg)

    plan.shots = shots
    plan.structure = _structure(shots)
    plan.emphasis = _emphasis(u, plan)
    plan.notes = [
        f"dead time removed: {plan.removed_seconds:.1f}s",
        f"shots: {len(shots)}",
        f"speed: {speed:.2f}x" if abs(speed - 1.0) > 0.01 else "speed: 1.00x",
        "layouts: " + ", ".join(sorted({s.layout for s in shots})),
    ]
    log.info(
        "Edit plan: %d shots, %.1fs -> %.1fs (removed %.1fs, speed %.2fx), layouts %s",
        len(shots),
        duration,
        plan.duration,
        plan.removed_seconds,
        speed,
        sorted({s.layout for s in shots}),
    )
    return plan


def _keep_ranges(u: VideoUnderstanding, cfg: EditConfig, silences: list[Interval] | None) -> list[Interval]:
    """Everything worth keeping: the recording minus true dead time."""
    duration = u.duration
    if not cfg.remove_dead_time:
        return [(0.0, duration)]
    idle = merge_intervals([(a.start, a.end) for a in u.actions if a.kind == "idle"])
    dead: list[Interval] = []
    if silences:
        quiet = merge_intervals([(max(0.0, s), min(duration, e)) for s, e in silences])
        for a, b in idle:
            for c, d in quiet:
                s, e = max(a, c), min(b, d)
                if e - s >= cfg.min_dead_gap:
                    dead.append((s, e))
    else:
        dead = [(a, b) for a, b in idle if b - a >= cfg.min_dead_gap]
    dead = merge_intervals(dead)
    # keep a little of every pause so cuts do not feel like glitches
    reveals = [a.end for a in u.actions if a.kind in ("reveal", "navigate")]
    padded: list[Interval] = []
    for a, b in dead:
        s, e = a + cfg.dead_padding, b - cfg.dead_padding
        # never cut away the moment right after a result appears: the viewer
        # needs a beat to actually read it
        for r in reveals:
            if r <= s <= r + 1.0:
                s = min(b - 0.2, s + cfg.result_hold)
        if e - s >= 0.3:
            padded.append((s, e))
    keep: list[Interval] = []
    cursor = 0.0
    for a, b in padded:
        if a - cursor > 0.35:
            keep.append((cursor, a))
        cursor = b
    if duration - cursor > 0.35:
        keep.append((cursor, duration))
    return keep or [(0.0, duration)]


def _fit_budget(keep: list[Interval], u: VideoUnderstanding, cfg: EditConfig) -> tuple[list[Interval], float]:
    """Trim/accelerate the kept material so the Reel fits ``max_duration``."""
    total = sum(b - a for a, b in keep)
    if total <= cfg.max_duration:
        return keep, 1.0
    speed = min(cfg.max_speedup, total / cfg.max_duration)
    remaining = cfg.max_duration * speed
    if total <= remaining:
        return keep, speed

    # Score each range: actions are interesting, the opening and the final
    # result are structurally required.
    scored: list[tuple[float, int, Interval]] = []
    for i, (a, b) in enumerate(keep):
        acts = [e for e in u.actions_between(a, b) if e.kind in ("click", "type", "reveal", "navigate", "scroll")]
        density = len(acts) / max(0.5, b - a)
        bonus = 0.0
        if i == 0:
            bonus += 2.0  # hook material
        if b >= u.duration - 6.0:
            bonus += 1.6  # the result/payoff
        scored.append((density + bonus, i, (a, b)))
    scored.sort(key=lambda s: -s[0])
    chosen: list[Interval] = []
    budget = remaining
    for _score, _i, (a, b) in scored:
        if budget <= 0.5:
            break
        span = min(b - a, budget)
        chosen.append((a, a + span))
        budget -= span
    chosen.sort()
    return merge_intervals(chosen), speed


def _cut_points(u: VideoUnderstanding, keep: list[Interval], cfg: EditConfig) -> list[float]:
    """Source timestamps where a cut feels motivated (an action starts)."""
    points: list[float] = []
    for a in u.actions:
        if a.kind in ("click", "navigate", "reveal", "scroll", "type"):
            points.append(a.start)
    inside: list[float] = []
    for p in sorted(points):
        if any(s + 0.4 <= p <= e - 0.4 for s, e in keep):
            inside.append(p)
    return inside


def _build_shots(
    u: VideoUnderstanding,
    keep: list[Interval],
    boundaries: list[float],
    cfg: EditConfig,
    speed: float,
) -> list[Shot]:
    shots: list[Shot] = []
    out_t = 0.0
    idx = 0
    for a, b in keep:
        marks = [p for p in boundaries if a < p < b]
        edges = [a] + marks + [b]
        # enforce min/max shot length
        merged: list[float] = [edges[0]]
        for e in edges[1:]:
            if e - merged[-1] < cfg.min_shot and e != edges[-1]:
                continue
            merged.append(e)
        final: list[float] = [merged[0]]
        for e in merged[1:]:
            while e - final[-1] > cfg.max_shot:
                final.append(final[-1] + cfg.max_shot)
            final.append(e)
        for s, e in zip(final, final[1:]):
            if e - s < 0.25:
                continue
            acts = [x for x in u.actions_between(s, e) if x.kind not in ("idle", "move")]
            kind = acts[0].kind if acts else ""
            label = next((x.label for x in acts if x.label), "")
            shot = Shot(
                index=idx,
                src_start=s,
                src_end=e,
                out_start=out_t,
                view=None,  # type: ignore[arg-type]
                action_kind=kind,
                label=label,
                speed=speed,
            )
            shots.append(shot)
            out_t += shot.out_duration
            idx += 1
    return shots


def _split_for_structure(shots: list[Shot], cfg: EditConfig) -> list[Shot]:
    """Cut shots that straddle a structural boundary (hook / setup / CTA).

    Without this a single long opening shot would swallow the hook, and the
    viral structure could only be applied by force. Boundaries are honoured
    only when both halves are long enough to stand on their own.
    """
    if not shots:
        return shots
    total = shots[-1].out_end
    marks = [cfg.hook_seconds, cfg.hook_seconds + cfg.setup_seconds]
    if total > 8:
        marks.append(total - cfg.cta_seconds)
    for mark in sorted(m for m in marks if 0 < m < total):
        for i, shot in enumerate(shots):
            if not (shot.out_start + 0.6 <= mark <= shot.out_end - 0.6):
                continue
            src_mark = shot.src_start + (mark - shot.out_start) * shot.speed
            head = replace(shot, src_end=src_mark)
            tail = replace(shot, src_start=src_mark, out_start=mark, index=shot.index + 1)
            shots = shots[:i] + [head, tail] + shots[i + 1 :]
            break
    out_t = 0.0
    for i, shot in enumerate(shots):
        shot.index = i
        shot.out_start = out_t
        out_t += shot.out_duration
    return shots


def _assign_roles(shots: list[Shot], u: VideoUnderstanding, cfg: EditConfig) -> None:
    """Viral structure, applied to the *actual* shot timing (never forced blindly)."""
    if not shots:
        return
    total = shots[-1].out_end
    payoff_start = total * cfg.payoff_fraction
    cta_start = max(payoff_start, total - cfg.cta_seconds)
    # the strongest reveal (if any) anchors the payoff instead of a fixed %
    reveals = [a for a in u.actions if a.kind == "reveal"]
    if reveals:
        best = max(reveals, key=lambda a: (a.confidence, a.start))
        for s in shots:
            if s.src_start <= best.start < s.src_end:
                payoff_start = min(payoff_start, s.out_start)
                break
    for s in shots:
        mid = (s.out_start + s.out_end) / 2
        if s.index == 0 or mid <= cfg.hook_seconds:
            s.role = "hook"
        elif mid <= cfg.hook_seconds + cfg.setup_seconds + 0.5:
            s.role = "setup"
        elif mid >= cta_start and total > 8:
            s.role = "cta"
        elif mid >= payoff_start:
            s.role = "payoff"
        else:
            s.role = "demo"


def _plan_views(shots: list[Shot], u: VideoUnderstanding, fcfg: FramingConfig, cfg: EditConfig) -> None:
    for s in shots:
        focus = [
            a.region
            for a in u.actions_between(s.src_start, s.src_end)
            if a.region is not None and a.kind in ("click", "type", "reveal")
        ]
        prefer = None
        if s.role in ("payoff", "cta"):
            prefer = None  # let the score decide; results usually need context
        fp = plan_framing(u, s.src_start, s.src_end, fcfg, prefer_layout=prefer, focus_regions=focus)
        s.view = fp.view
        s.layout = fp.layout
        if cfg.dynamic_zoom:
            if s.role == "hook":
                s.zoom_from, s.zoom_to = 1.0, min(cfg.zoom_max, 1.05)
            elif s.action_kind in ("click", "type"):
                s.zoom_from, s.zoom_to = 1.0, cfg.zoom_max
            elif s.role == "payoff":
                s.zoom_from, s.zoom_to = min(cfg.zoom_max, 1.05), 1.0  # pull out to reveal
            else:
                s.zoom_from = s.zoom_to = 1.0


def _structure(shots: list[Shot]) -> dict[str, tuple[float, float]]:
    out: dict[str, tuple[float, float]] = {}
    for s in shots:
        a, b = out.get(s.role, (s.out_start, s.out_end))
        out[s.role] = (min(a, s.out_start), max(b, s.out_end))
    return out


def _emphasis(u: VideoUnderstanding, plan: EditPlan) -> list[ActionEvent]:
    """Clicks (and reveals) remapped to output time, for on-screen effects."""
    out: list[ActionEvent] = []
    for a in u.actions:
        if a.kind not in ("click", "reveal"):
            continue
        for s in plan.shots:
            if s.src_start <= a.mid < s.src_end:
                t = s.out_start + (a.mid - s.src_start) / max(0.1, s.speed)
                out.append(
                    ActionEvent(
                        start=max(0.0, t - 0.1),
                        end=t + 0.5,
                        kind=a.kind,
                        region=a.region,
                        label=a.label,
                        confidence=a.confidence,
                    )
                )
                break
    return out


# --------------------------------------------------------------------------- rendering
@dataclass
class RenderSettings:
    width: int = 1080
    height: int = 1920
    fps: int = 30
    crf: int = 20
    preset: str = "medium"
    pix_fmt: str = "yuv420p"
    background: str = "#0F0F14"
    accent: str = "#FF7A00"
    card_margin: int = 36
    card_offset: int = -70  # card sits slightly above centre; captions live below
    transitions: bool = True
    transition_duration: float = 0.12
    zoom_headroom: float = 1.12


@dataclass
class RenderResult:
    path: Path
    duration: float
    shots: int
    shot_files: list[Path] = field(default_factory=list)


class SmartEditor:
    """Renders an :class:`EditPlan` into a silent 9:16 video."""

    def __init__(self, settings: RenderSettings, ffmpeg: FFmpeg | None = None):
        self.s = settings
        self.ff = ffmpeg or FFmpeg()

    # ------------------------------------------------------------------ api
    def render(self, src: Path, plan: EditPlan, dst: Path, *, work_dir: Path | None = None) -> RenderResult:
        work = work_dir or dst.parent / "shots"
        work.mkdir(parents=True, exist_ok=True)
        info = self.ff.probe(src)
        size = (info.width, info.height)
        files: list[Path] = []
        prev: Shot | None = None
        for i, shot in enumerate(plan.shots):
            out = work / f"shot_{shot.index:03d}.mp4"
            # A transition only makes sense where the source actually jumps
            # (dead time removed). Consecutive shots of the same take are hard
            # cuts - fading those would just look like a flicker.
            jump_in = prev is not None and abs(shot.src_start - prev.src_end) > 0.05
            nxt = plan.shots[i + 1] if i + 1 < len(plan.shots) else None
            jump_out = nxt is not None and abs(nxt.src_start - shot.src_end) > 0.05
            self.render_shot(src, shot, out, source_size=size, fade_in=jump_in, fade_out=jump_out)
            files.append(out)
            prev = shot
        if not files:
            raise RuntimeError("edit plan contains no shots")
        if len(files) == 1:
            files[0].replace(dst)
        else:
            self._concat(files, dst, work)
        for f in files:
            f.unlink(missing_ok=True)
        duration = self.ff.duration(dst)
        log.info("Rendered %d shots -> %s (%.2fs)", len(plan.shots), dst.name, duration)
        return RenderResult(path=dst, duration=duration, shots=len(plan.shots))

    # --------------------------------------------------------------- shots
    def render_shot(
        self,
        src: Path,
        shot: Shot,
        dst: Path,
        *,
        source_size: tuple[int, int],
        fade_in: bool = False,
        fade_out: bool = False,
    ) -> Path:
        sw, sh = source_size
        if not sw or not sh:
            info = self.ff.probe(src)
            sw, sh = info.width, info.height
        graph = self.shot_filter(shot, sw, sh, fade_in=fade_in, fade_out=fade_out)
        args = [
            "-ss",
            f"{shot.src_start:.3f}",
            "-t",
            f"{shot.src_duration:.3f}",
            "-i",
            str(src),
            "-filter_complex",
            graph,
            "-map",
            "[v]",
            "-an",
            "-r",
            str(self.s.fps),
            "-c:v",
            "libx264",
            "-preset",
            self.s.preset,
            "-crf",
            str(self.s.crf),
            "-pix_fmt",
            self.s.pix_fmt,
            "-vsync",
            "cfr",
            str(dst),
        ]
        self.ff.run(args, timeout=3600)
        return dst

    def shot_filter(
        self, shot: Shot, sw: int, sh: int, *, fade_in: bool = False, fade_out: bool = False
    ) -> str:
        """Build the filter graph for one shot (crop -> layout -> zoom -> fade)."""
        tw, th = self.s.width, self.s.height
        vw = _even(shot.view.w * sw)
        vh = _even(shot.view.h * sh)
        vw = min(vw, _even(sw))
        vh = min(vh, _even(sh))
        x_expr = _keyframe_expression(shot.view.x_keyframes, shot.view.x, sw, vw, shot.speed)
        y_expr = _keyframe_expression(shot.view.y_keyframes, shot.view.y, sh, vh, shot.speed)

        parts: list[str] = []
        chain = ["setpts=PTS-STARTPTS"]
        if abs(shot.speed - 1.0) > 0.01:
            chain.append(f"setpts=PTS/{shot.speed:.5f}")
        chain.append(f"crop={vw}:{vh}:x='{x_expr}':y='{y_expr}'")
        chain.append("setsar=1")

        zooming = abs(shot.zoom_to - shot.zoom_from) > 1e-3 or shot.zoom_from > 1.001
        if shot.layout == "canvas":
            card_w = _even(tw - 2 * self.s.card_margin)
            card_h = _even(card_w * vh / max(1, vw))
            card_h = min(card_h, _even(th * 0.72))
            card_w = _even(min(card_w, card_h * vw / max(1, vh)))
            card_y = max(60, int((th - card_h) / 2 + self.s.card_offset))
            parts.append("[0:v]" + ",".join(chain) + ",split=2[card][bgsrc]")
            parts.append(
                f"[bgsrc]scale={tw}:{th}:force_original_aspect_ratio=increase,"
                f"crop={tw}:{th},gblur=sigma=32,eq=brightness=-0.18:saturation=1.25,"
                f"drawbox=x=0:y=0:w=iw:h=ih:color={_c(self.s.background)}@0.55:t=fill[bg]"
            )
            if zooming:
                # zoom *inside* the card: magnifying the whole canvas would cut
                # the card (and its border) off at the edges
                zw = _even(card_w * self.s.zoom_headroom)
                zh = _even(card_h * self.s.zoom_headroom)
                parts.append(f"[card]scale={zw}:{zh}[cardz]")
                parts.append(f"[cardz]{self._zoom_filter(shot, card_w, card_h)}[cardv]")
            else:
                parts.append(f"[card]scale={card_w}:{card_h}[cardv]")
            parts.append(
                f"[bg][cardv]overlay=(W-w)/2:{card_y}:format=auto[withcard]"
            )
            border = (
                f"[withcard]drawbox=x=(iw-{card_w})/2-4:y={card_y - 4}:w={card_w + 8}:h={card_h + 8}:"
                f"color={_c(self.s.accent)}@0.85:t=4[bordered]"
            )
            parts.append(border.replace("[bordered]", "[v0]"))
            parts.append(f"[v0]format={self.s.pix_fmt}{self._fade(shot, fade_in, fade_out)}[v]")
        else:
            head = "[0:v]" + ",".join(chain)
            fade = self._fade(shot, fade_in, fade_out)
            if zooming:
                zw = _even(tw * self.s.zoom_headroom)
                zh = _even(th * self.s.zoom_headroom)
                parts.append(f"{head},scale={zw}:{zh}[scaled]")
                parts.append(f"[scaled]{self._zoom_filter(shot, tw, th)}{fade}[v]")
            else:
                parts.append(f"{head},scale={tw}:{th},format={self.s.pix_fmt}{fade}[v]")
        return ";".join(parts)

    def _fade(self, shot: Shot, fade_in: bool, fade_out: bool) -> str:
        """A very short dip at real cuts only (where the source jumps)."""
        if not self.s.transitions:
            return ""
        d = max(0.04, min(self.s.transition_duration, shot.out_duration / 3))
        bits = []
        if fade_in:
            bits.append(f"fade=t=in:st=0:d={d:.3f}")
        if fade_out:
            bits.append(f"fade=t=out:st={max(0.0, shot.out_duration - d):.3f}:d={d:.3f}")
        return ("," + ",".join(bits)) if bits else ""

    def _zoom_filter(self, shot: Shot, tw: int, th: int) -> str:
        d = max(0.2, shot.out_duration)
        z0 = max(1.0, shot.zoom_from)
        z1 = max(1.0, shot.zoom_to)
        # smooth cosine ease between the two zoom levels
        expr = (
            f"{z0:.4f}+({z1 - z0:.4f})*(0.5-0.5*cos(PI*min(1,max(0,(on/{self.s.fps})/{d:.3f}))))"
        )
        return (
            f"zoompan=z='{expr}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            f"d=1:s={tw}x{th}:fps={self.s.fps},format={self.s.pix_fmt}"
        )

    # -------------------------------------------------------------- concat
    def _concat(self, files: list[Path], dst: Path, work: Path) -> None:
        listing = work / "concat.txt"
        listing.write_text("".join(f"file '{f.resolve()}'\n" for f in files))
        try:
            self.ff.run(
                ["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(dst)],
                timeout=3600,
            )
        except Exception:  # pragma: no cover - re-encode fallback
            log.warning("Stream-copy concat failed; re-encoding")
            self.ff.run(
                [
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(listing),
                    "-c:v",
                    "libx264",
                    "-preset",
                    self.s.preset,
                    "-crf",
                    str(self.s.crf),
                    "-pix_fmt",
                    self.s.pix_fmt,
                    str(dst),
                ],
                timeout=3600,
            )
        listing.unlink(missing_ok=True)


# --------------------------------------------------------------------------- helpers
def _even(v: float) -> int:
    n = int(round(v))
    n = max(2, n)
    return n - (n % 2)


def _c(hex_color: str) -> str:
    return "0x" + hex_color.lstrip("#")


def _keyframe_expression(
    keyframes: list[tuple[float, float]], static: float, source_px: int, view_px: int, speed: float
) -> str:
    """FFmpeg ``crop`` x/y expression (source px) from normalised keyframes."""
    max_v = max(0, source_px - view_px)
    if not keyframes or len(keyframes) < 2:
        return f"{min(max_v, max(0, int(round(static * source_px))))}"
    pts = [
        (round(t / max(0.05, speed), 3), float(min(max_v, max(0, v * source_px))))
        for t, v in keyframes
    ]
    expr = piecewise_linear_expression([pts], var="t", precision=1)
    return f"clip({expr},0,{max_v})"


def card_rect(
    settings: RenderSettings, view_w_px: float, view_h_px: float
) -> Region:
    """Where the video card sits inside the ``canvas`` layout (normalised output coords).

    Mirrors the arithmetic in :meth:`SmartEditor.shot_filter` so captions, click
    effects and quality gates know exactly where the recording is on screen.
    """
    tw, th = settings.width, settings.height
    card_w = _even(tw - 2 * settings.card_margin)
    card_h = _even(card_w * view_h_px / max(1.0, view_w_px))
    card_h = min(card_h, _even(th * 0.72))
    card_w = _even(min(card_w, card_h * view_w_px / max(1.0, view_h_px)))
    card_y = max(60, int((th - card_h) / 2 + settings.card_offset))
    card_x = (tw - card_w) / 2
    return Region(card_x / tw, card_y / th, card_w / tw, card_h / th, kind="card")


def source_point_to_output(
    shot: Shot,
    point: tuple[float, float],
    t: float,
    settings: RenderSettings,
    source_size: tuple[int, int],
) -> tuple[float, float] | None:
    """Map a normalised *source* point to normalised *output canvas* coordinates.

    Returns ``None`` when the point is outside the visible view (e.g. the cursor
    left the framed area), which is exactly what the quality gate needs to know.
    """
    view = shot.view_at_output(t)
    if not view.contains_point(*point):
        return None
    u = (point[0] - view.x) / max(1e-6, view.w)
    v = (point[1] - view.y) / max(1e-6, view.h)
    if shot.layout != "canvas":
        return (u, v)
    sw, sh = source_size
    card = card_rect(settings, view.w * max(1, sw), view.h * max(1, sh))
    return (card.x + u * card.w, card.y + v * card.h)


def visible_output_region(
    shot: Shot, t: float, settings: RenderSettings, source_size: tuple[int, int]
) -> Region:
    """Where the recording itself is drawn on the output canvas at time ``t``."""
    if shot.layout != "canvas":
        return Region(0.0, 0.0, 1.0, 1.0, kind="card")
    view = shot.view_at_output(t)
    sw, sh = source_size
    return card_rect(settings, view.w * max(1, sw), view.h * max(1, sh))


def estimate_render_seconds(plan: EditPlan) -> float:
    """Rough cost model used for logging/progress (not a hard guarantee)."""
    return sum(max(0.4, s.out_duration) * (1.6 if s.layout == "canvas" else 1.0) for s in plan.shots)

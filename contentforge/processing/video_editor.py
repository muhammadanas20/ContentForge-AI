"""FFmpeg-driven video editing: cuts, 9:16 crop, zoom, transitions and final encode.

The editor works in two render passes to keep filter graphs manageable and
each step resumable:

1. :meth:`VideoEditor.render_cut` - apply the keep-list (silence/jump cuts),
   crop to vertical, apply zoom pulses and micro fade transitions. Produces a
   silent(!) vertical video in output time.
2. :meth:`VideoEditor.render_final` - burn the overlay ASS (subtitles, progress
   bar, branding, intro/outro cards) and mux the mixed audio track.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

from contentforge.config.schema import CursorFollowConfig, VideoConfig
from contentforge.log import get_logger
from contentforge.processing.cursor import (
    CursorTrack,
    Keyframe,
    evaluate_keyframes,
    follow_path,
    piecewise_linear_expression,
    simplify_keyframes,
)
from contentforge.processing.segments import Interval
from contentforge.utils.ffmpeg import FFmpeg, MediaInfo

log = get_logger("video")


@dataclass
class CropPlan:
    """Crop rectangle in source pixels producing a 9:16 region.

    ``x``/``y`` are the static position.  When ``x_keyframes`` is set the crop
    window pans horizontally: each inner list holds ``(output_time, x_px)``
    keyframes of one kept segment; values are interpolated linearly inside a
    segment and held across cuts.  The window never leaves the frame - the
    planner clamps every keyframe and the expression is clamped again for
    safety.
    """

    x: int
    y: int
    w: int
    h: int
    mode: str
    x_keyframes: list[list[Keyframe]] = field(default_factory=list)
    src_width: int = 0

    @property
    def dynamic(self) -> bool:
        return bool(self.x_keyframes) and any(len(seg) for seg in self.x_keyframes)

    def x_at(self, t: float) -> float:
        """Crop left edge (px) at output time ``t``."""
        if not self.dynamic:
            return float(self.x)
        return evaluate_keyframes(self.x_keyframes, t)

    @property
    def ffmpeg(self) -> str:
        if not self.dynamic:
            return f"crop={self.w}:{self.h}:{self.x}:{self.y}"
        max_x = max(0, (self.src_width or (self.x + self.w)) - self.w)
        expr = piecewise_linear_expression(self.x_keyframes, var="t", precision=2)
        return f"crop={self.w}:{self.h}:x='clip({expr},0,{max_x})':y={self.y}"

    def to_dict(self) -> dict:
        d = {"x": self.x, "y": self.y, "w": self.w, "h": self.h, "mode": self.mode}
        if self.dynamic:
            d["dynamic"] = True
            d["keyframes"] = sum(len(seg) for seg in self.x_keyframes)
            xs = [v for seg in self.x_keyframes for _, v in seg]
            d["x_min"], d["x_max"] = int(min(xs)), int(max(xs))
        return d


@dataclass
class ZoomPulse:
    """A zoom-in/zoom-out event in *output* time."""

    start: float
    duration: float
    max_zoom: float
    center_x: float = 0.5  # fraction of frame
    center_y: float = 0.5


def plan_crop(
    info: MediaInfo, target_w: int, target_h: int, mode: str, center_x: float = 0.5
) -> CropPlan:
    """Compute a crop rectangle with the target aspect ratio inside the source frame.

    For a 16:9 screen recording and a 9:16 target this picks the tallest
    possible strip and slides it horizontally to ``center_x``.
    """
    sw, sh = info.width, info.height
    target_ratio = target_w / target_h
    src_ratio = sw / sh if sh else target_ratio
    if src_ratio > target_ratio:
        # too wide -> full height, narrower width
        h = sh
        w = int(round(sh * target_ratio))
    else:
        w = sw
        h = int(round(sw / target_ratio))
    w -= w % 2
    h -= h % 2
    if mode == "left":
        cx = 0.0
    elif mode == "right":
        cx = 1.0
    elif mode == "center" or mode == "blur-pad":
        cx = 0.5
    else:  # smart
        cx = center_x
    x = int(round(cx * sw - w / 2))
    x = max(0, min(sw - w, x))
    y = max(0, (sh - h) // 2)
    return CropPlan(x=x, y=y, w=w, h=h, mode=mode, src_width=sw)


def plan_cursor_crop(
    base: CropPlan,
    track: CursorTrack,
    keep: list[Interval],
    cfg: CursorFollowConfig,
) -> CropPlan:
    """Turn a cursor track (source time) into a panning crop plan (output time).

    Returns ``base`` unchanged (static crop) when the track is not reliable,
    when the source is not wider than the crop, or when the cursor never
    leaves the static window - so the fallback is always the proven static path.
    """
    if not track.is_reliable(cfg.min_detections, cfg.min_coverage):
        log.info(
            "Cursor follow: track unreliable (%d detections, %.0f%% coverage) -> static crop",
            track.detections,
            track.coverage * 100,
        )
        return base
    sw = base.src_width or track.width
    if sw <= base.w or not track.samples:
        return base
    window = base.w / sw
    times, xs, _ys = track.positions()
    segments: list[list[Keyframe]] = []
    offset = 0.0
    left: float | None = None  # first segment starts centred on the cursor (no opening pan)
    all_left: list[float] = []
    prev_end: float | None = None
    for s, e in keep:
        if prev_end is not None and s - prev_end >= cfg.snap_gap_seconds:
            left = None  # long cut: content changes anyway, re-centre on the cursor
        prev_end = e
        seg_t = [t for t in times if s <= t < e]
        seg_x = [x for t, x in zip(times, xs) if s <= t < e]
        seg_len = e - s
        if not seg_t:
            # nothing sampled inside this segment: hold the current position
            hold = base.x / sw if left is None else left
            segments.append([(offset, hold * sw), (offset + seg_len, hold * sw)])
            offset += seg_len
            continue
        # anchor the start of the segment so the expression covers [offset, offset+len)
        if seg_t[0] > s:
            seg_t.insert(0, s)
            seg_x.insert(0, seg_x[0])
        path = follow_path(
            seg_t,
            seg_x,
            window=window,
            deadzone=cfg.deadzone,
            smoothing=cfg.smoothing,
            max_speed=cfg.max_speed,
            start=left,
        )
        left = path[-1]
        pts: list[Keyframe] = [(offset + (t - s), p * sw) for t, p in zip(seg_t, path)]
        pts.append((offset + seg_len, left * sw))
        pts = simplify_keyframes(pts, cfg.keyframe_tolerance * sw)
        segments.append(pts)
        all_left.extend(path)
        offset += seg_len
    if not all_left:
        return base
    max_x = sw - base.w
    if max(all_left) * sw - min(all_left) * sw < 1.0:
        # never moves - keep static crop at the followed position
        x = int(round(min(max_x, max(0.0, all_left[0] * sw))))
        return CropPlan(x=x, y=base.y, w=base.w, h=base.h, mode=base.mode, src_width=sw)
    clamped = [[(round(t, 3), float(max(0.0, min(max_x, v)))) for t, v in seg] for seg in segments]
    first_x = int(round(clamped[0][0][1]))
    plan = CropPlan(
        x=first_x,
        y=base.y,
        w=base.w,
        h=base.h,
        mode="cursor",
        x_keyframes=clamped,
        src_width=sw,
    )
    log.info(
        "Cursor follow: %d keyframes over %d segments, x range %d..%d px",
        sum(len(seg) for seg in clamped),
        len(clamped),
        int(min(v for seg in clamped for _, v in seg)),
        int(max(v for seg in clamped for _, v in seg)),
    )
    return plan


def plan_zoom_pulses(
    output_duration: float,
    *,
    interval: float,
    duration: float,
    max_zoom: float,
    anchors: list[float] | None = None,
) -> list[ZoomPulse]:
    """Schedule zoom pulses every ``interval`` seconds (or at supplied anchors, e.g. sentence starts)."""
    pulses: list[ZoomPulse] = []
    if output_duration <= duration + 1.0:
        return pulses
    times = (
        anchors if anchors else [t for t in _frange(interval, output_duration - duration, interval)]
    )
    last_end = -1.0
    for t in times:
        if t < 1.0 or t + duration > output_duration - 0.5 or t < last_end + 1.0:
            continue
        pulses.append(ZoomPulse(start=t, duration=duration, max_zoom=max_zoom))
        last_end = t + duration
    return pulses


def _frange(start: float, stop: float, step: float):
    t = start
    while t <= stop:
        yield t
        t += step


def zoom_expression(pulses: list[ZoomPulse], ease: str = "in-out", var: str = "time") -> str:
    """Build an ffmpeg expression for the zoom factor over time, in [1, max_zoom].

    Each pulse rises for the first 40 %, holds, then falls over the last 40 %.
    Implemented with ``between`` + cosine easing so it evaluates per frame inside
    ``zoompan`` (whose time variable is ``time``).
    """
    if not pulses:
        return "1"
    t = var
    parts = []
    for p in pulses:
        s, d, z = p.start, p.duration, p.max_zoom - 1.0
        rise = d * 0.4
        fall_start = s + d - rise
        if ease == "in-out":
            up = f"(0.5-0.5*cos(PI*({t}-{s:.3f})/{rise:.3f}))"
            down = f"(0.5+0.5*cos(PI*({t}-{fall_start:.3f})/{rise:.3f}))"
        else:
            up = f"(({t}-{s:.3f})/{rise:.3f})"
            down = f"(1-({t}-{fall_start:.3f})/{rise:.3f})"
        parts.append(
            f"{z:.4f}*(between({t},{s:.3f},{s + rise:.3f})*{up}"
            f"+between({t},{s + rise:.3f},{fall_start:.3f})"
            f"+between({t},{fall_start:.3f},{s + d:.3f})*{down})"
        )
    return "1+" + "+".join(parts)


class VideoEditor:
    """High-level editing operations built on :class:`FFmpeg`."""

    def __init__(self, config: VideoConfig, ffmpeg: FFmpeg | None = None):
        self.config = config
        self.ff = ffmpeg or FFmpeg()

    # ------------------------------------------------------------ pass 1
    def render_cut(
        self,
        src: Path,
        dst: Path,
        *,
        keep: list[Interval],
        crop: CropPlan,
        zoom_pulses: list[ZoomPulse] | None = None,
        output_duration: float | None = None,
    ) -> Path:
        """Cut, crop, zoom and encode a silent vertical video."""
        cfg = self.config
        W, H, fps = cfg.width, cfg.height, cfg.fps
        n = len(keep)
        filters: list[str] = []

        # 1) trim each kept range, reset timestamps and apply micro fades per segment.
        #    Fades are applied *before* concat: a fade-out that ends exactly at the
        #    segment end never blacks out the following material.
        use_fade = cfg.transitions.enabled and cfg.transitions.type == "fade" and n > 1
        fd = cfg.transitions.duration
        for i, (s, e) in enumerate(keep):
            seg = [f"trim=start={s:.3f}:end={e:.3f}", "setpts=PTS-STARTPTS"]
            seg_len = e - s
            if use_fade and seg_len > fd * 3:
                if i > 0:
                    seg.append(f"fade=t=in:st=0:d={fd:.3f}")
                if i < n - 1:
                    seg.append(f"fade=t=out:st={seg_len - fd:.3f}:d={fd:.3f}")
            filters.append(f"[0:v]{','.join(seg)}[v{i}]")
        if n > 1:
            inputs = "".join(f"[v{i}]" for i in range(n))
            filters.append(f"{inputs}concat=n={n}:v=1:a=0[vc]")
            chain_in = "[vc]"
        else:
            chain_in = "[v0]"

        # 2) crop to 9:16 region, scale up to output size
        chain = [crop.ffmpeg, f"scale={W}:{H}:flags=lanczos", "setsar=1", f"fps={fps}"]

        # 3) zoom pulses: crop a smaller window around centre then scale back up
        if zoom_pulses:
            z = zoom_expression(zoom_pulses, cfg.zoom.ease, var="time")
            chain.append(
                f"zoompan=z='{z}':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps={fps}"
            )

        chain.append(f"format={cfg.pix_fmt}")
        filters.append(f"{chain_in}{','.join(chain)}[vout]")
        graph = ";".join(filters)

        args = [
            "-i",
            str(src),
            "-filter_complex",
            graph,
            "-map",
            "[vout]",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            cfg.preset,
            "-crf",
            str(cfg.crf),
        ]
        if output_duration:
            args += ["-t", f"{output_duration:.3f}"]
        args.append(str(dst))
        log.info(
            "Rendering cut/crop/zoom pass (%d segments, %d zoom pulses)", n, len(zoom_pulses or [])
        )
        self.ff.run(args, timeout=7200)
        return dst

    # ------------------------------------------------------------ pass 2
    def render_final(
        self,
        video: Path,
        audio: Path | None,
        dst: Path,
        *,
        ass_path: Path | None = None,
        fonts_dir: Path | None = None,
        duration: float | None = None,
        logo: Path | None = None,
        logo_position: tuple[int, int] | None = None,
        logo_width: int | None = None,
    ) -> Path:
        """Burn overlays (ASS) and mux audio into the deliverable MP4."""
        cfg = self.config
        args: list[str] = ["-i", str(video)]
        if audio:
            args += ["-i", str(audio)]
        if logo:
            args += ["-i", str(logo)]

        vf: list[str] = []
        filter_complex: list[str] = []
        v_label = "[0:v]"
        if logo:
            lw = logo_width or cfg.branding.logo_width
            lx, ly = logo_position or (cfg.width - lw - 40, 40)
            logo_idx = 2 if audio else 1
            filter_complex.append(f"[{logo_idx}:v]scale={lw}:-1[logo]")
            filter_complex.append(f"{v_label}[logo]overlay={lx}:{ly}[vl]")
            v_label = "[vl]"
        if ass_path:
            ass_arg = f"ass='{_escape_filter_path(ass_path)}'"
            if fonts_dir:
                ass_arg += f":fontsdir='{_escape_filter_path(fonts_dir)}'"
            vf.append(ass_arg)
        vf.append(f"format={cfg.pix_fmt}")

        if filter_complex:
            filter_complex.append(f"{v_label}{','.join(vf)}[vout]")
            args += ["-filter_complex", ";".join(filter_complex), "-map", "[vout]"]
        else:
            args += ["-vf", ",".join(vf), "-map", "0:v"]

        if audio:
            args += ["-map", "1:a", "-c:a", "aac", "-b:a", cfg.audio_bitrate, "-ar", "48000"]
        else:
            args += ["-an"]
        args += [
            "-c:v",
            "libx264",
            "-preset",
            cfg.preset,
            "-crf",
            str(cfg.crf),
            "-movflags",
            "+faststart",
            "-shortest",
        ]
        if duration:
            args += ["-t", f"{duration:.3f}"]
        args.append(str(dst))
        log.info("Rendering final pass (subtitles=%s, audio=%s)", bool(ass_path), bool(audio))
        self.ff.run(args, timeout=7200)
        return dst

    # ------------------------------------------------------------ helpers
    def retime_video(self, src: Path, dst: Path, target_duration: float) -> Path:
        """Speed the video up/down slightly so it matches the narration length.

        Only used when narration replaces the original audio and the lengths
        differ by less than ~35 %; otherwise the caller pads/truncates instead.
        """
        info = self.ff.probe(src)
        if info.duration <= 0:
            raise ValueError("source has no duration")
        factor = target_duration / info.duration  # >1 = slow down
        factor = max(0.65, min(1.35, factor))
        self.ff.run(
            [
                "-i",
                str(src),
                "-vf",
                f"setpts={factor:.5f}*PTS,fps={self.config.fps}",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                self.config.preset,
                "-crf",
                str(self.config.crf),
                "-t",
                f"{target_duration:.3f}",
                str(dst),
            ],
            timeout=7200,
        )
        return dst

    def freeze_extend(self, src: Path, dst: Path, target_duration: float) -> Path:
        """Extend a video to ``target_duration`` by holding the last frame (tpad)."""
        info = self.ff.probe(src)
        extra = max(0.0, target_duration - info.duration)
        if extra < 0.05:
            if src != dst:
                dst.write_bytes(src.read_bytes())
            return dst
        self.ff.run(
            [
                "-i",
                str(src),
                "-vf",
                f"tpad=stop_mode=clone:stop_duration={extra:.3f}",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                self.config.preset,
                "-crf",
                str(self.config.crf),
                str(dst),
            ],
            timeout=7200,
        )
        return dst


def _escape_filter_path(p: Path) -> str:
    """Escape a path for use inside a quoted ffmpeg filter option."""
    s = str(p)
    return s.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def ceil_even(v: float) -> int:
    n = int(math.ceil(v))
    return n + (n % 2)

"""Quality gates - the last thing that runs before a Reel may be packaged.

The v0.2 pipeline trusted FFmpeg's exit code.  That is not a quality signal: a
perfectly encoded file can still be a blurred crop with no narration and
captions running off the screen.

Every gate here answers one concrete question about the *finished* file and the
plan that produced it.  ``error`` severity blocks packaging; ``warning`` is
recorded in the report and in the manifest so problems are visible without
stopping the batch.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from contentforge.log import get_logger
from contentforge.models.schemas import (
    EditPlan,
    GroundedScript,
    QualityReport,
    VideoUnderstanding,
)
from contentforge.processing.editor import RenderSettings, source_point_to_output
from contentforge.processing.framing import preservation_report
from contentforge.utils.ffmpeg import FFmpeg

log = get_logger("quality")


@dataclass
class QualityThresholds:
    aspect: float = 9 / 16
    aspect_tolerance: float = 0.01
    min_text_keep: float = 0.55
    min_narration_coverage: float = 0.55
    narration_slack: float = 2.5  # seconds narration may fall short of the video
    min_duration: float = 6.0
    max_duration: float = 95.0
    max_peak_db: float = -0.2
    min_mean_db: float = -34.0
    min_file_bytes: int = 40_000
    caption_margin: int = 60
    cover_size: tuple[int, int] = (1080, 1920)


class QualityGate:
    """Runs every gate and returns a :class:`QualityReport`."""

    def __init__(
        self,
        thresholds: QualityThresholds | None = None,
        ffmpeg: FFmpeg | None = None,
        render_settings: RenderSettings | None = None,
    ):
        self.t = thresholds or QualityThresholds()
        self.ff = ffmpeg or FFmpeg()
        self.rs = render_settings or RenderSettings()

    def run(
        self,
        final_video: Path,
        *,
        plan: EditPlan | None = None,
        understanding: VideoUnderstanding | None = None,
        script: GroundedScript | None = None,
        narration: Any | None = None,
        overlay: Any | None = None,
        cover: Path | None = None,
        audio_levels: dict[str, float] | None = None,
        work_dir: Path | None = None,
        source_size: tuple[int, int] | None = None,
        caption_font: Any | None = None,
    ) -> QualityReport:
        r = QualityReport()
        final_video = Path(final_video)

        # 1 - the file exists and decodes
        exists = final_video.exists() and final_video.stat().st_size >= self.t.min_file_bytes
        r.add(
            "output_file",
            exists,
            f"{final_video.name}: {final_video.stat().st_size if final_video.exists() else 0} bytes",
            value=final_video.stat().st_size if final_video.exists() else 0,
        )
        info = None
        if exists:
            try:
                info = self.ff.probe(final_video)
            except Exception as exc:  # pragma: no cover - defensive
                r.add("output_readable", False, f"probe failed: {exc}")
        if info is not None:
            r.add("output_readable", info.has_video, "video stream present")

            # 2 - 9:16
            ratio = info.width / max(1, info.height)
            ok = abs(ratio - self.t.aspect) <= self.t.aspect_tolerance
            r.add("aspect_9_16", ok, f"{info.width}x{info.height} (ratio {ratio:.4f})", value=[info.width, info.height])

            # 3 - duration window
            dur = info.duration
            ok = self.t.min_duration <= dur <= self.t.max_duration
            r.add(
                "duration_window",
                ok,
                f"{dur:.1f}s (allowed {self.t.min_duration:.0f}-{self.t.max_duration:.0f}s)",
                value=round(dur, 2),
            )

            # 4 - audio present
            r.add("audio_track", info.has_audio, "audio stream present")

        # 5 - important text preserved by the framing
        if plan is not None and understanding is not None:
            worst = 1.0
            worst_shot = -1
            for shot in plan.shots:
                boxes = understanding.text_boxes_at((shot.src_start + shot.src_end) / 2, window=1.5)
                if not boxes:
                    continue
                view = shot.view_at_output(shot.out_start + shot.out_duration / 2)
                rep = preservation_report(view, boxes, min_keep=self.t.min_text_keep)
                if rep["kept"] < worst:
                    worst, worst_shot = rep["kept"], shot.index
            r.add(
                "ocr_regions_preserved",
                worst >= self.t.min_text_keep,
                f"worst shot #{worst_shot} keeps {worst * 100:.0f}% of the important text "
                f"(min {self.t.min_text_keep * 100:.0f}%)",
                value=round(worst, 3),
            )

        # 6 - the cursor is visible when it matters
        if plan is not None and understanding is not None and source_size:
            clicks = [a for a in understanding.actions if a.kind == "click"]
            visible = 0
            checked = 0
            for a in clicks:
                for shot in plan.shots:
                    if not (shot.src_start <= a.mid < shot.src_end):
                        continue
                    checked += 1
                    frame = understanding.frame_at(a.mid)
                    point = (frame.cursor.x, frame.cursor.y) if frame and frame.cursor.detected else None
                    if point is None and a.region is not None:
                        point = (a.region.x + a.region.w / 2, a.region.y + a.region.h / 2)
                    if point is None:
                        break
                    t_out = shot.out_start + (a.mid - shot.src_start) / max(0.1, shot.speed)
                    mapped = source_point_to_output(shot, point, t_out, self.rs, source_size)
                    if mapped is not None:
                        visible += 1
                    break
            if checked:
                r.add(
                    "cursor_visible_at_clicks",
                    visible >= max(1, int(checked * 0.75)),
                    f"{visible}/{checked} click moments keep the pointer inside the frame",
                    severity="warning",
                    value=[visible, checked],
                )

        # 7/8 - narration exists and matches the timeline
        narration_path = getattr(narration, "path", None)
        has_narration = bool(narration_path and Path(narration_path).exists())
        r.add("narration_exists", has_narration, str(narration_path or "missing"))
        if has_narration and info is not None:
            coverage = float(getattr(narration, "coverage", 0.0))
            n_dur = float(getattr(narration, "duration", 0.0))
            gap = abs(info.duration - n_dur)
            r.add(
                "narration_covers_timeline",
                coverage >= self.t.min_narration_coverage,
                f"speech covers {coverage * 100:.0f}% of the edit (min "
                f"{self.t.min_narration_coverage * 100:.0f}%)",
                value=round(coverage, 3),
            )
            r.add(
                "narration_duration_matches",
                gap <= self.t.narration_slack,
                f"narration {n_dur:.1f}s vs video {info.duration:.1f}s (max drift "
                f"{self.t.narration_slack:.1f}s)",
                value=round(gap, 2),
            )

        # 9/10 - captions exist and fit on screen
        chunks = list(getattr(overlay, "chunks", []) or [])
        r.add("captions_exist", bool(chunks), f"{len(chunks)} caption chunks")
        if chunks:
            overflow = self._caption_overflow(chunks, caption_font)
            r.add(
                "captions_fit_screen",
                not overflow,
                "all chunks fit the safe width" if not overflow else f"too wide: {', '.join(overflow[:3])}",
                value=overflow[:5],
            )

        # 11 - audio levels
        if audio_levels:
            peak = audio_levels.get("peak_db")
            mean = audio_levels.get("mean_db")
            if peak is not None:
                r.add(
                    "audio_peak",
                    peak <= self.t.max_peak_db,
                    f"peak {peak:.1f} dBFS (max {self.t.max_peak_db})",
                    severity="warning",
                    value=peak,
                )
            if mean is not None:
                r.add(
                    "audio_loudness",
                    mean >= self.t.min_mean_db,
                    f"mean {mean:.1f} dBFS (min {self.t.min_mean_db})",
                    severity="warning",
                    value=mean,
                )

        # 12 - cover
        if cover is not None:
            ok = Path(cover).exists()
            detail = "missing"
            if ok:
                try:
                    from PIL import Image

                    with Image.open(cover) as im:
                        size = im.size
                    ok = size == tuple(self.t.cover_size)
                    detail = f"{size[0]}x{size[1]}"
                except Exception as exc:  # pragma: no cover - defensive
                    ok, detail = False, f"unreadable: {exc}"
            r.add("cover_ready", ok, detail)

        # 13 - the working directory is not full of leftovers
        if work_dir is not None and Path(work_dir).is_dir():
            leftovers = [p.name for p in Path(work_dir).glob("shot_*.mp4")]
            leftovers += [p.name for p in Path(work_dir).glob("**/seg_*_raw.wav")]
            r.add(
                "work_dir_clean",
                not leftovers,
                "no intermediate shot files left" if not leftovers else f"{len(leftovers)} leftover file(s)",
                severity="warning",
                value=leftovers[:5],
            )

        # 14 - the script is grounded in the recording
        if script is not None:
            mapped = sum(1 for s in script.segments if s.visual_action)
            r.add(
                "script_grounded",
                script.grounded and mapped == len(script.segments),
                f"{mapped}/{len(script.segments)} narration segments map to a visual moment"
                + (" (degraded fallback)" if script.degraded else ""),
                severity="warning" if script.degraded else "error",
                value=[mapped, len(script.segments)],
            )

        log.info(
            "Quality: %s (%d checks, %d error(s), %d warning(s))",
            "PASS" if r.passed else "FAIL",
            len(r.checks),
            len(r.errors),
            len(r.warnings),
        )
        for c in r.errors + r.warnings:
            log.warning("  %s %s: %s", "FAIL" if c.severity == "error" else "warn", c.name, c.detail)
        return r

    # ------------------------------------------------------------- helpers
    def _caption_overflow(self, chunks: list[Any], font: Any | None = None) -> list[str]:
        """Chunks that cannot be made to fit the mobile-safe width."""
        from contentforge.media.captions import OverlayStyle, _measure, fit_font_size

        style = OverlayStyle(width=self.rs.width, height=self.rs.height, margin_h=self.t.caption_margin)
        max_w = style.width - 2 * style.margin_h
        bad = []
        for c in chunks:
            text = getattr(c, "text", "").upper()
            if _measure(text, fit_font_size(text, style)) > max_w:
                bad.append(text)
        return bad

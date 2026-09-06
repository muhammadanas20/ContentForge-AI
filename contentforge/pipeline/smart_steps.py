"""Steps of the v0.3 **smart** pipeline (understanding -> edit -> Reel).

The classic v0.2 steps in :mod:`contentforge.pipeline.steps` are untouched and
still selectable with ``pipeline.mode: classic``.  The smart order is::

    probe -> extract_audio -> understand -> transcribe -> plan_edit -> script
          -> narration -> compose -> captions -> mix -> render_final -> cover
          -> social -> quality -> package -> analytics -> archive -> cleanup_work

Each step stores JSON artefacts so a job can resume mid-way, and every step
degrades gracefully: no OCR engine, no TTS, no transcript - the Reel is still
produced, with the loss recorded in the manifest and the quality report.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from contentforge.ai.narration import NarrationBuilder, NarrationResult, NarrationSegment
from contentforge.ai.script_writer import GroundedScriptPlanner
from contentforge.ai.tts import TTSError, get_tts_engine
from contentforge.config.schema import Settings
from contentforge.log import get_logger
from contentforge.media.audio import ReelAudioMixer
from contentforge.media.captions import (
    CaptionWord,
    OverlayPlan,
    OverlayStyle,
    ReelOverlayRenderer,
    build_overlay_plan,
)
from contentforge.media.cover import CoverGenerator, CoverStyle
from contentforge.models.schemas import EditPlan, GroundedScript, VideoUnderstanding
from contentforge.pipeline.context import JobContext
from contentforge.pipeline.quality import QualityGate, QualityThresholds
from contentforge.pipeline.steps import (
    AnalyticsInitStep,
    ArchiveStep,
    CleanupWorkStep,
    ExtractAudioStep,
    PackageStep,
    ProbeStep,
    SocialStep,
    Step,
    TranscribeStep,
)
from contentforge.processing.editor import (
    EditConfig,
    RenderSettings,
    SmartEditor,
    plan_edit,
    source_point_to_output,
    visible_output_region,
)
from contentforge.processing.framing import FramingConfig, FramingWeights
from contentforge.processing.video_understanding import understand_video
from contentforge.utils import atomic_write_json, atomic_write_text

log = get_logger("smart")


# --------------------------------------------------------------------- config
def framing_config(s: Settings) -> FramingConfig:
    f = s.framing
    w = f.weights
    return FramingConfig(
        target_width=f.target_width,
        target_height=f.target_height,
        max_zoom=f.max_zoom,
        canvas_bias=f.canvas_bias,
        focus_boost=f.focus_boost,
        focus_falloff=f.focus_falloff,
        samples_per_shot=f.samples_per_shot,
        deadzone=f.smoothing_deadzone,
        smoothing=f.smoothing_ema,
        max_pan_per_second=f.max_pan_per_second,
        weights=FramingWeights(
            text_kept=w.text_kept,
            text_cut=w.text_cut,
            cursor=w.cursor,
            action=w.action,
            prominence=w.prominence,
            content=w.content,
            legibility=w.legibility,
            zoom=w.zoom_penalty,
        ),
    )


def edit_config(s: Settings) -> EditConfig:
    e = s.editing
    return EditConfig(
        max_duration=e.max_duration,
        min_shot=e.min_shot,
        max_shot=e.max_shot,
        remove_dead_time=e.remove_dead_time,
        min_dead_gap=e.min_dead_gap,
        dead_padding=e.dead_padding,
        max_speedup=e.max_speedup,
        dynamic_zoom=e.dynamic_zoom,
        zoom_max=e.zoom_max,
        hook_seconds=e.hook_seconds,
        setup_seconds=e.setup_seconds,
        cta_seconds=e.cta_seconds,
        payoff_fraction=e.payoff_fraction,
        result_hold=e.result_hold,
    )


def render_settings(s: Settings) -> RenderSettings:
    return RenderSettings(
        width=s.framing.target_width,
        height=s.framing.target_height,
        fps=s.video.fps,
        crf=s.video.crf,
        preset=s.video.preset,
        background=s.video.branding.background_color,
        accent=s.video.branding.primary_color,
        card_offset=s.editing.card_offset,
        transitions=s.editing.transitions,
        zoom_headroom=s.editing.zoom_headroom,
    )


def overlay_style(s: Settings) -> OverlayStyle:
    c = s.reel_captions
    return OverlayStyle(
        width=s.framing.target_width,
        height=s.framing.target_height,
        font=c.font,
        font_size=c.font_size,
        outline=c.outline,
        max_words=c.max_words,
        max_chars=c.max_chars,
        primary=c.text_color,
        highlight=c.highlight_color,
        accent=s.video.branding.primary_color,
        safe_bottom=c.safe_bottom,
        safe_top=c.safe_top,
        watermark=s.video.branding.watermark_text if s.video.branding.enabled else "",
        progress_bar=c.progress_bar,
        click_rings=c.click_rings,
        hook_card=c.hook_card,
    )


# ------------------------------------------------------------------- loaders
def load_understanding(ctx: JobContext) -> VideoUnderstanding | None:
    p = ctx.artifact("understanding_json")
    if not p or not Path(p).exists():
        return None
    return VideoUnderstanding.from_dict(json.loads(Path(p).read_text()))


def load_plan(ctx: JobContext) -> EditPlan | None:
    p = ctx.artifact("edit_plan_json")
    if not p or not Path(p).exists():
        return None
    return EditPlan.from_dict(json.loads(Path(p).read_text()))


def load_grounded_script(ctx: JobContext) -> GroundedScript | None:
    p = ctx.artifact("grounded_json")
    if not p or not Path(p).exists():
        return None
    return GroundedScript.from_dict(json.loads(Path(p).read_text()))


def load_narration(ctx: JobContext) -> NarrationResult | None:
    data = ctx.data.get("narration") or {}
    path = ctx.artifact("narration")
    if not data.get("available") or path is None or not Path(path).exists():
        return None
    result = NarrationResult(
        path=Path(path),
        duration=float(data.get("duration", 0.0)),
        engine=str(data.get("engine", "")),
        voice=str(data.get("voice", "")),
        sample_rate=int(data.get("sample_rate", 24000)),
        timeline_duration=float(data.get("timeline_duration", 0.0)),
    )
    result.segments = [
        NarrationSegment(
            start=float(s["start"]),
            end=float(s["end"]),
            text=s["text"],
            role=s.get("role", "demo"),
            tempo=float(s.get("tempo", 1.0)),
        )
        for s in data.get("segments", [])
    ]
    return result


def load_overlay(ctx: JobContext) -> OverlayPlan | None:
    p = ctx.artifact("overlay_json")
    if not p or not Path(p).exists():
        return None
    raw = json.loads(Path(p).read_text())
    from contentforge.media.captions import CaptionChunk

    plan = OverlayPlan(
        duration=float(raw.get("duration", 0.0)),
        hook_text=raw.get("hook_text", ""),
        cta_text=raw.get("cta_text", ""),
    )
    for c in raw.get("chunks", []):
        plan.chunks.append(
            CaptionChunk(
                start=float(c["start"]),
                end=float(c["end"]),
                words=[CaptionWord(w["text"], float(w["start"]), float(w["end"])) for w in c["words"]],
                band=c.get("band", "bottom"),
                role=c.get("role", "demo"),
            )
        )
    plan.clicks = [(float(t), float(x), float(y)) for t, x, y in raw.get("clicks", [])]
    return plan


# --------------------------------------------------------------------- steps
class UnderstandStep(Step):
    """Watch the recording: sample frames, OCR, cursor, clicks, scroll, changes."""

    name = "understand"

    def run(self, ctx: JobContext) -> dict[str, Any]:
        cfg = self.settings.understanding
        website = ctx.data.get("website_hint") or cfg.website_url
        context = ctx.data.get("website_context") or cfg.website_context
        u = understand_video(
            ctx.source,
            sample_fps=cfg.sample_fps,
            work_width=cfg.work_width,
            ocr_every_seconds=cfg.ocr_every_seconds,
            ocr_engine=cfg.ocr_engine,
            ocr_lang=cfg.ocr_languages,
            max_frames=cfg.max_frames,
            track_cursor=cfg.track_cursor,
            website=website,
            website_context=context,
        )
        atomic_write_json(ctx.path("understanding.json"), u.to_dict())
        ctx.set_artifact("understanding_json", ctx.path("understanding.json"))
        summary = u.summary()
        ctx.data["understanding"] = summary
        log.info(
            "Understanding: %d frames, %d actions (%s), OCR backend %s",
            len(u.frames),
            len(u.actions),
            ", ".join(f"{k}x{v}" for k, v in summary.get("action_counts", {}).items()) or "none",
            u.ocr_backend,
        )
        return summary


class PlanEditStep(Step):
    """Decide what to keep, where to cut and how to frame every shot."""

    name = "plan_edit"

    def run(self, ctx: JobContext) -> dict[str, Any]:
        u = load_understanding(ctx)
        if u is None:
            raise RuntimeError("no video understanding available - cannot plan the edit")
        silences = ctx.data.get("silences")
        if silences is None:
            silences = self._silences(ctx)
        plan = plan_edit(
            u,
            config=edit_config(self.settings),
            framing=framing_config(self.settings),
            silences=[(float(a), float(b)) for a, b in silences] if silences else None,
        )
        if not plan.shots:
            raise RuntimeError("edit plan is empty - the recording has no usable material")
        atomic_write_json(ctx.path("edit_plan.json"), plan.to_dict())
        ctx.set_artifact("edit_plan_json", ctx.path("edit_plan.json"))
        ctx.data["edit"] = plan.summary()
        ctx.data["final_duration"] = plan.duration
        return plan.summary()

    def _silences(self, ctx: JobContext) -> list[list[float]] | None:
        """Quiet stretches of the original audio - dead time is only cut where
        nothing is said *and* nothing happens on screen."""
        wav = ctx.artifact("source_audio")
        if not wav or not Path(wav).exists() or not (ctx.data.get("media") or {}).get("has_audio"):
            return None
        try:
            spans = self.ff.detect_silence(
                Path(wav),
                threshold_db=self.settings.audio.silence.threshold_db,
                min_duration=self.settings.audio.silence.min_duration,
            )
        except Exception as exc:  # noqa: BLE001 - silence detection is advisory
            log.info("Silence detection unavailable (%s)", exc)
            return None
        ctx.data["silences"] = [[round(a, 3), round(b, 3)] for a, b in spans]
        return ctx.data["silences"]


class GroundedScriptStep(Step):
    """Write narration that is bound to what the recording actually shows."""

    name = "script"

    def run(self, ctx: JobContext) -> dict[str, Any]:
        s = self.settings
        u = load_understanding(ctx)
        plan = load_plan(ctx)
        if plan is None:
            raise RuntimeError("edit plan missing - run plan_edit first")
        transcript_text = ""
        tp = ctx.artifact("transcript_json")
        if tp and Path(tp).exists():
            try:
                transcript_text = json.loads(Path(tp).read_text()).get("text", "")
            except (OSError, ValueError):
                transcript_text = ""
        website = ctx.data.get("website_hint") or s.understanding.website_url
        planner = GroundedScriptPlanner(s.script, brand=s.project.brand)
        script = planner.plan(
            plan,
            u,
            website=website,
            website_context=ctx.data.get("website_context") or s.understanding.website_context,
            transcript_text=transcript_text,
        )
        atomic_write_json(ctx.path("grounded_script.json"), script.to_dict())
        atomic_write_text(ctx.path("script.md"), script.to_markdown())
        # A v0.2-shaped script.json keeps the social/packaging steps working.
        atomic_write_json(ctx.path("script.json"), script.to_legacy_dict())
        ctx.set_artifact("grounded_json", ctx.path("grounded_script.json"))
        ctx.set_artifact("script_json", ctx.path("script.json"))
        ctx.set_artifact("script_md", ctx.path("script.md"))
        ctx.data["title"] = script.title
        ctx.data["keywords"] = script.keywords
        ctx.data["script"] = {
            "source": script.source,
            "segments": len(script.segments),
            "words": script.word_count,
            "degraded": script.degraded,
        }
        self.db.update_job(ctx.job_id, title=script.title)
        return ctx.data["script"]


class GroundedNarrationStep(Step):
    """Synthesise each script segment into its slot on the edit timeline."""

    name = "narration"

    def run(self, ctx: JobContext) -> dict[str, Any]:
        script = load_grounded_script(ctx)
        if script is None:
            raise RuntimeError("grounded script missing - run script first")
        out = ctx.path("narration.wav")
        try:
            engine = get_tts_engine(self.settings.tts)
            builder = NarrationBuilder(
                engine,
                self.ff,
                sample_rate=self.settings.tts.output_sample_rate,
            )
            result = builder.build(script, out, work_dir=ctx.path("narration_parts"))
        except (TTSError, RuntimeError) as exc:
            log.error("Narration failed (%s); the Reel will use the original audio", exc)
            self.db.add_event("warning", f"Narration unavailable: {exc}", ctx.job_id)
            ctx.data["narration"] = {"available": False, "error": str(exc)}
            return {"available": False, "error": str(exc)}
        ctx.set_artifact("narration", result.path)
        ctx.data["narration"] = {"available": True, **result.to_dict()}
        return {
            "engine": result.engine,
            "voice": result.voice,
            "duration": round(result.duration, 2),
            "coverage": round(result.coverage, 2),
            "segments": len(result.segments),
        }


class ComposeStep(Step):
    """Render the silent 9:16 Reel from the edit plan."""

    name = "compose"

    def run(self, ctx: JobContext) -> dict[str, Any]:
        plan = load_plan(ctx)
        if plan is None:
            raise RuntimeError("edit plan missing - run plan_edit first")
        editor = SmartEditor(render_settings(self.settings), self.ff)
        out = ctx.path("composed.mp4")
        result = editor.render(ctx.source, plan, out, work_dir=ctx.path("shots"))
        ctx.set_artifact("composed_video", result.path)
        ctx.data["composed"] = {"duration": result.duration, "shots": result.shots}
        ctx.data["final_duration"] = result.duration
        return ctx.data["composed"]


class CaptionsStep(Step):
    """Plan captions, click rings and branding overlays (no rendering yet)."""

    name = "captions"

    def run(self, ctx: JobContext) -> dict[str, Any]:
        s = self.settings
        plan = load_plan(ctx)
        script = load_grounded_script(ctx)
        narration = load_narration(ctx)
        if plan is None or script is None:
            raise RuntimeError("edit plan or script missing")
        duration = float(ctx.data.get("final_duration") or plan.duration)
        segments = narration.segments if narration and narration.segments else script.segments
        rs = render_settings(s)
        source_size = (
            int((ctx.data.get("media") or {}).get("width") or 0),
            int((ctx.data.get("media") or {}).get("height") or 0),
        )

        # where the important content sits on the output canvas, per shot
        focus: list[tuple[float, float, Any]] = []
        clicks: list[tuple[float, float, float]] = []
        for shot in plan.shots:
            region = visible_output_region(shot, shot.out_start + shot.out_duration / 2, rs, source_size)
            focus.append((shot.out_start, shot.out_end, region))
        if s.editing.click_effects and source_size[0]:
            for a in plan.emphasis:
                if a.kind != "click" or a.region is None:
                    continue
                point = (a.region.x + a.region.w / 2, a.region.y + a.region.h / 2)
                for shot in plan.shots:
                    if shot.out_start <= a.mid < shot.out_end:
                        mapped = source_point_to_output(shot, point, a.mid, rs, source_size)
                        if mapped:
                            clicks.append((a.mid, mapped[0] * rs.width, mapped[1] * rs.height))
                        break

        word_timings = self._word_timings(ctx, segments) if s.reel_captions.word_level else None
        overlay = build_overlay_plan(
            segments,
            duration=duration,
            style=overlay_style(s),
            focus_regions=focus,
            clicks=clicks,
            hook_text=_hook_of(script),
            cta_text=script.cta_text() or s.script.cta_default,
            word_timings=word_timings,
        )
        atomic_write_json(ctx.path("overlay.json"), overlay.to_dict())
        atomic_write_text(ctx.path("captions.srt"), overlay.to_srt())
        atomic_write_text(
            ctx.path("captions.txt"), "\n".join(c.text for c in overlay.chunks)
        )
        ctx.set_artifact("overlay_json", ctx.path("overlay.json"))
        ctx.set_artifact("srt", ctx.path("captions.srt"))
        ctx.set_artifact("captions_txt", ctx.path("captions.txt"))
        ctx.data["caption_count"] = len(overlay.chunks)
        return {"chunks": len(overlay.chunks), "clicks": len(overlay.clicks)}

    def _word_timings(self, ctx: JobContext, segments: list[Any]) -> dict[int, list[CaptionWord]] | None:
        """Whisper word timings on the narration, mapped back onto each segment."""
        narration = ctx.artifact("narration")
        if not narration or not Path(narration).exists():
            return None
        try:
            from contentforge.ai.transcriber import Transcriber

            cfg = self.settings.transcription.model_copy(update={"word_timestamps": True})
            asr = Transcriber(cfg).transcribe(Path(narration))
        except Exception as exc:  # noqa: BLE001 - alignment is best effort
            log.info("Word-level caption timing unavailable (%s); using estimated timing", exc)
            return None
        words = [w for seg in asr.segments for w in getattr(seg, "words", []) or []]
        if not words:
            return None
        out: dict[int, list[CaptionWord]] = {}
        for i, seg in enumerate(segments):
            inside = [w for w in words if seg.start - 0.15 <= w.start <= seg.end + 0.35]
            if len(inside) >= max(2, len(seg.text.split()) // 2):
                out[i] = [CaptionWord(w.word.strip(), w.start, w.end) for w in inside if w.word.strip()]
        return out or None


class MixStep(Step):
    """Narration + ducked music + click ticks, normalised."""

    name = "mix"

    def run(self, ctx: JobContext) -> dict[str, Any]:
        s = self.settings
        duration = float(ctx.data.get("final_duration") or 0.0)
        overlay = load_overlay(ctx)
        music = Path(s.audio.mix.background_music) if s.audio.mix.background_music else None
        if music and not music.is_absolute():
            music = s.paths.assets / music
        narration = ctx.artifact("narration")
        original = ctx.artifact("source_audio") if s.audio.mix.original_volume > 0 else None
        mixer = ReelAudioMixer(s.audio, self.ff)
        out = ctx.path("mixed.wav")
        result = mixer.mix(
            out,
            duration=duration,
            narration=Path(narration) if narration else None,
            music=music if music and music.exists() else None,
            original=Path(original) if original else None,
            click_times=[t for t, _x, _y in (overlay.clicks if overlay else [])]
            if s.editing.click_sfx
            else (),
            sample_rate=48000,
        )
        ctx.set_artifact("mixed_audio", result.path)
        ctx.data["mix"] = result.to_dict()
        try:
            ctx.data["audio_levels"] = mixer.measure(result.path)
        except Exception:  # noqa: BLE001 - measurement is advisory
            ctx.data["audio_levels"] = {}
        return {"tracks": result.tracks, "ducked": result.ducked, "clicks": result.clicks}


class BurnStep(Step):
    """Burn the overlays in and mux the mixed audio: this is the deliverable."""

    name = "render_final"

    def run(self, ctx: JobContext) -> dict[str, Any]:
        s = self.settings
        video = ctx.artifact("composed_video")
        overlay = load_overlay(ctx)
        if video is None or overlay is None:
            raise RuntimeError("composed video or overlay plan missing")
        audio = ctx.artifact("mixed_audio")
        renderer = ReelOverlayRenderer(overlay_style(s), self.ff)
        out = ctx.path("final.mp4")
        renderer.render(
            Path(video),
            overlay,
            out,
            ass_path=ctx.path("overlay.ass"),
            audio=Path(audio) if audio else None,
            crf=s.video.crf,
            preset=s.video.preset,
        )
        ctx.set_artifact("ass", ctx.path("overlay.ass"))
        info = self.ff.probe(out)
        if not info.has_video or info.duration <= 0:
            raise RuntimeError("final render produced an unreadable file")
        ctx.set_artifact("final_video", out)
        ctx.data["final"] = {
            "duration": info.duration,
            "width": info.width,
            "height": info.height,
            "size_bytes": info.size_bytes,
        }
        self.db.update_job(ctx.job_id, duration_seconds=info.duration)
        if s.pipeline.delete_intermediates_early:
            comp = ctx.artifact("composed_video")
            if comp and Path(comp).exists():
                Path(comp).unlink(missing_ok=True)
        return ctx.data["final"]


class CoverStep(Step):
    """Score candidate frames and design a branded Instagram cover."""

    name = "cover"

    def run(self, ctx: JobContext) -> dict[str, Any]:
        s = self.settings
        u = load_understanding(ctx)
        plan = load_plan(ctx)
        script = load_grounded_script(ctx)
        style = CoverStyle(
            width=s.cover.width,
            height=s.cover.height,
            title_size=s.cover.title_size,
            max_title_words=s.cover.max_title_words,
            accent=s.video.branding.primary_color,
            background=s.video.branding.background_color,
            brand=s.project.brand,
        )
        out = ctx.path("cover.jpg")
        result = CoverGenerator(style, self.ff).generate(
            ctx.source,
            out,
            understanding=u,
            plan=plan,
            script=script,
            subtitle=ctx.data.get("website_hint", ""),
            work_dir=ctx.path("cover_work"),
            concepts=tuple(s.cover.concepts) or None,
        )
        ctx.set_artifact("cover", result.path)
        ctx.set_artifact("thumbnail", result.path)  # packaged as the cover image
        ctx.data["cover"] = result.to_dict()
        return {"concept": result.concept, "frame_time": round(result.frame_time, 2)}


class QualityStep(Step):
    """Twelve gates between a rendered file and a published Reel."""

    name = "quality"

    def run(self, ctx: JobContext) -> dict[str, Any]:
        s = self.settings
        q = s.quality
        final = ctx.artifact("final_video")
        if final is None:
            raise RuntimeError("no final video to check")
        media = ctx.data.get("media") or {}
        gate = QualityGate(
            QualityThresholds(
                min_text_keep=q.min_text_keep,
                min_narration_coverage=q.min_narration_coverage,
                narration_slack=q.narration_slack_seconds,
                min_duration=q.min_duration,
                max_duration=q.max_duration,
                max_peak_db=q.max_peak_db,
                min_mean_db=q.min_mean_db,
            ),
            self.ff,
            render_settings(s),
        )
        report = gate.run(
            Path(final),
            plan=load_plan(ctx),
            understanding=load_understanding(ctx),
            script=load_grounded_script(ctx),
            narration=load_narration(ctx),
            overlay=load_overlay(ctx),
            cover=ctx.artifact("cover"),
            audio_levels=ctx.data.get("audio_levels"),
            work_dir=ctx.work_dir,
            source_size=(int(media.get("width") or 0), int(media.get("height") or 0)),
        )
        atomic_write_json(ctx.path("quality.json"), report.to_dict())
        atomic_write_text(ctx.path("quality.md"), report.to_markdown())
        ctx.set_artifact("quality_json", ctx.path("quality.json"))
        ctx.set_artifact("quality_md", ctx.path("quality.md"))
        ctx.data["quality"] = report.to_dict()
        for c in report.errors:
            self.db.add_event("error", f"Quality gate failed: {c.name} - {c.detail}", ctx.job_id)
        for c in report.warnings:
            self.db.add_event("warning", f"Quality warning: {c.name} - {c.detail}", ctx.job_id)
        if not report.passed and q.block_on_error:
            raise RuntimeError(
                "quality gates failed: " + "; ".join(f"{c.name} ({c.detail})" for c in report.errors)
            )
        return {"passed": report.passed, "errors": len(report.errors), "warnings": len(report.warnings)}


def _hook_of(script: GroundedScript) -> str:
    for seg in script.segments:
        if seg.role == "hook":
            return seg.text
    return script.title


SMART_STEPS: list[type[Step]] = [
    ProbeStep,
    ExtractAudioStep,
    UnderstandStep,
    TranscribeStep,
    PlanEditStep,
    GroundedScriptStep,
    GroundedNarrationStep,
    ComposeStep,
    CaptionsStep,
    MixStep,
    BurnStep,
    CoverStep,
    SocialStep,
    QualityStep,
    PackageStep,
    AnalyticsInitStep,
    ArchiveStep,
    CleanupWorkStep,
]


def steps_for_mode(settings: Settings) -> list[type[Step]]:
    """Step list for ``pipeline.mode`` (``smart`` by default)."""
    from contentforge.pipeline.steps import DEFAULT_STEPS

    return SMART_STEPS if settings.pipeline.mode == "smart" else DEFAULT_STEPS

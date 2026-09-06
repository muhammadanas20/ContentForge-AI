"""Concrete pipeline steps.

Each step is a small class with:

* ``name``      - stable identifier used for resume/skip bookkeeping;
* ``enabled``   - reads the ``pipeline.steps`` switch;
* ``run(ctx)``  - performs the work, storing artefacts/data on the context and
                  returning a dict of outputs for the database.

Steps only talk to each other through :class:`JobContext`, which keeps them
independently testable and lets the runner resume from any point.
"""

from __future__ import annotations

import abc
import shutil
import time
from pathlib import Path
from typing import Any

from contentforge.ai import Script, ScriptWriter, Transcriber, Transcript
from contentforge.ai.social import SocialWriter
from contentforge.ai.tts import TTSError, TTSResult, get_tts_engine
from contentforge.archive import Archiver
from contentforge.config.schema import Settings
from contentforge.db import Database
from contentforge.log import get_logger
from contentforge.output import UploadPackager
from contentforge.pipeline.context import JobContext
from contentforge.processing import (
    AudioMixer,
    CursorTrack,
    CursorTracker,
    Timeline,
    VideoEditor,
    analyse_video,
    build_keep_ranges,
    plan_crop,
    plan_cursor_crop,
    plan_zoom_pulses,
)
from contentforge.subtitles import AssRenderer, OverlaySpec, build_captions, write_all
from contentforge.thumbnails import ThumbnailGenerator
from contentforge.utils import FFmpeg, atomic_write_json, atomic_write_text, disk_free_gb, read_json

log = get_logger("steps")


class Step(abc.ABC):
    name: str = "step"

    def __init__(self, settings: Settings, db: Database, ffmpeg: FFmpeg):
        self.settings = settings
        self.db = db
        self.ff = ffmpeg

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.settings.pipeline.steps, self.name, True))

    @abc.abstractmethod
    def run(self, ctx: JobContext) -> dict[str, Any]: ...


# --------------------------------------------------------------------------
class ProbeStep(Step):
    """Validate the input and record media info."""

    name = "probe"

    def run(self, ctx: JobContext) -> dict[str, Any]:
        floor = float(self.settings.pipeline.min_free_disk_gb_to_start)
        if floor > 0:
            free = disk_free_gb(ctx.work_dir.parent)
            if free < floor:
                raise RuntimeError(
                    f"Only {free:.2f} GB free on the data disk (< {floor:.1f} GB); "
                    "run 'contentforge cleanup' or free space before processing"
                )
        info = self.ff.probe(ctx.source)
        if not info.has_video:
            raise ValueError(f"{ctx.source.name} has no video stream")
        if info.duration <= 0.5:
            raise ValueError(f"{ctx.source.name} is too short ({info.duration:.2f}s)")
        max_d = self.settings.video.max_duration_seconds * 3
        if info.duration > max_d:
            raise ValueError(
                f"{ctx.source.name} is {info.duration:.0f}s - longer than {max_d}s; record shorter clips"
            )
        ctx.data["media"] = {
            "duration": info.duration,
            "width": info.width,
            "height": info.height,
            "fps": info.fps,
            "has_audio": info.has_audio,
            "size_bytes": info.size_bytes,
        }
        return ctx.data["media"]


class ExtractAudioStep(Step):
    name = "extract_audio"

    def run(self, ctx: JobContext) -> dict[str, Any]:
        wav = ctx.path("source_audio.wav")
        a = self.settings.audio
        if ctx.data["media"]["has_audio"]:
            self.ff.extract_audio(ctx.source, wav, sample_rate=a.sample_rate, channels=a.channels)
        else:
            self.ff.make_silence(wav, ctx.data["media"]["duration"], a.sample_rate)
        ctx.set_artifact("source_audio", wav)
        return {"source_audio": str(wav)}


class TranscribeStep(Step):
    name = "transcribe"

    def run(self, ctx: JobContext) -> dict[str, Any]:
        wav = ctx.artifact("source_audio")
        assert wav is not None
        if ctx.data["media"]["has_audio"]:
            transcript = Transcriber(self.settings.transcription).transcribe(wav)
        else:
            transcript = Transcript(
                language="en", duration=ctx.data["media"]["duration"], segments=[]
            )
        files = transcript.save(ctx.path("transcript"))
        for k, p in files.items():
            ctx.set_artifact(f"transcript_{k}", p)
        ctx.data["transcript_words"] = transcript.word_count
        ctx.data["language"] = transcript.language
        return {
            "words": transcript.word_count,
            "language": transcript.language,
            **{k: str(v) for k, v in files.items()},
        }


class ScriptStep(Step):
    name = "script"

    def run(self, ctx: JobContext) -> dict[str, Any]:
        transcript = _load_transcript(ctx)
        text = transcript.text.strip()
        hint = ctx.data.get("website_hint") or _website_from_filename(ctx.source)
        writer = ScriptWriter(self.settings.script, brand=self.settings.project.brand)
        if len(text.split()) < 8:
            log.warning(
                "Transcript is very short (%d words); script will be generic", len(text.split())
            )
            text = (
                text or f"A quick look at a useful tool for students on {hint or 'this website'}."
            )
        script = writer.write(text, website_hint=hint)
        atomic_write_json(ctx.path("script.json"), script.to_dict())
        atomic_write_text(ctx.path("script.md"), script.to_markdown())
        ctx.set_artifact("script_json", ctx.path("script.json"))
        ctx.set_artifact("script_md", ctx.path("script.md"))
        ctx.data["title"] = script.title
        ctx.data["keywords"] = script.keywords
        self.db.update_job(ctx.job_id, title=script.title)
        return {"title": script.title, "words": script.word_count, "source": script.source}


class NarrationStep(Step):
    name = "narration"

    def run(self, ctx: JobContext) -> dict[str, Any]:
        script = _load_script(ctx)
        out = ctx.path("narration.wav")
        try:
            engine = get_tts_engine(self.settings.tts)
            result = engine.synthesize(script.narration, out)
        except TTSError as exc:
            # Narration is optional: fall back to the original audio but keep going.
            log.error("Narration failed (%s); continuing with original audio", exc)
            self.db.add_event("warning", f"Narration unavailable: {exc}", ctx.job_id)
            ctx.data["narration"] = {"available": False, "error": str(exc)}
            return {"available": False, "error": str(exc)}
        ctx.set_artifact("narration", result.path)
        ctx.data["narration"] = {
            "available": True,
            "engine": result.engine,
            "voice": result.voice,
            "duration": result.duration,
            "sentences": [[s, e, t] for s, e, t in result.sentence_timings],
            "alignment": None,
        }
        out_info: dict[str, Any] = {
            "engine": result.engine,
            "voice": result.voice,
            "duration": round(result.duration, 2),
            "word_alignment": "disabled",
        }
        wl = self.settings.subtitles.word_level
        if wl.enabled and self.settings.pipeline.steps.subtitles:
            alignment = self._align_words(ctx, script.narration, result)
            if alignment is not None:
                ctx.data["narration"]["alignment"] = alignment.to_dict()
                out_info["word_alignment"] = f"{alignment.match_ratio:.0%}"
            else:
                out_info["word_alignment"] = "fallback-sentence"
        return out_info

    def _align_words(self, ctx: JobContext, narration_text: str, result: TTSResult):
        """Whisper word timestamps on the TTS audio -> per-word script timing (None on failure)."""
        from contentforge.ai.alignment import align_script

        wl = self.settings.subtitles.word_level
        try:
            asr_cfg = self.settings.transcription.model_copy(update={"word_timestamps": True})
            asr = Transcriber(asr_cfg).transcribe(result.path)
            alignment = align_script(
                narration_text,
                asr,
                duration=result.duration,
                min_match_ratio=wl.min_match_ratio,
                min_word_seconds=wl.min_word_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - alignment is best effort by design
            log.warning("Word-level alignment failed (%s); using sentence timings", exc)
            self.db.add_event("warning", f"Word alignment unavailable: {exc}", ctx.job_id)
            return None
        if alignment is None:
            self.db.add_event(
                "warning", "Word alignment rejected (low match); sentence timings", ctx.job_id
            )
        return alignment


class AnalyseStep(Step):
    """Silence + motion analysis -> keep-list, timeline, crop plan."""

    name = "analyse"

    def run(self, ctx: JobContext) -> dict[str, Any]:
        s = self.settings
        media = ctx.data["media"]
        duration = float(media["duration"])
        wav = ctx.artifact("source_audio")
        steps = s.pipeline.steps

        silences: list[tuple[float, float]] = []
        if steps.silence_removal and media["has_audio"] and wav:
            silences = self.ff.detect_silence(
                wav,
                threshold_db=s.audio.silence.threshold_db,
                min_duration=s.audio.silence.min_duration,
            )
        analysis = None
        smart = steps.vertical_crop and s.video.crop.mode == "smart"
        if steps.jump_cuts or smart:
            tracker = None
            fc = s.video.crop.follow_cursor
            if smart and fc.enabled:
                tracker = CursorTracker(
                    int(media["width"]),
                    int(media["height"]),
                    work_width=fc.work_width,
                    min_size_px=fc.min_size_px,
                    max_size_px=fc.max_size_px,
                )
            analysis = analyse_video(
                ctx.source,
                sample_fps=s.video.crop.sample_fps,
                cursor_tracker=tracker,
                cursor_sample_fps=fc.sample_fps,
            )
        low_motion = None
        if steps.jump_cuts and analysis is not None and s.video.jump_cuts.enabled:
            low_motion = analysis.low_motion_intervals(
                s.video.jump_cuts.motion_threshold, s.video.jump_cuts.min_gap_seconds
            )

        keep = (
            build_keep_ranges(
                duration,
                silences,
                padding=s.audio.silence.keep_padding,
                min_silence=s.audio.silence.min_duration,
                low_motion=low_motion,
                min_gap=s.video.jump_cuts.min_gap_seconds,
            )
            if (steps.silence_removal or steps.jump_cuts)
            else [(0.0, duration)]
        )

        # Hard cap on output length: keep the first N seconds of kept material
        cap = float(s.video.max_duration_seconds)
        trimmed: list[tuple[float, float]] = []
        acc = 0.0
        for a, b in keep:
            if acc >= cap:
                break
            seg = min(b - a, cap - acc)
            trimmed.append((a, a + seg))
            acc += seg
        keep = trimmed or keep

        timeline = Timeline(keep)
        center = analysis.dominant_center(s.video.crop.smoothing) if analysis is not None else 0.5
        cursor = analysis.cursor if analysis is not None else None
        ctx.data["edit"] = {
            "silences": silences,
            "low_motion": low_motion or [],
            "keep": keep,
            "output_duration": timeline.output_duration,
            "removed_seconds": timeline.removed_seconds,
            "crop_center_x": center,
            "cursor_track": cursor.to_dict() if cursor is not None else None,
        }
        log.info(
            "Edit plan: %d segments, %.1fs -> %.1fs (removed %.1fs), crop centre %.2f",
            len(keep),
            duration,
            timeline.output_duration,
            timeline.removed_seconds,
            center,
        )
        return {
            "segments": len(keep),
            "output_duration": round(timeline.output_duration, 2),
            "removed_seconds": round(timeline.removed_seconds, 2),
            "cursor_detections": cursor.detections if cursor is not None else 0,
            "cursor_coverage": round(cursor.coverage, 2) if cursor is not None else 0.0,
        }


class RenderCutStep(Step):
    """Cuts + 9:16 crop + zoom + transitions -> silent vertical video (output time)."""

    name = "render_cut"

    def run(self, ctx: JobContext) -> dict[str, Any]:
        s = self.settings
        from contentforge.utils.ffmpeg import MediaInfo

        media = ctx.data["media"]
        info = MediaInfo(
            path=ctx.source,
            width=media["width"],
            height=media["height"],
            duration=media["duration"],
        )
        edit = ctx.data["edit"]
        keep = [tuple(k) for k in edit["keep"]]
        mode = s.video.crop.mode if s.pipeline.steps.vertical_crop else "center"
        crop = plan_crop(info, s.video.width, s.video.height, mode, center_x=edit["crop_center_x"])
        fc = s.video.crop.follow_cursor
        if mode == "smart" and fc.enabled and edit.get("cursor_track"):
            track = CursorTrack.from_dict(edit["cursor_track"])
            crop = plan_cursor_crop(crop, track, keep, fc)

        pulses = []
        if s.pipeline.steps.auto_zoom and s.video.zoom.enabled:
            anchors = None
            narr = ctx.data.get("narration") or {}
            if narr.get("available") and narr.get("sentences"):
                anchors = [float(st) for st, _, _ in narr["sentences"]][
                    1:
                ]  # zoom at each new sentence
            pulses = plan_zoom_pulses(
                edit["output_duration"],
                interval=s.video.zoom.interval_seconds,
                duration=s.video.zoom.duration_seconds,
                max_zoom=s.video.zoom.max_zoom,
                anchors=anchors,
            )
        out = ctx.path("cut.mp4")
        VideoEditor(s.video, self.ff).render_cut(
            ctx.source,
            out,
            keep=keep,
            crop=crop,
            zoom_pulses=pulses,
            output_duration=edit["output_duration"],
        )
        ctx.set_artifact("cut_video", out)
        ctx.data["crop"] = crop.to_dict()
        ctx.data["zoom_pulses"] = len(pulses)
        return {"crop": ctx.data["crop"], "zoom_pulses": len(pulses)}


class SyncStep(Step):
    """Reconcile video length with narration length and build the final audio mix."""

    name = "sync_audio"

    def run(self, ctx: JobContext) -> dict[str, Any]:
        s = self.settings
        edit = ctx.data["edit"]
        keep = [tuple(k) for k in edit["keep"]]
        cut = ctx.artifact("cut_video")
        assert cut is not None
        video_dur = self.ff.duration(cut)
        narr = ctx.data.get("narration") or {}
        editor = VideoEditor(s.video, self.ff)
        mixer = AudioMixer(s.audio, self.ff)

        final_video = cut
        final_duration = video_dur
        narration_path = ctx.artifact("narration") if narr.get("available") else None

        if narration_path:
            n_dur = float(narr["duration"]) + 0.6  # small tail so the CTA card is readable
            ratio = n_dur / max(0.1, video_dur)
            if 0.75 <= ratio <= 1.30:
                final_video = editor.retime_video(cut, ctx.path("synced.mp4"), n_dur)
                final_duration = self.ff.duration(final_video)
                ctx.data["sync"] = {"method": "retime", "factor": round(ratio, 3)}
            elif ratio > 1.30:
                final_video = editor.freeze_extend(cut, ctx.path("synced.mp4"), n_dur)
                final_duration = self.ff.duration(final_video)
                ctx.data["sync"] = {"method": "freeze-extend", "extra": round(n_dur - video_dur, 2)}
            else:  # narration much shorter: keep video, narration ends early (music/original fill)
                ctx.data["sync"] = {"method": "none", "note": "narration shorter than video"}
        else:
            ctx.data["sync"] = {"method": "original-audio"}

        # Original audio (cut to match the edit) - only mixed if configured or narration is missing
        original_cut = None
        if ctx.data["media"]["has_audio"] and (
            not narration_path or s.audio.mix.original_volume > 0
        ):
            original_cut = mixer.cut_audio(ctx.source, ctx.path("original_cut.wav"), keep)
            if ctx.data["sync"].get("method") == "retime" and original_cut:
                # keep original audio in sync with the retimed video using atempo
                factor = 1.0 / ctx.data["sync"]["factor"]
                self.ff.run(
                    [
                        "-i",
                        str(original_cut),
                        "-af",
                        _atempo_chain(factor),
                        "-c:a",
                        "pcm_s16le",
                        str(ctx.path("original_cut_retimed.wav")),
                    ]
                )
                original_cut = ctx.path("original_cut_retimed.wav")

        music = Path(s.audio.mix.background_music) if s.audio.mix.background_music else None
        if music and not music.is_absolute():
            music = s.root / music
        mix_cfg = s.audio
        if not narration_path and original_cut and mix_cfg.mix.original_volume == 0:
            # no narration available -> the original audio must be audible
            mix_cfg = mix_cfg.model_copy(deep=True)
            mix_cfg.mix.original_volume = 1.0
        mixed = AudioMixer(mix_cfg, self.ff).mix(
            ctx.path("mix.wav"),
            duration=final_duration,
            narration=narration_path,
            original=original_cut,
            music=music if music and music.exists() else None,
        )
        ctx.set_artifact("synced_video", final_video)
        ctx.set_artifact("mixed_audio", mixed)
        ctx.data["final_duration"] = final_duration
        return {"duration": round(final_duration, 2), **ctx.data["sync"]}


class SubtitlesStep(Step):
    name = "subtitles"

    def run(self, ctx: JobContext) -> dict[str, Any]:
        s = self.settings
        duration = float(ctx.data["final_duration"])
        narr = ctx.data.get("narration") or {}
        script = _load_script(ctx) if ctx.has_artifact("script_json") else None

        if narr.get("available") and script:
            transcript = _narration_transcript(narr, script, duration)
        else:
            # Captions follow the original speech remapped through the edit timeline
            transcript = _remap_transcript(_load_transcript(ctx), ctx)
            if ctx.data.get("sync", {}).get("method") == "retime":
                f = ctx.data["sync"]["factor"]
                for seg in transcript.segments:
                    seg.start *= f
                    seg.end *= f
                    for w in seg.words:
                        w.start *= f
                        w.end *= f
                transcript.duration = duration

        keywords = list(s.subtitles.highlight_keywords) + list(ctx.data.get("keywords", []))
        captions = build_captions(
            transcript,
            max_words=s.subtitles.max_words_per_caption,
            max_chars=s.subtitles.max_chars_per_line,
            highlight_keywords=keywords,
            uppercase=s.subtitles.uppercase,
        )
        files = write_all(
            captions, ctx.path("subtitles.x"), [f for f in s.subtitles.formats if f != "ass"]
        )
        for fmt, p in files.items():
            ctx.set_artifact({"json": "captions_json", "txt": "captions_txt"}.get(fmt, fmt), p)

        overlays = OverlaySpec(
            duration=duration,
            progress_bar=s.video.progress_bar if s.pipeline.steps.progress_bar else None,
            branding=s.video.branding if s.pipeline.steps.branding else None,
            intro_text=script.hook if script else "",
            outro_text=script.cta if script else s.script.cta_default,
            intro_seconds=s.video.branding.intro_seconds,
            outro_seconds=s.video.branding.outro_seconds,
        )
        sub_cfg = (
            s.subtitles
            if s.pipeline.steps.subtitles
            else s.subtitles.model_copy(update={"enabled": False})
        )
        ass = AssRenderer(sub_cfg, s.video.width, s.video.height).render(
            captions, ctx.path("overlay.ass"), overlays
        )
        ctx.set_artifact("ass", ass)
        ctx.data["caption_count"] = len(captions)
        return {"captions": len(captions), "style": s.subtitles.style}


class RenderFinalStep(Step):
    name = "render_final"

    def run(self, ctx: JobContext) -> dict[str, Any]:
        s = self.settings
        video = ctx.artifact("synced_video") or ctx.artifact("cut_video")
        assert video is not None
        audio = ctx.artifact("mixed_audio")
        ass = ctx.artifact("ass")
        logo = None
        if s.pipeline.steps.branding and s.video.branding.logo_path:
            lp = Path(s.video.branding.logo_path)
            lp = lp if lp.is_absolute() else s.paths.assets / lp
            logo = lp if lp.exists() else None
        out = ctx.path("final.mp4")
        VideoEditor(s.video, self.ff).render_final(
            video,
            audio,
            out,
            ass_path=ass,
            duration=float(ctx.data["final_duration"]),
            logo=logo,
            logo_position=(40, 60 + s.video.progress_bar.height) if logo else None,
        )
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
        # Low-disk: the cut/synced intermediates are superseded by final.mp4 now.
        # Only the artefacts the resume logic requires for *later* steps are kept.
        # (the thumbnail step still wants the clean, overlay-free video, so the
        # *last* pre-overlay file is dropped there; cut.mp4 goes now when synced.mp4 exists)
        if s.pipeline.delete_intermediates_early and ctx.artifact("synced_video") != ctx.artifact(
            "cut_video"
        ):
            freed = _drop_artifacts(ctx, "cut_video")
            if freed:
                log.info("Deleted superseded intermediate cut.mp4 (%.0f MB)", freed / 1e6)
        return ctx.data["final"]


class ThumbnailStep(Step):
    name = "thumbnail"

    def run(self, ctx: JobContext) -> dict[str, Any]:
        s = self.settings
        final = ctx.artifact("final_video")
        assert final is not None
        script = _load_script(ctx) if ctx.has_artifact("script_json") else None
        title = script.title if script else ctx.data.get("title") or ctx.source.stem
        hook = script.hook if script else ""
        keywords = script.keywords if script else []
        gen = ThumbnailGenerator(s.thumbnail, s.video.branding, s.project.brand)
        at = max(0.2, float(ctx.data["final_duration"]) * s.thumbnail.frame_position)
        frame = self.ff.extract_frame(
            ctx.artifact("synced_video") or final, ctx.path("thumbnail_frame.png"), at=at
        )
        thumb = gen.render(
            frame,
            ctx.path("thumbnail.jpg"),
            title,
            subtitle=hook if s.thumbnail.overlay_title else "",
        )
        ctx.set_artifact("thumbnail", thumb)
        ctx.set_artifact("thumbnail_frame", frame)
        if s.thumbnail.canva_brief:
            brief = gen.build_brief(title, hook, keywords, ctx.data.get("website_hint", ""))
            files = gen.write_brief(brief, ctx.work_dir)
            ctx.set_artifact("brief_md", files["md"])
            ctx.set_artifact("brief_json", files["json"])
        # Low-disk: the pre-overlay video was only still needed for this clean frame grab.
        if s.pipeline.delete_intermediates_early:
            freed = _drop_artifacts(ctx, "synced_video", "cut_video")
            if freed:
                log.info("Deleted superseded pre-overlay video (%.0f MB)", freed / 1e6)
        return {"thumbnail": str(thumb)}


class SocialStep(Step):
    name = "social"

    def run(self, ctx: JobContext) -> dict[str, Any]:
        s = self.settings
        script = (
            _load_script(ctx)
            if ctx.has_artifact("script_json")
            else Script(
                hook=ctx.source.stem, body=[], cta=s.script.cta_default, title=ctx.source.stem
            )
        )
        recent = self.db.recent_hashtag_sets(s.social.hashtags.rotation_memory)
        pkg = SocialWriter(s.social, brand=s.project.brand).write(
            script, recent_hashtags=recent, website=ctx.data.get("website_hint", "")
        )
        self.db.add_hashtag_set(ctx.job_id, pkg.hashtags)
        atomic_write_json(ctx.path("social.json"), pkg.to_dict())
        atomic_write_text(ctx.path("caption.md"), pkg.to_markdown())
        ctx.set_artifact("social_json", ctx.path("social.json"))
        ctx.set_artifact("social_md", ctx.path("caption.md"))
        ctx.data["caption"] = pkg.full_caption
        return {"hashtags": len(pkg.hashtags), "caption_chars": len(pkg.full_caption)}


class PackageStep(Step):
    name = "package"

    def run(self, ctx: JobContext) -> dict[str, Any]:
        s = self.settings
        final = ctx.artifact("final_video")
        assert final is not None
        files = {
            k: ctx.artifact(k)
            for k in (
                "thumbnail",
                "social_md",
                "social_json",
                "script_md",
                "script_json",
                "brief_md",
                "brief_json",
                "ass",
                "srt",
                "captions_json",
                "captions_txt",
                "transcript_txt",
                "transcript_srt",
                "transcript_json",
                "narration",
                # v0.3 (absent in classic runs -> silently skipped)
                "grounded_json",
                "quality_json",
                "quality_md",
                "edit_plan_json",
                "overlay_json",
            )
        }
        out_dir = UploadPackager(s.paths.output).build(
            slug=ctx.slug,
            final_video=final,
            files=files,
            caption_text=ctx.data.get("caption", ""),
            manifest_extra={
                "job_id": ctx.job_id,
                "title": ctx.data.get("title", ctx.source.stem),
                "source": ctx.source.name,
                "duration": ctx.data.get("final", {}).get("duration"),
                "edit": ctx.data.get("edit"),
                "narration": {
                    k: v for k, v in (ctx.data.get("narration") or {}).items() if k != "sentences"
                },
                "sync": ctx.data.get("sync"),
                "crop": ctx.data.get("crop"),
                "captions": ctx.data.get("caption_count"),
                "understanding": ctx.data.get("understanding"),
                "quality": (ctx.data.get("quality") or {}).get("summary"),
                "cover": (ctx.data.get("cover") or {}).get("concept"),
                "settings": {
                    "tts": s.tts.engine,
                    "subtitle_style": s.subtitles.style,
                    "whisper": s.transcription.model_size,
                },
            },
        )
        ctx.data["output_dir"] = str(out_dir)
        self.db.update_job(ctx.job_id, output_dir=str(out_dir))
        return {"output_dir": str(out_dir)}


class AnalyticsInitStep(Step):
    """Register the video in analytics with a zero baseline so it appears in reports."""

    name = "analytics"

    def run(self, ctx: JobContext) -> dict[str, Any]:
        for platform in self.settings.social.platform_defaults:
            self.db.add_metrics(
                ctx.job_id,
                platform,
                views=0,
                likes=0,
                comments=0,
                shares=0,
                notes="baseline (auto)",
            )
        return {"platforms": self.settings.social.platform_defaults}


class ArchiveStep(Step):
    name = "archive"

    def run(self, ctx: JobContext) -> dict[str, Any]:
        s = self.settings
        extra = [p for p in (ctx.artifact("transcript_json"), ctx.artifact("script_json")) if p]
        dest = Archiver(s.paths.archive).archive_job(
            job_id=ctx.job_id,
            slug=ctx.slug,
            source=ctx.source,
            extra_files=extra,
            delete_source=True,
            metadata={"output_dir": ctx.data.get("output_dir")},
        )
        ctx.data["archive_dir"] = str(dest)
        new_source = dest / ctx.source.name
        if new_source.exists():
            ctx.source = new_source
            self.db.update_job(ctx.job_id, source_path=str(new_source))
        return {"archive_dir": str(dest)}


class CleanupWorkStep(Step):
    name = "cleanup_work"

    @property
    def enabled(self) -> bool:
        return self.settings.pipeline.cleanup_work_on_success

    def run(self, ctx: JobContext) -> dict[str, Any]:
        # Never delete work files before the output package is verified: the
        # packaged video must exist, be readable and match the rendered final.
        out_dir = Path(ctx.data.get("output_dir") or "")
        packaged = out_dir / f"{ctx.slug}.mp4"
        if not out_dir.is_dir() or not packaged.exists():
            raise RuntimeError(f"output package missing ({packaged}); keeping work directory")
        expected = int((ctx.data.get("final") or {}).get("size_bytes") or 0)
        if expected and packaged.stat().st_size != expected:
            raise RuntimeError(
                f"packaged video size {packaged.stat().st_size} != rendered {expected}; keeping work directory"
            )
        if not (out_dir / "manifest.json").exists():
            raise RuntimeError("manifest.json missing from package; keeping work directory")
        # Everything needed lives in output/ now: remove the work dir (state.json stays for traceability).
        removed = 0
        for p in ctx.work_dir.iterdir():
            if p.name == "state.json":
                continue
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
            removed += 1
        return {"removed": removed}


DEFAULT_STEPS: list[type[Step]] = [
    ProbeStep,
    ExtractAudioStep,
    TranscribeStep,
    ScriptStep,
    NarrationStep,
    AnalyseStep,
    RenderCutStep,
    SyncStep,
    SubtitlesStep,
    RenderFinalStep,
    ThumbnailStep,
    SocialStep,
    PackageStep,
    AnalyticsInitStep,
    ArchiveStep,
    CleanupWorkStep,
]


# ------------------------------------------------------------------ helpers
def _drop_artifacts(ctx: JobContext, *keys: str) -> int:
    """Delete the files behind ``keys`` (if they exist) and forget them; returns bytes freed."""
    freed = 0
    seen: set[Path] = set()
    for key in keys:
        p = ctx.artifact(key)
        if p is None:
            continue
        if p.exists() and p not in seen and p.parent == ctx.work_dir:
            freed += p.stat().st_size
            p.unlink(missing_ok=True)
            seen.add(p)
        ctx.artifacts.pop(key, None)
    return freed


def _load_transcript(ctx: JobContext) -> Transcript:
    p = ctx.artifact("transcript_json")
    if p and p.exists():
        return Transcript.load(p)
    return Transcript(language="en", duration=float(ctx.data["media"]["duration"]), segments=[])


def _load_script(ctx: JobContext) -> Script:
    p = ctx.artifact("script_json")
    data = read_json(p) if p else None
    if not data:
        raise FileNotFoundError("script.json missing - run the script step first")
    return Script.from_dict(data)


def _website_from_filename(source: Path) -> str:
    """``smallpdf.com - convert.mp4`` or ``smallpdf_com.mkv`` -> ``smallpdf.com``."""
    import re

    stem = source.stem.replace("_", ".")
    m = re.search(r"([a-z0-9-]+\.(?:com|org|net|pk|io|ai|app|edu|co|dev|xyz))", stem, re.I)
    return m.group(1).lower() if m else ""


def _narration_transcript(narr: dict[str, Any], script: Script, duration: float) -> Transcript:
    """Word timings for narration.

    Preferred: the Whisper-aligned per-word timings stored by ``NarrationStep``
    (narration clock == final clock, see ``contentforge/ai/alignment.py``).
    Fallback: distribute each sentence's words evenly across its TTS timing.
    """
    from contentforge.ai.alignment import Alignment
    from contentforge.ai.transcriber import TranscriptSegment, TranscriptWord

    if narr.get("alignment"):
        try:
            al = Alignment.from_dict(narr["alignment"])
            if al.ok:
                sentences = [
                    (float(s), float(e), str(t)) for s, e, t in narr.get("sentences") or []
                ]
                return al.to_transcript(sentences or None, duration)
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("Stored word alignment unusable (%s); sentence timings", exc)

    segments = []
    for i, (start, end, text) in enumerate(narr.get("sentences") or []):
        words = str(text).split()
        if not words:
            continue
        span = max(0.2, float(end) - float(start))
        per = span / len(words)
        tw = [
            TranscriptWord(w, float(start) + j * per, float(start) + (j + 1) * per)
            for j, w in enumerate(words)
        ]
        segments.append(TranscriptSegment(i, float(start), float(end), str(text), tw))
    if not segments:
        return Transcript.from_text(
            script.narration, min(duration, float(narr.get("duration", duration)))
        )
    return Transcript(language="en", duration=duration, segments=segments, engine="narration")


def _remap_transcript(transcript: Transcript, ctx: JobContext) -> Transcript:
    """Map original speech timings through the edit timeline (cuts)."""
    from contentforge.ai.transcriber import TranscriptSegment, TranscriptWord

    edit = ctx.data.get("edit")
    if not edit:
        return transcript
    tl = Timeline([tuple(k) for k in edit["keep"]])
    segments = []
    for seg in transcript.segments:
        words = []
        for w in seg.words:
            r = tl.remap_range(w.start, w.end)
            if r:
                words.append(TranscriptWord(w.word, r[0], r[1], w.probability))
        r = tl.remap_range(seg.start, seg.end)
        if r or words:
            s = r[0] if r else words[0].start
            e = r[1] if r else words[-1].end
            text = seg.text if r else " ".join(w.word for w in words)
            segments.append(TranscriptSegment(seg.id, s, e, text, words))
    return Transcript(
        language=transcript.language,
        duration=tl.output_duration,
        segments=segments,
        engine=transcript.engine,
        model=transcript.model,
    )


def _atempo_chain(factor: float) -> str:
    """ffmpeg atempo only accepts 0.5-2.0 per instance; chain as needed."""
    parts = []
    f = factor
    while f > 2.0:
        parts.append("atempo=2.0")
        f /= 2.0
    while f < 0.5:
        parts.append("atempo=0.5")
        f /= 0.5
    parts.append(f"atempo={f:.5f}")
    return ",".join(parts)


def now() -> float:
    return time.time()

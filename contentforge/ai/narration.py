"""Narration built from a **grounded script** and fitted to the edit timeline.

The v0.2 narration step synthesised one blob of speech and then stretched the
*video* to match it.  That is how a 76 s recording ends up with 13 s of
narration (or a video slowed to a crawl).

v0.3 works the other way round: the edit plan owns the clock.  Every
:class:`~contentforge.models.schemas.ScriptSegment` has a slot on the output
timeline, each slot is synthesised separately, gently time-fitted (atempo, never
beyond ``max_stretch``) and placed at its slot start.  The result is a narration
track that is exactly as long as the Reel and lines up with what is on screen.

The TTS abstraction is untouched - any :class:`~contentforge.ai.tts.base.TTSEngine`
(Piper, Kokoro, Edge, eSpeak) can be plugged in, and if none is available the
builder degrades gracefully instead of raising.
"""

from __future__ import annotations

import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from contentforge.ai.tts.base import TTSEngine, TTSError
from contentforge.log import get_logger
from contentforge.models.schemas import GroundedScript, ScriptSegment
from contentforge.utils.ffmpeg import FFmpeg

log = get_logger("narration")


@dataclass
class NarrationSegment:
    start: float
    end: float
    text: str
    role: str = "demo"
    tempo: float = 1.0
    raw_duration: float = 0.0

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "text": self.text,
            "role": self.role,
            "tempo": round(self.tempo, 3),
            "raw_duration": round(self.raw_duration, 3),
        }


@dataclass
class NarrationResult:
    path: Path
    duration: float
    engine: str
    voice: str
    sample_rate: int
    segments: list[NarrationSegment] = field(default_factory=list)
    timeline_duration: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def speech_seconds(self) -> float:
        return sum(s.duration for s in self.segments)

    @property
    def coverage(self) -> float:
        """Share of the Reel that actually has narration on it."""
        if self.timeline_duration <= 0:
            return 0.0
        return min(1.0, self.speech_seconds / self.timeline_duration)

    def sentence_timings(self) -> list[tuple[float, float, str]]:
        return [(s.start, s.end, s.text) for s in self.segments]

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "duration": round(self.duration, 3),
            "engine": self.engine,
            "voice": self.voice,
            "sample_rate": self.sample_rate,
            "timeline_duration": round(self.timeline_duration, 3),
            "coverage": round(self.coverage, 3),
            "segments": [s.to_dict() for s in self.segments],
            "notes": self.notes,
        }


class NarrationBuilder:
    """Synthesises a grounded script into a timeline-aligned narration track."""

    def __init__(
        self,
        engine: TTSEngine,
        ffmpeg: FFmpeg | None = None,
        *,
        sample_rate: int = 24000,
        max_stretch: float = 1.22,  # never speed speech up more than this
        min_stretch: float = 0.88,
        tail_seconds: float = 0.25,
    ):
        self.engine = engine
        self.ff = ffmpeg or FFmpeg()
        self.sample_rate = sample_rate
        self.max_stretch = max_stretch
        self.min_stretch = min_stretch
        self.tail_seconds = tail_seconds

    # ------------------------------------------------------------------ api
    def build(self, script: GroundedScript, out_path: Path, *, work_dir: Path | None = None) -> NarrationResult:
        segments = [s for s in script.segments if s.text.strip()]
        if not segments:
            raise TTSError("grounded script has no narration segments")
        work = work_dir or out_path.parent / "narration_parts"
        work.mkdir(parents=True, exist_ok=True)
        timeline_duration = max(s.end for s in segments)

        parts: list[tuple[Path, NarrationSegment]] = []
        engine_name, voice = self.engine.name, self.engine.voice
        for i, seg in enumerate(segments):
            raw = work / f"seg_{i:02d}_raw.wav"
            result = self.engine.synthesize(seg.text, raw)
            fitted, tempo = self._fit(raw, work / f"seg_{i:02d}.wav", result.duration, seg)
            ns = NarrationSegment(
                start=seg.start,
                end=seg.start + self._duration(fitted),
                text=seg.text,
                role=seg.role,
                tempo=tempo,
                raw_duration=result.duration,
            )
            parts.append((fitted, ns))
            engine_name, voice = result.engine, result.voice

        # keep segments from overlapping after fitting
        for (_, a), (_, b) in zip(parts, parts[1:]):
            if a.end > b.start:
                a.end = max(a.start + 0.2, b.start - 0.02)

        total = max(timeline_duration, max(ns.end for _, ns in parts)) + self.tail_seconds
        self._assemble([(p, ns.start) for p, ns in parts], out_path, total)
        duration = self._duration(out_path)
        result = NarrationResult(
            path=out_path,
            duration=duration,
            engine=engine_name,
            voice=voice,
            sample_rate=self.sample_rate,
            segments=[ns for _, ns in parts],
            timeline_duration=timeline_duration,
        )
        stretched = [ns for ns in result.segments if abs(ns.tempo - 1.0) > 0.02]
        if stretched:
            result.notes.append(f"{len(stretched)} segment(s) time-fitted to their visual slot")
        log.info(
            "Narration: %.1fs over a %.1fs timeline (%.0f%% coverage, engine=%s)",
            duration,
            timeline_duration,
            result.coverage * 100,
            engine_name,
        )
        for f in work.glob("seg_*_raw.wav"):
            f.unlink(missing_ok=True)
        return result

    # -------------------------------------------------------------- helpers
    def _fit(self, src: Path, dst: Path, duration: float, seg: ScriptSegment) -> tuple[Path, float]:
        """Time-fit one utterance into its visual slot with atempo."""
        slot = max(0.4, seg.duration)
        if duration <= 0:
            return src, 1.0
        tempo = duration / slot
        tempo = max(self.min_stretch, min(self.max_stretch, tempo))
        if abs(tempo - 1.0) < 0.03:
            src.replace(dst)
            return dst, 1.0
        self.ff.run(
            [
                "-i",
                str(src),
                "-filter:a",
                f"atempo={tempo:.4f}",
                "-ar",
                str(self.sample_rate),
                "-ac",
                "1",
                "-c:a",
                "pcm_s16le",
                str(dst),
            ]
        )
        return dst, tempo

    def _assemble(self, parts: list[tuple[Path, float]], out_path: Path, total: float) -> None:
        """Place every utterance at its slot start on one silent bed."""
        if not parts:
            raise TTSError("no narration parts to assemble")
        args: list[str] = []
        filters: list[str] = []
        args += ["-f", "lavfi", "-t", f"{total:.3f}", "-i", f"anullsrc=r={self.sample_rate}:cl=mono"]
        labels = ["[0:a]"]
        for i, (path, start) in enumerate(parts, start=1):
            args += ["-i", str(path)]
            delay_ms = int(round(max(0.0, start) * 1000))
            filters.append(
                f"[{i}:a]aresample={self.sample_rate},aformat=channel_layouts=mono,"
                f"adelay={delay_ms}|{delay_ms}[d{i}]"
            )
            labels.append(f"[d{i}]")
        filters.append(
            "".join(labels) + f"amix=inputs={len(labels)}:duration=first:dropout_transition=0:normalize=0[out]"
        )
        args += [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[out]",
            "-t",
            f"{total:.3f}",
            "-ar",
            str(self.sample_rate),
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(out_path),
        ]
        self.ff.run(args)

    def _duration(self, path: Path) -> float:
        try:
            with wave.open(str(path), "rb") as w:
                return w.getnframes() / float(w.getframerate() or 1)
        except (OSError, wave.Error):
            return self.ff.duration(path)


def fit_script_to_timeline(
    script: GroundedScript, speaking_rate_wps: float = 2.6, safety: float = 0.92
) -> GroundedScript:
    """Trim segment texts so each one can actually be spoken in its slot.

    This is what keeps narration and picture in sync *before* a single sample is
    synthesised - the TTS fitting afterwards only has to make small corrections.
    """
    for seg in script.segments:
        budget = max(2, int(seg.duration * speaking_rate_wps * safety))
        words = seg.text.split()
        if len(words) > budget:
            trimmed = " ".join(words[:budget]).rstrip(",;:")
            if not trimmed.endswith((".", "!", "?")):
                trimmed += "."
            seg.text = trimmed
    return script

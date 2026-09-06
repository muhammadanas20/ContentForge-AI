"""Speech recognition with Faster Whisper.

Produces a :class:`Transcript` with word-level timestamps and writes TXT, SRT
and JSON artefacts. The model is loaded lazily and cached per process.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from contentforge.config.schema import TranscriptionConfig
from contentforge.log import get_logger
from contentforge.utils import atomic_write_json, atomic_write_text, format_srt_time, read_json

log = get_logger("transcriber")

_MODEL_CACHE: dict[tuple, Any] = {}


@dataclass
class TranscriptWord:
    word: str
    start: float
    end: float
    probability: float = 1.0


@dataclass
class TranscriptSegment:
    id: int
    start: float
    end: float
    text: str
    words: list[TranscriptWord] = field(default_factory=list)


@dataclass
class Transcript:
    language: str
    duration: float
    segments: list[TranscriptSegment]
    language_probability: float = 1.0
    engine: str = "faster-whisper"
    model: str = ""

    # ---------------------------------------------------------- accessors
    @property
    def text(self) -> str:
        return " ".join(s.text.strip() for s in self.segments if s.text.strip())

    @property
    def words(self) -> list[TranscriptWord]:
        out: list[TranscriptWord] = []
        for s in self.segments:
            out.extend(s.words)
        return out

    @property
    def word_count(self) -> int:
        return len(self.text.split())

    def speech_intervals(self, gap: float = 0.4) -> list[tuple[float, float]]:
        """Merge words/segments into continuous speech ranges separated by ``gap`` seconds."""
        units = [(w.start, w.end) for w in self.words] or [(s.start, s.end) for s in self.segments]
        out: list[tuple[float, float]] = []
        for start, end in sorted(units):
            if out and start - out[-1][1] <= gap:
                out[-1] = (out[-1][0], max(out[-1][1], end))
            else:
                out.append((start, end))
        return out

    # ------------------------------------------------------ serialisation
    def to_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "language_probability": self.language_probability,
            "duration": self.duration,
            "engine": self.engine,
            "model": self.model,
            "text": self.text,
            "segments": [asdict(s) for s in self.segments],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Transcript:
        segs = [
            TranscriptSegment(
                id=s["id"],
                start=s["start"],
                end=s["end"],
                text=s["text"],
                words=[TranscriptWord(**w) for w in s.get("words", [])],
            )
            for s in data.get("segments", [])
        ]
        return cls(
            language=data.get("language", "en"),
            duration=float(data.get("duration", 0)),
            segments=segs,
            language_probability=float(data.get("language_probability", 1.0)),
            engine=data.get("engine", ""),
            model=data.get("model", ""),
        )

    def to_srt(self) -> str:
        lines = []
        for i, s in enumerate(self.segments, 1):
            lines.append(
                f"{i}\n{format_srt_time(s.start)} --> {format_srt_time(s.end)}\n{s.text.strip()}\n"
            )
        return "\n".join(lines)

    def save(self, base: Path) -> dict[str, Path]:
        """Write ``<base>.txt/.srt/.json`` and return the paths."""
        base = Path(base)
        out = {
            "txt": atomic_write_text(base.with_suffix(".txt"), self.text + "\n"),
            "srt": atomic_write_text(base.with_suffix(".srt"), self.to_srt()),
            "json": atomic_write_json(base.with_suffix(".json"), self.to_dict()),
        }
        return out

    @classmethod
    def load(cls, json_path: Path) -> Transcript:
        data = read_json(json_path)
        if data is None:
            raise FileNotFoundError(json_path)
        return cls.from_dict(data)

    @classmethod
    def from_text(cls, text: str, duration: float, words_per_second: float = 2.6) -> Transcript:
        """Synthesise evenly spaced word timings for plain text (used for narration timing fallback)."""
        words = text.split()
        if not words:
            return cls(language="en", duration=duration, segments=[])
        per = duration / len(words) if duration > 0 else 1.0 / words_per_second
        tw = [TranscriptWord(w, i * per, (i + 1) * per) for i, w in enumerate(words)]
        seg = TranscriptSegment(0, 0.0, duration, text, tw)
        return cls(language="en", duration=duration, segments=[seg], engine="synthetic")


class Transcriber:
    """Faster-Whisper based transcriber."""

    def __init__(self, config: TranscriptionConfig):
        self.config = config

    def _model(self):
        key = (
            self.config.model_size,
            self.config.device,
            self.config.compute_type,
            str(self.config.download_root),
        )
        if key not in _MODEL_CACHE:
            from faster_whisper import WhisperModel  # heavy import, keep lazy

            Path(self.config.download_root).mkdir(parents=True, exist_ok=True)
            log.info("Loading Whisper model '%s' (%s/%s)", *key[:3])
            t0 = time.time()
            _MODEL_CACHE[key] = WhisperModel(
                self.config.model_size,
                device=self.config.device,
                compute_type=self.config.compute_type,
                download_root=str(self.config.download_root),
            )
            log.info("Whisper model ready in %.1fs", time.time() - t0)
        return _MODEL_CACHE[key]

    def transcribe(self, audio_path: str | Path) -> Transcript:
        model = self._model()
        segments_iter, info = model.transcribe(
            str(audio_path),
            language=self.config.language,
            beam_size=self.config.beam_size,
            vad_filter=self.config.vad_filter,
            word_timestamps=self.config.word_timestamps,
        )
        segments: list[TranscriptSegment] = []
        for i, seg in enumerate(segments_iter):
            words = [
                TranscriptWord(
                    w.word.strip(), float(w.start), float(w.end), float(w.probability or 1.0)
                )
                for w in (seg.words or [])
                if w.word.strip()
            ]
            segments.append(
                TranscriptSegment(i, float(seg.start), float(seg.end), seg.text.strip(), words)
            )
        transcript = Transcript(
            language=info.language,
            language_probability=float(info.language_probability or 1.0),
            duration=float(info.duration or 0.0),
            segments=segments,
            model=self.config.model_size,
        )
        log.info(
            "Transcribed %.1fs of audio: %d segments, %d words (lang=%s)",
            transcript.duration,
            len(segments),
            transcript.word_count,
            transcript.language,
        )
        return transcript

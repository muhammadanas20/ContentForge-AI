"""Abstract TTS engine interface."""

from __future__ import annotations

import abc
import re
from dataclasses import dataclass, field
from pathlib import Path


class TTSError(RuntimeError):
    """Raised when synthesis fails or the engine is unavailable."""


@dataclass
class TTSResult:
    path: Path
    duration: float
    engine: str
    voice: str
    sample_rate: int
    # Optional per-sentence timings (start, end, text) when the engine can provide them.
    sentence_timings: list[tuple[float, float, str]] = field(default_factory=list)


class TTSEngine(abc.ABC):
    """Common interface for all TTS backends."""

    name: str = "base"

    @abc.abstractmethod
    def is_available(self) -> bool:
        """True when the runtime dependency (and model) can be used right now."""

    @abc.abstractmethod
    def synthesize(self, text: str, out_path: Path) -> TTSResult:
        """Synthesize ``text`` into a WAV file at ``out_path``."""

    @property
    @abc.abstractmethod
    def voice(self) -> str: ...

    # ------------------------------------------------------------ helpers
    @staticmethod
    def split_sentences(text: str) -> list[str]:
        """Split narration into sentence chunks (engines synthesise per sentence for timing)."""
        parts = re.split(r"(?<=[.!?])\s+", text.strip())
        return [p.strip() for p in parts if p.strip()]

    @staticmethod
    def normalise_text(text: str) -> str:
        """Expand things TTS engines mispronounce (URLs, brand names, symbols)."""
        t = text
        t = re.sub(r"\bStudentTools\.pk\b", "Student Tools dot P K", t)
        t = re.sub(
            r"\b(\w+)\.(com|org|net|pk|io|ai|edu)\b",
            lambda m: f"{m.group(1)} dot {m.group(2).upper()}",
            t,
        )
        t = t.replace("&", " and ").replace("%", " percent").replace("@", " at ")
        t = re.sub(r"\s+", " ", t).strip()
        return t

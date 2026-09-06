"""Kokoro TTS backend (high quality, needs the ``kokoro`` package + torch)."""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

from contentforge.ai.tts.base import TTSEngine, TTSError, TTSResult
from contentforge.config.schema import KokoroConfig
from contentforge.log import get_logger

log = get_logger("tts.kokoro")

KOKORO_SAMPLE_RATE = 24000


class KokoroTTS(TTSEngine):
    name = "kokoro"

    def __init__(self, config: KokoroConfig, speed: float = 1.0):
        self.config = config
        self.speed = speed
        self._pipeline = None

    @property
    def voice(self) -> str:
        return self.config.voice

    def is_available(self) -> bool:
        try:
            import kokoro  # noqa: F401
        except ImportError:
            return False
        return True

    def _load(self):
        if self._pipeline is None:
            try:
                from kokoro import KPipeline  # type: ignore
            except ImportError as exc:
                raise TTSError("kokoro is not installed (pip install kokoro soundfile)") from exc
            log.info("Loading Kokoro pipeline (lang=%s)", self.config.lang_code)
            self._pipeline = KPipeline(lang_code=self.config.lang_code)
        return self._pipeline

    def synthesize(self, text: str, out_path: Path) -> TTSResult:
        pipeline = self._load()
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        text = self.normalise_text(text)
        sentences = self.split_sentences(text) or [text]
        pause = np.zeros(int(KOKORO_SAMPLE_RATE * 0.25), dtype=np.float32)
        timings: list[tuple[float, float, str]] = []
        buffers: list[np.ndarray] = []
        cursor = 0.0
        for i, sentence in enumerate(sentences):
            parts = []
            for _, _, audio in pipeline(sentence, voice=self.config.voice, speed=self.speed):
                arr = audio.numpy() if hasattr(audio, "numpy") else np.asarray(audio)
                parts.append(arr.astype(np.float32))
            audio = np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)
            start = cursor
            buffers.append(audio)
            cursor += len(audio) / KOKORO_SAMPLE_RATE
            timings.append((start, cursor, sentence))
            if i < len(sentences) - 1:
                buffers.append(pause)
                cursor += len(pause) / KOKORO_SAMPLE_RATE
        pcm = np.concatenate(buffers) if buffers else np.zeros(0, dtype=np.float32)
        pcm16 = np.clip(pcm * 32767, -32768, 32767).astype(np.int16)
        with wave.open(str(out_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(KOKORO_SAMPLE_RATE)
            wf.writeframes(pcm16.tobytes())
        return TTSResult(
            out_path, cursor, self.name, self.config.voice, KOKORO_SAMPLE_RATE, timings
        )

"""Microsoft Edge neural TTS backend (online, free, no API key).

Edge TTS returns MP3; we transcode to WAV with ffmpeg so downstream mixing is
uniform. Sentence timings are computed from per-sentence synthesis.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from contentforge.ai.tts.base import TTSEngine, TTSError, TTSResult
from contentforge.config.schema import EdgeConfig
from contentforge.log import get_logger
from contentforge.utils.ffmpeg import FFmpeg

log = get_logger("tts.edge")


class EdgeTTS(TTSEngine):
    name = "edge"

    def __init__(self, config: EdgeConfig, speed: float = 1.0, sample_rate: int = 24000):
        self.config = config
        self.speed = speed
        self.sample_rate = sample_rate

    @property
    def voice(self) -> str:
        return self.config.voice

    def is_available(self) -> bool:
        try:
            import edge_tts  # noqa: F401
        except ImportError:
            return False
        return True

    def _rate(self) -> str:
        """Combine configured rate with the global speed multiplier -> ``+15%`` style string."""
        base = int(self.config.rate.strip("%") or 0)
        pct = base + int(round((self.speed - 1.0) * 100))
        return f"{pct:+d}%"

    async def _synth_one(self, text: str, mp3_path: Path) -> None:
        import edge_tts  # type: ignore

        communicate = edge_tts.Communicate(
            text, self.config.voice, rate=self._rate(), pitch=self.config.pitch
        )
        await communicate.save(str(mp3_path))

    def synthesize(self, text: str, out_path: Path) -> TTSResult:
        if not self.is_available():
            raise TTSError("edge-tts is not installed (pip install edge-tts)")
        ff = FFmpeg()
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        text = self.normalise_text(text)
        sentences = self.split_sentences(text) or [text]
        tmp_dir = out_path.parent / (out_path.stem + "_edge_parts")
        tmp_dir.mkdir(exist_ok=True)
        wavs: list[Path] = []
        timings: list[tuple[float, float, str]] = []
        cursor = 0.0
        try:
            for i, sentence in enumerate(sentences):
                mp3 = tmp_dir / f"{i:03d}.mp3"
                wav = tmp_dir / f"{i:03d}.wav"
                try:
                    asyncio.run(self._synth_one(sentence, mp3))
                except Exception as exc:
                    raise TTSError(f"Edge TTS failed: {exc}") from exc
                if not mp3.exists() or mp3.stat().st_size == 0:
                    raise TTSError("Edge TTS returned no audio (network blocked?)")
                ff.run(
                    [
                        "-i",
                        str(mp3),
                        "-ac",
                        "1",
                        "-ar",
                        str(self.sample_rate),
                        "-c:a",
                        "pcm_s16le",
                        str(wav),
                    ]
                )
                d = ff.duration(wav)
                timings.append((cursor, cursor + d, sentence))
                cursor += d
                wavs.append(wav)
                if i < len(sentences) - 1:
                    gap = tmp_dir / f"{i:03d}_gap.wav"
                    ff.make_silence(gap, 0.25, self.sample_rate)
                    wavs.append(gap)
                    cursor += 0.25
            ff.concat_files(wavs, out_path)
        finally:
            for f in tmp_dir.glob("*"):
                f.unlink(missing_ok=True)
            tmp_dir.rmdir()
        return TTSResult(out_path, cursor, self.name, self.config.voice, self.sample_rate, timings)

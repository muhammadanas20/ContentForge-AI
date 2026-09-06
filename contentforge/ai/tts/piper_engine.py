"""Piper TTS backend (fully offline, fast on CPU).

Voices are ONNX models downloaded from HuggingFace
(``rhasspy/piper-voices``). When ``auto_download`` is enabled the model and
its JSON config are fetched into ``models_dir`` on first use.
"""

from __future__ import annotations

import wave
from pathlib import Path

import requests

from contentforge.ai.tts.base import TTSEngine, TTSError, TTSResult
from contentforge.config.schema import PiperConfig
from contentforge.log import get_logger
from contentforge.utils.ffmpeg import FFmpeg, ffmpeg_available

log = get_logger("tts.piper")

HF_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0"


def voice_relative_path(voice: str) -> str:
    """``en_US-lessac-medium`` -> ``en/en_US/lessac/medium/en_US-lessac-medium``."""
    try:
        locale, name, quality = voice.split("-", 2)
    except ValueError as exc:
        raise TTSError(f"Invalid Piper voice name '{voice}' (expected xx_XX-name-quality)") from exc
    lang = locale.split("_")[0]
    return f"{lang}/{locale}/{name}/{quality}/{voice}"


class PiperTTS(TTSEngine):
    name = "piper"

    def __init__(self, config: PiperConfig, speed: float = 1.0):
        self.config = config
        self.speed = speed
        self._voice_obj = None

    @property
    def voice(self) -> str:
        return self.config.voice

    @property
    def model_path(self) -> Path:
        return Path(self.config.models_dir) / f"{self.config.voice}.onnx"

    def is_available(self) -> bool:
        try:
            import piper  # noqa: F401
        except ImportError:
            return False
        return self.model_path.exists() or self.config.auto_download

    # ------------------------------------------------------------- model
    def ensure_model(self) -> Path:
        mp = self.model_path
        cfg = mp.with_suffix(".onnx.json")
        if mp.exists() and cfg.exists():
            return mp
        if not self.config.auto_download:
            raise TTSError(f"Piper voice missing: {mp} (auto_download is off)")
        rel = voice_relative_path(self.config.voice)
        mp.parent.mkdir(parents=True, exist_ok=True)
        for url, dst in ((f"{HF_BASE}/{rel}.onnx", mp), (f"{HF_BASE}/{rel}.onnx.json", cfg)):
            if dst.exists():
                continue
            log.info("Downloading Piper voice: %s", url)
            try:
                with requests.get(url, stream=True, timeout=120) as r:
                    r.raise_for_status()
                    tmp = dst.with_suffix(dst.suffix + ".part")
                    with tmp.open("wb") as fh:
                        for chunk in r.iter_content(1 << 20):
                            fh.write(chunk)
                    tmp.replace(dst)
            except requests.RequestException as exc:
                raise TTSError(
                    f"Failed to download Piper voice '{self.config.voice}': {exc}"
                ) from exc
        return mp

    def _load(self):
        if self._voice_obj is None:
            from piper import PiperVoice  # type: ignore

            mp = self.ensure_model()
            log.info("Loading Piper voice %s", mp.name)
            self._voice_obj = PiperVoice.load(
                str(mp), config_path=str(mp.with_suffix(".onnx.json"))
            )
        return self._voice_obj

    # -------------------------------------------------------- synthesis
    def synthesize(self, text: str, out_path: Path) -> TTSResult:
        voice = self._load()
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        text = self.normalise_text(text)
        sentences = self.split_sentences(text) or [text]
        sample_rate = int(voice.config.sample_rate)
        length_scale = self.config.length_scale / max(0.25, self.speed)

        timings: list[tuple[float, float, str]] = []
        cursor = 0.0
        pause = b"\x00\x00" * int(sample_rate * 0.25)  # 250 ms between sentences
        with wave.open(str(out_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            for i, sentence in enumerate(sentences):
                pcm = self._synth_sentence(voice, sentence, length_scale)
                start = cursor
                wf.writeframes(pcm)
                cursor += len(pcm) / 2 / sample_rate
                timings.append((start, cursor, sentence))
                if i < len(sentences) - 1:
                    wf.writeframes(pause)
                    cursor += len(pause) / 2 / sample_rate
        return TTSResult(out_path, cursor, self.name, self.config.voice, sample_rate, timings)

    def _synth_sentence(self, voice, sentence: str, length_scale: float) -> bytes:
        """Return raw 16-bit PCM for one sentence, across piper-tts API versions."""
        kwargs = {
            "length_scale": length_scale,
            "noise_scale": self.config.noise_scale,
            "noise_w": self.config.noise_w,
        }
        chunks: list[bytes] = []
        # piper-tts >= 1.3 exposes ``synthesize`` yielding AudioChunk objects
        if hasattr(voice, "synthesize") and not hasattr(voice, "synthesize_stream_raw"):
            try:
                from piper import SynthesisConfig  # type: ignore

                syn_cfg = SynthesisConfig(
                    length_scale=kwargs["length_scale"],
                    noise_scale=kwargs["noise_scale"],
                    noise_w_scale=kwargs["noise_w"],
                )
                for chunk in voice.synthesize(sentence, syn_config=syn_cfg):
                    chunks.append(chunk.audio_int16_bytes)
                return b"".join(chunks)
            except (ImportError, TypeError, AttributeError):
                pass
        # piper-tts 1.2 API
        if hasattr(voice, "synthesize_stream_raw"):
            for chunk in voice.synthesize_stream_raw(sentence, **kwargs):
                chunks.append(chunk)
            return b"".join(chunks)
        raise TTSError("Unsupported piper-tts version: no synthesize API found")

    @staticmethod
    def duration_of(path: Path) -> float:
        if ffmpeg_available():
            return FFmpeg().duration(path)
        with wave.open(str(path), "rb") as wf:
            return wf.getnframes() / wf.getframerate()

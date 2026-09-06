"""Engine selection and graceful fallback."""

from __future__ import annotations

from contentforge.ai.tts.base import TTSEngine, TTSError
from contentforge.ai.tts.edge_engine import EdgeTTS
from contentforge.ai.tts.espeak_engine import EspeakTTS
from contentforge.ai.tts.kokoro_engine import KokoroTTS
from contentforge.ai.tts.piper_engine import PiperTTS
from contentforge.config.schema import TTSConfig
from contentforge.log import get_logger

log = get_logger("tts")

# Preference order for automatic fallback. eSpeak is last: it always works
# (no model, no network) so narration is never silently dropped.
ENGINE_ORDER = ("piper", "kokoro", "edge", "espeak")


def _build(name: str, config: TTSConfig) -> TTSEngine:
    if name == "piper":
        return PiperTTS(config.piper, speed=config.speed)
    if name == "kokoro":
        return KokoroTTS(config.kokoro, speed=config.speed)
    if name == "edge":
        return EdgeTTS(config.edge, speed=config.speed, sample_rate=config.output_sample_rate)
    if name == "espeak":
        return EspeakTTS(voice=config.espeak.voice, speed=config.speed, words_per_minute=config.espeak.words_per_minute)
    raise TTSError(f"Unknown TTS engine '{name}'")


def available_engines(config: TTSConfig) -> list[str]:
    out = []
    for name in ENGINE_ORDER:
        try:
            if _build(name, config).is_available():
                out.append(name)
        except Exception:  # pragma: no cover - defensive
            continue
    return out


def get_tts_engine(config: TTSConfig, *, allow_fallback: bool = True) -> TTSEngine:
    """Return the configured engine, or the first available one when ``allow_fallback``."""
    preferred = _build(config.engine, config)
    if preferred.is_available():
        return preferred
    if not allow_fallback:
        raise TTSError(f"TTS engine '{config.engine}' is not available")
    for name in ENGINE_ORDER:
        if name == config.engine:
            continue
        eng = _build(name, config)
        if eng.is_available():
            log.warning("TTS engine '%s' unavailable - falling back to '%s'", config.engine, name)
            return eng
    raise TTSError(
        "No TTS engine available. Install one of: piper-tts (offline), kokoro, edge-tts (online)."
    )

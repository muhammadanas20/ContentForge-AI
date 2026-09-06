"""eSpeak NG TTS backend - the always-available offline fallback.

Piper (neural, best quality) and Edge (online, natural) stay the primary
engines.  eSpeak NG exists so that **narration is never silently skipped**:
it is a ~5 MB formant synthesiser that runs anywhere, needs no model download
and no network, and is what Piper itself uses for phonemisation.

The library is loaded through ``ctypes`` from, in order:

1. ``$ESPEAK_LIBRARY``,
2. the ``espeakng-loader`` package (ships a bundled ``libespeak-ng``),
3. the system library (``libespeak-ng.so.1`` / ``libespeak.so.1``).
"""

from __future__ import annotations

import array
import ctypes
import ctypes.util
import os
import threading
import wave
from pathlib import Path

from contentforge.ai.tts.base import TTSEngine, TTSError, TTSResult
from contentforge.log import get_logger

log = get_logger("tts.espeak")

AUDIO_OUTPUT_SYNCHRONOUS = 0x02
ESPEAK_RATE = 1
ESPEAK_VOLUME = 2
ESPEAK_PITCH = 3
ESPEAK_CHARS_UTF8 = 0x01

_LOCK = threading.Lock()
_STATE: dict[str, object] = {}


def _find_library() -> str | None:
    explicit = os.environ.get("ESPEAK_LIBRARY")
    if explicit and Path(explicit).exists():
        return explicit
    try:  # bundled build (pip install espeakng-loader)
        import espeakng_loader  # type: ignore

        path = str(espeakng_loader.get_library_path())
        if Path(path).exists():
            return path
    except Exception:  # pragma: no cover - optional dependency
        pass
    for name in ("espeak-ng", "espeak"):
        found = ctypes.util.find_library(name)
        if found:
            return found
    for candidate in ("libespeak-ng.so.1", "libespeak-ng.so", "libespeak.so.1"):
        try:
            ctypes.CDLL(candidate)
            return candidate
        except OSError:
            continue
    return None


def _data_path() -> bytes | None:
    explicit = os.environ.get("ESPEAK_DATA_PATH")
    if explicit:
        return explicit.encode()
    try:
        import espeakng_loader  # type: ignore

        return str(espeakng_loader.get_data_path()).encode()
    except Exception:  # pragma: no cover - optional dependency
        return None


def _library():
    """Load + initialise libespeak-ng once per process (thread-safe)."""
    with _LOCK:
        if "lib" in _STATE:
            return _STATE["lib"], int(_STATE["sample_rate"])  # type: ignore[arg-type]
        path = _find_library()
        if not path:
            raise TTSError("libespeak-ng not found (pip install espeakng-loader or dnf install espeak-ng)")
        lib = ctypes.CDLL(path)
        lib.espeak_Initialize.restype = ctypes.c_int
        lib.espeak_Initialize.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        rate = lib.espeak_Initialize(AUDIO_OUTPUT_SYNCHRONOUS, 0, _data_path(), 0)
        if rate <= 0:
            raise TTSError("espeak_Initialize failed")
        _STATE["lib"] = lib
        _STATE["sample_rate"] = rate
        return lib, rate


class EspeakTTS(TTSEngine):
    """Offline formant synthesis through libespeak-ng."""

    name = "espeak"

    def __init__(self, voice: str = "en-us", speed: float = 1.0, words_per_minute: int = 165):
        self._voice = voice
        self.speed = speed
        self.words_per_minute = words_per_minute

    @property
    def voice(self) -> str:
        return self._voice

    def is_available(self) -> bool:
        try:
            _library()
            return True
        except Exception:
            return False

    # ------------------------------------------------------------- synth
    def _synth_samples(self, text: str) -> tuple[array.array, int]:
        lib, rate = _library()
        buf = array.array("h")

        cb_type = ctypes.CFUNCTYPE(
            ctypes.c_int, ctypes.POINTER(ctypes.c_short), ctypes.c_int, ctypes.c_void_p
        )

        def _cb(wav, numsamples, _events):  # pragma: no cover - exercised via synthesize()
            if wav and numsamples > 0:
                buf.extend(wav[i] for i in range(numsamples))
            return 0

        cb = cb_type(_cb)
        with _LOCK:
            lib.espeak_SetSynthCallback(cb)
            lib.espeak_SetVoiceByName(self._voice.encode())
            wpm = int(max(80, min(400, self.words_per_minute * max(0.5, self.speed))))
            lib.espeak_SetParameter(ESPEAK_RATE, wpm, 0)
            payload = text.encode("utf-8")
            lib.espeak_Synth(
                payload,
                len(payload) + 1,
                0,
                0,
                0,
                ESPEAK_CHARS_UTF8,
                None,
                None,
            )
            lib.espeak_Synchronize()
        return buf, rate

    def synthesize(self, text: str, out_path: Path) -> TTSResult:
        clean = self.normalise_text(text)
        if not clean.strip():
            raise TTSError("nothing to synthesize")
        sentences = self.split_sentences(clean) or [clean]
        _lib, rate = _library()
        all_samples = array.array("h")
        timings: list[tuple[float, float, str]] = []
        for sentence in sentences:
            samples, rate = self._synth_samples(sentence)
            start = len(all_samples) / rate
            all_samples.extend(samples)
            # a short breath between sentences keeps the narration listenable
            all_samples.extend([0] * int(rate * 0.18))
            timings.append((start, len(all_samples) / rate, sentence))
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(out_path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(all_samples.tobytes())
        duration = len(all_samples) / rate
        log.info("eSpeak narration: %.2fs, %d sentences -> %s", duration, len(sentences), out_path.name)
        return TTSResult(
            path=out_path,
            duration=duration,
            engine=self.name,
            voice=self._voice,
            sample_rate=rate,
            sentence_timings=timings,
        )

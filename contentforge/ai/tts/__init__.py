"""Text-to-speech backends behind a single interface.

Use :func:`get_tts_engine` to obtain the configured engine; every engine
implements :class:`TTSEngine`.
"""

from contentforge.ai.tts.base import TTSEngine, TTSError, TTSResult
from contentforge.ai.tts.edge_engine import EdgeTTS
from contentforge.ai.tts.factory import available_engines, get_tts_engine
from contentforge.ai.tts.kokoro_engine import KokoroTTS
from contentforge.ai.tts.piper_engine import PiperTTS

__all__ = [
    "EdgeTTS",
    "KokoroTTS",
    "PiperTTS",
    "TTSEngine",
    "TTSError",
    "TTSResult",
    "available_engines",
    "get_tts_engine",
]

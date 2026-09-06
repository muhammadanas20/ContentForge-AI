"""Caption segmentation: turn word timings into short, readable caption chunks."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from contentforge.ai.transcriber import Transcript, TranscriptWord


@dataclass
class CaptionWord:
    text: str
    start: float
    end: float
    highlight: bool = False


@dataclass
class Caption:
    start: float
    end: float
    words: list[CaptionWord] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "text": self.text,
            "words": [
                {
                    "text": w.text,
                    "start": round(w.start, 3),
                    "end": round(w.end, 3),
                    "highlight": w.highlight,
                }
                for w in self.words
            ],
        }


def build_captions(
    transcript: Transcript,
    *,
    max_words: int = 4,
    max_chars: int = 22,
    max_duration: float = 2.2,
    min_duration: float = 0.35,
    highlight_keywords: list[str] | None = None,
    uppercase: bool = True,
    strip_punctuation: bool = False,
) -> list[Caption]:
    """Chunk words into captions of at most ``max_words``/``max_chars`` each.

    Chunks also break on sentence punctuation and when a pause longer than
    0.6 s occurs, which keeps captions in sync with natural phrasing.
    """
    keywords = {k.lower() for k in (highlight_keywords or [])}
    words = transcript.words or Transcript.from_text(transcript.text, transcript.duration).words
    captions: list[Caption] = []
    current: list[CaptionWord] = []

    def flush() -> None:
        if not current:
            return
        start = current[0].start
        end = max(current[-1].end, start + min_duration)
        captions.append(Caption(start, end, list(current)))
        current.clear()

    for w in words:
        text = _clean_word(w.word, uppercase, strip_punctuation)
        if not text:
            continue
        cw = CaptionWord(text, w.start, w.end, highlight=_is_keyword(w, keywords))
        prev = current[-1] if current else None
        if prev is not None:
            too_many = len(current) >= max_words
            too_long = len(" ".join(x.text for x in current)) + 1 + len(text) > max_chars
            too_slow = w.end - current[0].start > max_duration
            pause = w.start - prev.end > 0.6
            sentence_end = prev.text.rstrip()[-1:] in ".!?"
            if too_many or too_long or too_slow or pause or sentence_end:
                flush()
        current.append(cw)
    flush()

    # Avoid overlaps: each caption ends no later than the next starts
    for a, b in zip(captions, captions[1:]):
        if a.end > b.start:
            a.end = max(a.start + 0.1, b.start - 0.01)
    return captions


def _clean_word(word: str, uppercase: bool, strip_punct: bool) -> str:
    w = word.strip()
    if strip_punct:
        w = re.sub(r"[^\w'%-]", "", w)
    if uppercase:
        w = w.upper()
    return w


def _is_keyword(word: TranscriptWord, keywords: set[str]) -> bool:
    core = re.sub(r"[^\w]", "", word.word.lower())
    return bool(core) and (core in keywords or core.rstrip("s") in keywords)

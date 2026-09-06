"""Word-level alignment of the narration script with the synthesised audio.

Why
---
TTS engines return (at best) per-sentence timings.  v0.1 spread the words of a
sentence evenly across it, which is visibly off for long words, numbers and
URLs.  Here the narration WAV is transcribed with Faster-Whisper
(``word_timestamps=True``) and the *known* script text is aligned against the
recognised words, so every script word gets a real start/end time while the
on-screen text stays exactly what was written (ASR errors never leak into the
captions).

Clocks
------
Narration audio is mixed from ``t = 0`` of the final video (see ``SyncStep``:
the video is retimed / freeze-extended to the narration, never the other way
round), so the narration clock *is* the final clock.  The transcript-based
path (no narration) keeps using ``Timeline`` + retime factor as before.

Algorithm
---------
1. Normalise both token streams (lower-case, strip punctuation, expand the
   TTS pronunciation rewrites such as ``StudentTools.pk -> student tools dot p k``).
2. ``difflib.SequenceMatcher`` over the normalised tokens; equal blocks give
   script words a direct timing.
3. Unmatched script words (ASR substitutions/omissions) are interpolated
   proportionally to their character length between the nearest matched
   neighbours; leading/trailing gaps borrow the sentence boundaries.
4. Quality = matched script words / script words.  Below
   ``min_match_ratio`` the caller falls back to sentence-level timings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from contentforge.ai.transcriber import Transcript, TranscriptSegment, TranscriptWord
from contentforge.log import get_logger

log = get_logger("alignment")


@dataclass
class AlignedWord:
    word: str  # original script token (punctuation kept for captions)
    start: float
    end: float
    matched: bool  # True when timing came straight from ASR


@dataclass
class Alignment:
    words: list[AlignedWord] = field(default_factory=list)
    match_ratio: float = 0.0
    asr_words: int = 0
    engine: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.words)

    def to_dict(self) -> dict:
        return {
            "match_ratio": round(self.match_ratio, 3),
            "asr_words": self.asr_words,
            "engine": self.engine,
            "words": [
                [w.word, round(w.start, 3), round(w.end, 3), int(w.matched)] for w in self.words
            ],
        }

    @classmethod
    def from_dict(cls, d: dict) -> Alignment:
        al = cls(
            match_ratio=float(d.get("match_ratio", 0.0)),
            asr_words=int(d.get("asr_words", 0)),
            engine=str(d.get("engine", "")),
        )
        for w, s, e, m in d.get("words", []):
            al.words.append(AlignedWord(str(w), float(s), float(e), bool(m)))
        return al

    def to_transcript(
        self, sentences: list[tuple[float, float, str]] | None, duration: float
    ) -> Transcript:
        """Build a Transcript (segments = sentences) carrying the aligned word timings."""
        segments: list[TranscriptSegment] = []
        words = list(self.words)
        if sentences:
            i = 0
            for sid, (_s, _e, text) in enumerate(sentences):
                n = len(str(text).split())
                chunk = words[i : i + n]
                i += n
                if not chunk:
                    continue
                tw = [TranscriptWord(w.word, w.start, w.end) for w in chunk]
                segments.append(
                    TranscriptSegment(sid, chunk[0].start, chunk[-1].end, str(text), tw)
                )
            rest = words[i:]
            if rest:
                tw = [TranscriptWord(w.word, w.start, w.end) for w in rest]
                segments.append(
                    TranscriptSegment(
                        len(segments),
                        rest[0].start,
                        rest[-1].end,
                        " ".join(w.word for w in rest),
                        tw,
                    )
                )
        else:
            tw = [TranscriptWord(w.word, w.start, w.end) for w in words]
            segments.append(
                TranscriptSegment(
                    0, words[0].start, words[-1].end, " ".join(w.word for w in words), tw
                )
            )
        return Transcript(language="en", duration=duration, segments=segments, engine="aligned")


# --------------------------------------------------------------------------- normalisation
_REWRITES = [
    (re.compile(r"\bstudenttools\.pk\b", re.I), "student tools dot p k"),
    (re.compile(r"\b(\w+)\.(com|org|net|pk|io|ai|edu)\b", re.I), r"\1 dot \2"),
    (re.compile(r"&"), " and "),
    (re.compile(r"%"), " percent"),
]
_NUM_WORDS = {
    "0": "zero",
    "1": "one",
    "2": "two",
    "3": "three",
    "4": "four",
    "5": "five",
    "6": "six",
    "7": "seven",
    "8": "eight",
    "9": "nine",
    "10": "ten",
}


def normalise_tokens(text: str) -> list[list[str]]:
    """Per original whitespace token, the list of normalised sub-tokens it expands to."""
    out: list[list[str]] = []
    for tok in text.split():
        t = tok
        for rx, rep in _REWRITES:
            t = rx.sub(rep, t)
        subs = []
        for part in t.lower().split():
            part = re.sub(r"[^a-z0-9']", "", part)
            if not part:
                continue
            subs.append(_NUM_WORDS.get(part, part))
        out.append(subs)
    return out


def _flatten_asr(transcript: Transcript) -> list[tuple[str, float, float]]:
    flat: list[tuple[str, float, float]] = []
    for w in transcript.words:
        for subs in normalise_tokens(w.word):
            for s in subs:
                flat.append((s, float(w.start), float(w.end)))
    return flat


# --------------------------------------------------------------------------- alignment
def align_script(
    script_text: str,
    asr: Transcript,
    *,
    duration: float | None = None,
    min_match_ratio: float = 0.6,
    min_word_seconds: float = 0.06,
) -> Alignment | None:
    """Align ``script_text`` words to ``asr`` word timings.  ``None`` when not trustworthy."""
    script_tokens = script_text.split()
    if not script_tokens:
        return None
    norm = normalise_tokens(script_text)
    # map flattened script sub-token index -> original token index
    flat_script: list[str] = []
    owner: list[int] = []
    for i, subs in enumerate(norm):
        for s in subs:
            flat_script.append(s)
            owner.append(i)
    asr_flat = _flatten_asr(asr)
    if not asr_flat or not flat_script:
        return None
    asr_norm = [a[0] for a in asr_flat]

    sm = SequenceMatcher(a=flat_script, b=asr_norm, autojunk=False)
    starts: dict[int, float] = {}
    ends: dict[int, float] = {}
    for a0, b0, size in sm.get_matching_blocks():
        for k in range(size):
            tok = owner[a0 + k]
            _w, s, e = asr_flat[b0 + k]
            starts[tok] = min(starts.get(tok, s), s)
            ends[tok] = max(ends.get(tok, e), e)
    matched = sorted(starts)
    ratio = len(matched) / len(script_tokens)
    if ratio < min_match_ratio:
        log.warning(
            "Word alignment rejected: %.0f%% of %d script words matched (min %.0f%%)",
            ratio * 100,
            len(script_tokens),
            min_match_ratio * 100,
        )
        return None

    total = float(duration or asr.duration or (asr_flat[-1][2] + 0.2))
    n = len(script_tokens)
    words: list[AlignedWord] = []
    # anchor points: (index, start, end) for matched tokens; fill the gaps by interpolation
    anchors = [(i, starts[i], ends[i]) for i in matched]
    # enforce monotonic anchors (a rare cross-match can go backwards)
    clean: list[tuple[int, float, float]] = []
    for i, s, e in anchors:
        if clean and s < clean[-1][2] - 0.05:
            continue
        clean.append((i, s, max(e, s + 0.05)))
    anchors = clean
    if not anchors:
        return None
    first_i, first_s, _ = anchors[0]
    last_i, _, last_e = anchors[-1]

    def fill(lo_i: int, hi_i: int, t0: float, t1: float) -> None:
        """Distribute tokens lo_i..hi_i (inclusive) over [t0, t1] by character length."""
        idx = list(range(lo_i, hi_i + 1))
        if not idx:
            return
        lens = [max(1, len(re.sub(r"[^\w]", "", script_tokens[i]))) + 1 for i in idx]
        tot = float(sum(lens))
        t = t0
        span = max(0.0, t1 - t0)
        for i, ln in zip(idx, lens):
            d = span * ln / tot
            words.append(AlignedWord(script_tokens[i], t, t + d, matched=False))
            t += d

    # leading unmatched words: end at first anchor start, start at max(0, first_s - est)
    if first_i > 0:
        est = 0.28 * first_i
        fill(0, first_i - 1, max(0.0, first_s - est), first_s)
    for k, (i, s, e) in enumerate(anchors):
        nxt = anchors[k + 1] if k + 1 < len(anchors) else None
        gap_words = nxt[0] - i - 1 if nxt is not None else 0
        if nxt is not None and gap_words > 0:
            # ASR dropped/merged words: make room between the two anchors so
            # every script word is visible for at least ``min_word_seconds``.
            need = gap_words * min_word_seconds
            room = nxt[1] - e
            if room < need:
                short = need - room
                give_prev = min(short / 2, max(0.0, (e - s) - min_word_seconds))
                e -= give_prev
                give_next = min(short - give_prev, max(0.0, (nxt[2] - nxt[1]) - min_word_seconds))
                nxt = (nxt[0], nxt[1] + give_next, nxt[2])
                anchors[k + 1] = nxt
        words.append(AlignedWord(script_tokens[i], s, e, matched=True))
        if nxt is not None and gap_words > 0:
            fill(i + 1, nxt[0] - 1, e, nxt[1])
    if last_i < n - 1:
        est = (
            min(total - last_e, 0.28 * (n - 1 - last_i))
            if total > last_e
            else 0.28 * (n - 1 - last_i)
        )
        fill(last_i + 1, n - 1, last_e, last_e + max(0.1, est))

    # final sanity: monotonic, non-empty, clamped
    prev_end = 0.0
    for w in words:
        w.start = max(prev_end, w.start)
        w.end = max(w.start + 0.04, w.end)
        prev_end = w.end
    if duration:
        for w in words:
            w.start = min(w.start, duration)
            w.end = min(max(w.end, w.start + 0.02), duration + 0.02)
    al = Alignment(words=words, match_ratio=ratio, asr_words=len(asr_flat), engine=asr.engine)
    log.info(
        "Word alignment: %d/%d script words matched (%.0f%%), %d ASR words",
        len(matched),
        n,
        ratio * 100,
        len(asr_flat),
    )
    return al

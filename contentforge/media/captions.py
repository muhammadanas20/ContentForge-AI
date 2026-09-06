"""Reel overlays: word-highlighted captions, click rings, watermark, progress.

Everything a viewer sees on top of the picture is produced here as a single
ASS subtitle file, which FFmpeg burns in one pass (``ass=``).  Keeping it in
one file means one re-encode and predictable layout.

Design rules that come from the brief:

* captions never cover the important part of the screen - the caption band is
  chosen per chunk from the focus region of the shot it sits on;
* mobile-safe typography: big, bold, thick outline, high contrast;
* short chunks (<= 4 words) with the spoken word highlighted;
* word-level timing when a real alignment is available, estimated timing (by
  word length) otherwise - the caption still tracks the narration closely.

The v0.2 renderer (:mod:`contentforge.subtitles.ass_renderer`) is untouched and
still used by the classic pipeline.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from contentforge.log import get_logger
from contentforge.models.schemas import Region

log = get_logger("captions")

_ASS_TIME = "{:d}:{:02d}:{:05.2f}"


def _t(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return _ASS_TIME.format(h, m, s)


def _colour(hex_colour: str, alpha: str = "00") -> str:
    """``#RRGGBB`` -> ASS ``&HAABBGGRR``."""
    h = hex_colour.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha}{b}{g}{r}".upper()


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", " ")


@dataclass
class CaptionWord:
    text: str
    start: float
    end: float


@dataclass
class CaptionChunk:
    start: float
    end: float
    words: list[CaptionWord]
    band: str = "bottom"  # bottom | top | middle
    role: str = "demo"

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "text": self.text,
            "band": self.band,
            "role": self.role,
            "words": [
                {"text": w.text, "start": round(w.start, 3), "end": round(w.end, 3)} for w in self.words
            ],
        }


@dataclass
class OverlayStyle:
    width: int = 1080
    height: int = 1920
    font: str = "DejaVu Sans"
    font_size: int = 74
    outline: int = 7
    max_words: int = 4
    max_chars: int = 26
    primary: str = "#FFFFFF"
    highlight: str = "#FFB63D"
    outline_colour: str = "#000000"
    accent: str = "#FF7A00"
    margin_h: int = 70
    safe_bottom: int = 300  # Instagram UI sits at the very bottom
    safe_top: int = 240
    watermark: str = "StudentTools.pk"
    watermark_size: int = 34
    progress_bar: bool = True
    click_rings: bool = True
    hook_card: bool = True


@dataclass
class OverlayPlan:
    chunks: list[CaptionChunk] = field(default_factory=list)
    clicks: list[tuple[float, float, float]] = field(default_factory=list)  # t, x, y (output px)
    duration: float = 0.0
    hook_text: str = ""
    cta_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "duration": round(self.duration, 3),
            "hook_text": self.hook_text,
            "cta_text": self.cta_text,
            "chunks": [c.to_dict() for c in self.chunks],
            "clicks": [[round(t, 3), round(x), round(y)] for t, x, y in self.clicks],
        }

    def to_srt(self) -> str:
        out: list[str] = []
        for i, c in enumerate(self.chunks, start=1):
            out.append(str(i))
            out.append(f"{_srt_time(c.start)} --> {_srt_time(c.end)}")
            out.append(c.text)
            out.append("")
        return "\n".join(out)


def _srt_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# ----------------------------------------------------------------- metrics
_FONT_FILES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def _measure(text: str, size: int) -> float:
    """Rendered width of ``text`` in pixels (falls back to a rough estimate)."""
    try:
        from PIL import Image, ImageDraw, ImageFont

        for path in _FONT_FILES:
            if Path(path).exists():
                font = ImageFont.truetype(path, size)
                return float(ImageDraw.Draw(Image.new("RGB", (8, 8))).textlength(text, font=font))
    except Exception:  # pragma: no cover - PIL/font missing
        pass
    return 0.58 * size * len(text)


def wrap_lines(text: str, size: int, max_w: float) -> str:
    """Hard-wrap ``text`` into ASS lines (``\\N``) that fit ``max_w`` pixels."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        if current and _measure(trial, size) > max_w:
            lines.append(current)
            current = word
        else:
            current = trial
    if current:
        lines.append(current)
    return "\\N".join(lines[:3])


def fit_card(text: str, style: OverlayStyle, max_lines: int = 2) -> tuple[int, str]:
    """Font size + wrapped body for a hook/CTA card: never clipped, never 4 lines."""
    max_w = style.width - 2 * style.margin_h
    size = int(style.font_size * 1.18)
    while size > 46:
        body = wrap_lines(text, size, max_w)
        lines = body.split("\\N")
        if len(lines) <= max_lines and all(_measure(x, size) <= max_w for x in lines):
            return size, body
        size -= 4
    return size, wrap_lines(text, size, max_w)


def fit_font_size(text: str, style: OverlayStyle) -> int:
    """Largest font size at which ``text`` still fits the mobile-safe width.

    Captions are one line by design, so an unusually long word (a URL, say)
    shrinks that chunk instead of running off the screen.
    """
    max_w = style.width - 2 * style.margin_h
    size = style.font_size
    while size > 40 and _measure(text, size) > max_w:
        size -= 3
    return size


# --------------------------------------------------------------------- build
def chunk_segment(
    text: str,
    start: float,
    end: float,
    *,
    max_words: int = 4,
    max_chars: int = 26,
    words: Sequence[CaptionWord] | None = None,
) -> list[CaptionChunk]:
    """Split one narration line into short caption chunks with word timings.

    ``words`` carries real (Whisper) timings when available; otherwise timing is
    estimated from word length, which tracks speech closely enough for reading.
    """
    tokens = [w for w in re.split(r"\s+", text.strip()) if w]
    if not tokens:
        return []
    if words:
        timed = list(words)
    else:
        weights = [max(2.0, len(w)) for w in tokens]
        total = sum(weights)
        span = max(0.3, end - start)
        timed = []
        cursor = start
        for token, weight in zip(tokens, weights):
            dur = span * weight / total
            timed.append(CaptionWord(token, cursor, cursor + dur))
            cursor += dur
    chunks: list[CaptionChunk] = []
    current: list[CaptionWord] = []
    for word in timed:
        candidate = current + [word]
        too_long = len(candidate) > max_words or len(" ".join(w.text for w in candidate)) > max_chars
        if current and too_long:
            chunks.append(CaptionChunk(current[0].start, current[-1].end, current))
            current = [word]
        else:
            current = candidate
        if current and current[-1].text.endswith((".", "!", "?")):
            chunks.append(CaptionChunk(current[0].start, current[-1].end, current))
            current = []
    if current:
        chunks.append(CaptionChunk(current[0].start, current[-1].end, current))
    return chunks


def build_overlay_plan(
    narration_segments: Sequence[Any],
    *,
    duration: float,
    style: OverlayStyle | None = None,
    focus_regions: Sequence[tuple[float, float, Region]] | None = None,
    clicks: Sequence[tuple[float, float, float]] | None = None,
    hook_text: str = "",
    cta_text: str = "",
    word_timings: dict[int, list[CaptionWord]] | None = None,
) -> OverlayPlan:
    """Turn narration segments into a positioned, timed overlay plan.

    ``focus_regions`` are ``(start, end, region)`` in **output** coordinates
    (0-1 of the frame); a caption whose band would cover the focus region is
    moved to the other band.
    """
    st = style or OverlayStyle()
    plan = OverlayPlan(duration=duration, hook_text=hook_text, cta_text=cta_text)
    for i, seg in enumerate(narration_segments):
        text = getattr(seg, "text", "") or ""
        start = float(getattr(seg, "start", 0.0))
        end = float(getattr(seg, "end", start + 1.0))
        role = getattr(seg, "role", "demo")
        chunks = chunk_segment(
            text,
            start,
            end,
            max_words=st.max_words,
            max_chars=st.max_chars,
            words=(word_timings or {}).get(i),
        )
        for chunk in chunks:
            chunk.role = role
            chunk.band = _band_for(chunk, focus_regions or [], st)
            plan.chunks.append(chunk)
    plan.chunks.sort(key=lambda c: c.start)
    # the hook and CTA cards say the same thing in bigger type: do not double up
    if st.hook_card and hook_text:
        plan.chunks = [c for c in plan.chunks if (c.start + c.end) / 2 > 1.9]
    if cta_text and duration > 3:
        plan.chunks = [c for c in plan.chunks if (c.start + c.end) / 2 < duration - 1.9]
    # never let two chunks overlap: a reader can only follow one line
    for a, b in zip(plan.chunks, plan.chunks[1:]):
        if a.end > b.start:
            a.end = max(a.start + 0.25, b.start - 0.01)
    plan.clicks = [(float(t), float(x), float(y)) for t, x, y in (clicks or [])]
    return plan


def _band_for(chunk: CaptionChunk, focus: Sequence[tuple[float, float, Region]], st: OverlayStyle) -> str:
    """Bottom band unless the important content lives there."""
    mid = (chunk.start + chunk.end) / 2
    bottom = Region(0.0, 1.0 - (st.safe_bottom + 260) / st.height, 1.0, (st.safe_bottom + 260) / st.height)
    top = Region(0.0, st.safe_top / st.height * 0.4, 1.0, 260 / st.height)
    for s, e, region in focus:
        if not (s - 0.2 <= mid <= e + 0.2):
            continue
        if region.intersection_area(bottom) > 0.25 * max(1e-6, region.area):
            # important content is down there - go up, unless that is worse
            if region.intersection_area(top) > 0.25 * max(1e-6, region.area):
                return "bottom"
            return "top"
    return "bottom"


# -------------------------------------------------------------------- render
class ReelOverlayRenderer:
    """Writes the ASS file and burns it into the video."""

    def __init__(self, style: OverlayStyle | None = None, ffmpeg: Any | None = None):
        self.st = style or OverlayStyle()
        from contentforge.utils.ffmpeg import FFmpeg  # local import keeps module import cheap

        self.ff = ffmpeg or FFmpeg()

    # ------------------------------------------------------------------ ass
    def header(self) -> str:
        st = self.st
        return "\n".join(
            [
                "[Script Info]",
                "ScriptType: v4.00+",
                f"PlayResX: {st.width}",
                f"PlayResY: {st.height}",
                "WrapStyle: 2",
                "ScaledBorderAndShadow: yes",
                "YCbCr Matrix: TV.709",
                "",
                "[V4+ Styles]",
                "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
                "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
                "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
                f"Style: Caption,{st.font},{st.font_size},{_colour(st.primary)},{_colour(st.highlight)},"
                f"{_colour(st.outline_colour)},{_colour('#000000', '80')},-1,0,0,0,100,100,1.5,0,1,"
                f"{st.outline},2,5,{st.margin_h},{st.margin_h},0,1",
                f"Style: Card,{st.font},{int(st.font_size * 1.18)},{_colour(st.primary)},"
                f"{_colour(st.highlight)},{_colour(st.outline_colour)},{_colour('#000000', '80')},-1,0,0,0,"
                f"100,100,2,0,1,{st.outline + 1},2,5,{st.margin_h},{st.margin_h},0,1",
                f"Style: Mark,{st.font},{st.watermark_size},{_colour(st.primary, '40')},"
                f"{_colour(st.primary, '40')},{_colour(st.outline_colour, '60')},{_colour('#000000', 'A0')},"
                "-1,0,0,0,100,100,0,0,1,2,0,5,20,20,0,1",
                f"Style: Shape,{st.font},40,{_colour(st.accent)},{_colour(st.accent)},"
                f"{_colour(st.accent)},{_colour(st.accent)},0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1",
                "",
                "[Events]",
                "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
            ]
        )

    def build_ass(self, plan: OverlayPlan) -> str:
        st = self.st
        lines = [self.header()]
        add = lines.append

        # --- captions with the spoken word highlighted
        for chunk in plan.chunks:
            y = self._band_y(chunk.band)
            x = st.width // 2
            size = fit_font_size(chunk.text.upper(), st)
            resize = "" if size == st.font_size else f"\\fs{size}"
            for i, word in enumerate(chunk.words):
                start = word.start if i else chunk.start
                end = word.end if i < len(chunk.words) - 1 else chunk.end
                if end <= start:
                    continue
                parts = []
                for j, w in enumerate(chunk.words):
                    if j == i:
                        parts.append(
                            "{\\c" + _colour(st.highlight) + "\\fscx108\\fscy108}"
                            + _escape(w.text.upper())
                            + "{\\c" + _colour(st.primary) + "\\fscx100\\fscy100}"
                        )
                    else:
                        parts.append(_escape(w.text.upper()))
                body = " ".join(parts)
                pos = "{\\pos(" + f"{x},{y}" + ")\\an5\\fad(60,60)" + resize + "}"
                add(f"Dialogue: 1,{_t(start)},{_t(end)},Caption,,0,0,0,,{pos}{body}")

        # --- hook / CTA cards
        if st.hook_card and plan.hook_text:
            size, body = fit_card(_escape(plan.hook_text.upper()), st)
            add(
                f"Dialogue: 2,{_t(0.05)},{_t(1.85)},Card,,0,0,0,,"
                + "{\\pos(" + f"{st.width // 2},{int(st.height * 0.16)}" + f")\\an5\\fs{size}\\fad(120,160)}}"
                + body
            )
        if plan.cta_text and plan.duration > 3:
            add(
                f"Dialogue: 2,{_t(max(0.0, plan.duration - 1.9))},{_t(plan.duration)},Card,,0,0,0,,"
                + "{\\pos(" + f"{st.width // 2},{int(st.height * 0.845)}"
                + f")\\an5\\fs{fit_card(_escape(plan.cta_text.upper()), st)[0]}\\fad(160,120)}}"
                + fit_card(_escape(plan.cta_text.upper()), st)[1]
            )

        # --- click rings
        if st.click_rings:
            for t, x, y in plan.clicks:
                add(self._ring(t, x, y))

        # --- watermark + progress bar
        if st.watermark:
            add(
                f"Dialogue: 0,{_t(0.0)},{_t(max(0.1, plan.duration))},Mark,,0,0,0,,"
                + "{\\pos(" + f"{st.width // 2},{int(st.height * 0.955)}" + ")\\an5}"
                + _escape(st.watermark)
            )
        if st.progress_bar and plan.duration > 1:
            add(self._progress(plan.duration))
        return "\n".join(lines) + "\n"

    def _band_y(self, band: str) -> int:
        st = self.st
        if band == "top":
            return int(st.safe_top + st.font_size * 0.9)
        if band == "middle":
            return int(st.height * 0.5)
        return int(st.height - st.safe_bottom - st.font_size * 0.5)

    def _ring(self, t: float, x: float, y: float) -> str:
        st = self.st
        r = 70
        draw = f"m -{r} 0 b -{r} -{r} {r} -{r} {r} 0 b {r} {r} -{r} {r} -{r} 0"
        start, end = max(0.0, t - 0.05), t + 0.55
        tag = (
            "{\\an7\\pos(" + f"{int(x - 0)},{int(y - 0)}" + ")\\p1\\bord4\\shad0"
            f"\\1a&HFF&\\3c{_colour(st.accent)}\\alpha&H30&"
            "\\t(0,550,\\fscx210\\fscy210\\alpha&HFF&)}"
        )
        return f"Dialogue: 3,{_t(start)},{_t(end)},Shape,,0,0,0,,{tag}{draw}{{\\p0}}"

    def _progress(self, duration: float) -> str:
        st = self.st
        h = 8
        y = st.height - 10
        draw = f"m 0 0 l {st.width} 0 l {st.width} {h} l 0 {h}"
        tag = (
            "{\\an7\\pos(0," + str(y) + ")\\p1\\bord0\\shad0\\1c" + _colour(st.accent) + "\\alpha&H30&"
            "\\fscx0\\t(0," + str(int(duration * 1000)) + ",\\fscx100)}"
        )
        return f"Dialogue: 0,{_t(0.0)},{_t(duration)},Shape,,0,0,0,,{tag}{draw}{{\\p0}}"

    # --------------------------------------------------------------- burn-in
    def render(
        self,
        src: Path,
        plan: OverlayPlan,
        dst: Path,
        *,
        ass_path: Path | None = None,
        audio: Path | None = None,
        crf: int = 20,
        preset: str = "medium",
        fonts_dir: str | None = None,
    ) -> Path:
        ass_path = ass_path or dst.with_suffix(".ass")
        ass_path.parent.mkdir(parents=True, exist_ok=True)
        ass_path.write_text(self.build_ass(plan), encoding="utf-8")
        filter_arg = f"ass={_ff_escape(str(ass_path))}"
        if fonts_dir:
            filter_arg += f":fontsdir={_ff_escape(fonts_dir)}"
        args = ["-i", str(src)]
        if audio:
            args += ["-i", str(audio)]
        args += ["-vf", filter_arg, "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p"]
        if audio:
            args += ["-c:a", "aac", "-b:a", "192k", "-map", "0:v:0", "-map", "1:a:0", "-shortest"]
        else:
            args += ["-an"]
        args += [str(dst)]
        self.ff.run(args, timeout=3600)
        log.info("Burned %d caption chunks into %s", len(plan.chunks), dst.name)
        return dst


def _ff_escape(path: str) -> str:
    return path.replace("\\", "/").replace(":", r"\:").replace("'", r"\'")

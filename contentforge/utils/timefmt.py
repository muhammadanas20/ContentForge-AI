"""Timestamp and colour formatting helpers for subtitles and overlays."""

from __future__ import annotations


def format_srt_time(seconds: float) -> str:
    """``12.345`` -> ``00:00:12,345``."""
    seconds = max(0.0, seconds)
    ms = int(round((seconds - int(seconds)) * 1000))
    s = int(seconds)
    if ms == 1000:
        s += 1
        ms = 0
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def format_ass_time(seconds: float) -> str:
    """``12.345`` -> ``0:00:12.35`` (ASS uses centiseconds)."""
    seconds = max(0.0, seconds)
    cs = int(round((seconds - int(seconds)) * 100))
    s = int(seconds)
    if cs == 100:
        s += 1
        cs = 0
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def seconds_to_clock(seconds: float) -> str:
    """``75.2`` -> ``01:15``."""
    s = int(round(seconds))
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def hex_to_rgb(color: str) -> tuple[int, int, int]:
    """``#FF7A00`` -> ``(255, 122, 0)``. Accepts 3/6/8-digit hex (alpha ignored)."""
    c = color.strip().lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    if len(c) not in (6, 8):
        raise ValueError(f"Invalid hex colour: {color}")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def hex_to_rgba(color: str, default_alpha: int = 255) -> tuple[int, int, int, int]:
    c = color.strip().lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    r, g, b = hex_to_rgb("#" + c[:6])
    a = int(c[6:8], 16) if len(c) == 8 else default_alpha
    return r, g, b, a


def hex_to_ass_color(color: str, alpha: int = 0) -> str:
    """Convert ``#RRGGBB[AA]`` to ASS ``&HAABBGGRR`` (ASS alpha: 0 = opaque)."""
    r, g, b, a = hex_to_rgba(color, 255)
    if len(color.strip().lstrip("#")) == 8:
        alpha = 255 - a
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"

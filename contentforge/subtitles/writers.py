"""Plain subtitle file writers (SRT / JSON / TXT) for the upload package."""

from __future__ import annotations

import json
from pathlib import Path

from contentforge.subtitles.captions import Caption
from contentforge.utils import atomic_write_text, format_srt_time


def write_srt(captions: list[Caption], path: Path) -> Path:
    blocks = []
    for i, c in enumerate(captions, 1):
        blocks.append(f"{i}\n{format_srt_time(c.start)} --> {format_srt_time(c.end)}\n{c.text}\n")
    return atomic_write_text(path, "\n".join(blocks))


def write_json(captions: list[Caption], path: Path) -> Path:
    return atomic_write_text(
        path, json.dumps([c.to_dict() for c in captions], indent=2, ensure_ascii=False)
    )


def write_txt(captions: list[Caption], path: Path) -> Path:
    return atomic_write_text(path, "\n".join(c.text for c in captions) + "\n")


WRITERS = {"srt": write_srt, "json": write_json, "txt": write_txt}


def write_all(captions: list[Caption], base: Path, formats: list[str]) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for fmt in formats:
        writer = WRITERS.get(fmt)
        if writer:
            out[fmt] = writer(captions, base.with_suffix(f".{fmt}"))
    return out

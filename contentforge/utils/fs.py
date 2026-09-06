"""Filesystem helpers with safety in mind (atomic writes, guarded deletes)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import unicodedata
from pathlib import Path
from typing import Any


def slugify(text: str, max_length: int = 60) -> str:
    """Convert arbitrary text into a filesystem/URL-safe slug."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    text = re.sub(r"[\s_-]+", "-", text).strip("-")
    return text[:max_length].rstrip("-") or "untitled"


def file_sha1(path: str | Path, chunk_size: int = 1 << 20, max_bytes: int | None = None) -> str:
    """SHA-1 of a file (optionally only the first ``max_bytes``, for speed on large media)."""
    h = hashlib.sha1()
    read = 0
    with Path(path).open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
            read += len(chunk)
            if max_bytes is not None and read >= max_bytes:
                break
    return h.hexdigest()


def atomic_write_text(path: str | Path, text: str, encoding: str = "utf-8") -> Path:
    """Write text via a temp file + rename so readers never see partial content."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding=encoding)
    os.replace(tmp, p)
    return p


def atomic_write_json(path: str | Path, data: Any, indent: int = 2) -> Path:
    return atomic_write_text(path, json.dumps(data, indent=indent, ensure_ascii=False, default=str))


def read_json(path: str | Path, default: Any = None) -> Any:
    p = Path(path)
    if not p.exists():
        return default
    with p.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def wait_until_stable(
    path: str | Path, stable_seconds: float = 5, poll: float = 1, timeout: float = 3600
) -> bool:
    """Block until a file's size stops changing (i.e. the recorder finished writing).

    Returns ``True`` when stable, ``False`` on timeout or if the file vanished.
    """
    p = Path(path)
    deadline = time.monotonic() + timeout
    last_size = -1
    stable_since = time.monotonic()
    while time.monotonic() < deadline:
        if not p.exists():
            return False
        size = p.stat().st_size
        now = time.monotonic()
        if size != last_size:
            last_size = size
            stable_since = now
        elif size > 0 and now - stable_since >= stable_seconds:
            return True
        time.sleep(poll)
    return False


def dir_size_bytes(path: str | Path) -> int:
    total = 0
    p = Path(path)
    if not p.exists():
        return 0
    for f in p.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            continue
    return total


def disk_free_gb(path: str | Path) -> float:
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    usage = shutil.disk_usage(p)
    return usage.free / (1024**3)


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024:
            return f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} PB"


def safe_rmtree(path: str | Path, must_be_under: str | Path) -> bool:
    """Delete ``path`` only if it is inside ``must_be_under``. Returns True if deleted."""
    p = Path(path).resolve()
    guard = Path(must_be_under).resolve()
    if p == guard or guard not in p.parents:
        return False
    if p.is_dir():
        shutil.rmtree(p, ignore_errors=True)
    elif p.exists():
        p.unlink()
    return True

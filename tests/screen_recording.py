"""Synthetic but realistic "screen recording" generator used by cursor-crop tests.

Draws a browser-like tutorial page (top bar, sidebar, cards, body text) with a
real arrow-shaped mouse cursor that moves between targets, pauses, clicks
(button highlight), while a text caret blinks and the page scrolls once.
Frames are written through OpenCV's VideoWriter (mp4v) and re-encoded to H.264
with ffmpeg so the result behaves like a real OBS/GNOME recording.

The ground-truth cursor path is returned so tests can assert on tracking
accuracy rather than on "it did not crash".
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from contentforge.utils.ffmpeg import FFmpeg

WHITE = (255, 255, 255)


@dataclass
class Recording:
    path: Path
    width: int
    height: int
    fps: int
    duration: float
    cursor: list[tuple[float, float, float]]  # (t, x_px, y_px) per frame

    def cursor_at(self, t: float) -> tuple[float, float]:
        i = min(len(self.cursor) - 1, max(0, int(round(t * self.fps))))
        return self.cursor[i][1], self.cursor[i][2]


def _draw_page(w: int, h: int, scroll: int, caret_on: bool, clicked: int | None) -> np.ndarray:
    img = np.full((h, w, 3), (246, 247, 249), np.uint8)  # light grey (BGR)
    # top bar
    cv2.rectangle(img, (0, 0), (w, 64), (36, 30, 26), -1)
    cv2.rectangle(img, (220, 16), (w - 220, 48), (70, 66, 60), -1)
    cv2.putText(
        img,
        "studenttools.pk/pdf-to-word",
        (236, 40),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (220, 220, 220),
        2,
    )
    # sidebar
    cv2.rectangle(img, (0, 64), (260, h), (232, 234, 238), -1)
    for i in range(9):
        y = 100 + i * 52 - scroll // 3
        if 70 < y < h:
            cv2.rectangle(img, (24, y), (236, y + 30), (200, 204, 210), -1)
            cv2.putText(
                img, f"Tool {i + 1}", (36, y + 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (60, 60, 60), 1
            )
    # main content: heading, cards, paragraph lines
    y0 = 110 - scroll
    cv2.putText(
        img,
        "Convert PDF to Word in seconds",
        (300, y0),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.1,
        (30, 30, 30),
        2,
    )
    cards = [(300, y0 + 40), (640, y0 + 40), (980, y0 + 40)]
    for i, (cx, cy) in enumerate(cards):
        col = (0, 122, 255) if clicked == i else (255, 255, 255)
        cv2.rectangle(img, (cx, cy), (cx + 300, cy + 170), col, -1)
        cv2.rectangle(img, (cx, cy), (cx + 300, cy + 170), (190, 190, 190), 2)
        cv2.putText(
            img,
            ["Upload", "Choose format", "Download"][i],
            (cx + 20, cy + 40),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (20, 20, 20),
            2,
        )
        cv2.rectangle(img, (cx + 20, cy + 110), (cx + 180, cy + 150), (0, 122, 255), -1)
        cv2.putText(img, "Start", (cx + 60, cy + 138), cv2.FONT_HERSHEY_SIMPLEX, 0.7, WHITE, 2)
    for li in range(14):
        y = y0 + 260 + li * 30
        if 64 < y < h:
            cv2.rectangle(img, (300, y), (300 + 620 + (li * 37) % 260, y + 12), (205, 208, 214), -1)
    # text field + caret
    cv2.rectangle(img, (300, h - 90), (900, h - 40), WHITE, -1)
    cv2.rectangle(img, (300, h - 90), (900, h - 40), (150, 150, 150), 2)
    cv2.putText(img, "myfile.pdf", (312, h - 55), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (40, 40, 40), 2)
    if caret_on:
        cv2.line(img, (470, h - 82), (470, h - 48), (0, 0, 0), 2)
    return img


def _draw_cursor(img: np.ndarray, x: float, y: float, size: int = 22) -> None:
    """Classic white arrow with a black outline (like GNOME/Windows default)."""
    xi, yi = int(x), int(y)
    pts = np.array(
        [
            (0, 0),
            (0, int(size * 1.35)),
            (int(size * 0.32), int(size * 1.05)),
            (int(size * 0.55), int(size * 1.5)),
            (int(size * 0.72), int(size * 1.42)),
            (int(size * 0.5), int(size * 0.98)),
            (int(size * 0.9), int(size * 0.98)),
        ],
        np.int32,
    )
    pts = pts + np.array([xi, yi])
    cv2.fillPoly(img, [pts], WHITE)
    cv2.polylines(img, [pts], True, (0, 0, 0), 2, cv2.LINE_AA)


def _ease(a: float, b: float, u: float) -> float:
    u = 0.5 - 0.5 * np.cos(np.pi * min(1.0, max(0.0, u)))
    return a + (b - a) * u


def make_screen_recording(
    out: Path,
    *,
    width: int = 1280,
    height: int = 720,
    fps: int = 30,
    duration: float = 12.0,
    with_audio: bool = True,
    ffmpeg: FFmpeg | None = None,
) -> Recording:
    """Write a synthetic tutorial recording and return the ground-truth cursor path."""
    ff = ffmpeg or FFmpeg()
    raw = out.with_suffix(".raw.mp4")
    writer = cv2.VideoWriter(str(raw), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
    n = int(duration * fps)

    # Waypoints (t, x, y): start left, move to cards on the right, click, type, scroll, come back.
    sx, sy = width / 1280, height / 720
    way = [
        (0.0, 120, 300),
        (1.2, 120, 300),
        (2.4, 380, 250),  # card 1
        (3.2, 380, 250),
        (3.4, 380, 250),  # click
        (4.6, 1080, 260),  # card 3 (far right)
        (5.6, 1080, 260),
        (6.6, 700, 660),  # text field
        (8.0, 700, 660),  # typing pause (caret blinking)
        (8.6, 700, 660),  # scroll burst happens 8.0..8.6
        (10.0, 330, 180),  # heading (left)
        (11.0, 330, 180),
        (duration, 330, 180),
    ]
    way = [(t, x * sx, y * sy) for t, x, y in way]
    truth: list[tuple[float, float, float]] = []
    for i in range(n):
        t = i / fps
        # locate segment
        for (t0, x0, y0), (t1, x1, y1) in zip(way, way[1:]):
            if t0 <= t <= t1:
                u = (t - t0) / (t1 - t0) if t1 > t0 else 1.0
                x, y = _ease(x0, x1, u), _ease(y0, y1, u)
                break
        else:
            x, y = way[-1][1], way[-1][2]
        scroll = 0
        if 8.0 <= t < 8.6:
            scroll = int(180 * (t - 8.0) / 0.6)
        elif t >= 8.6:
            scroll = 180
        caret_on = 6.6 <= t < 8.6 and int(t * 2) % 2 == 0
        clicked = 0 if 3.2 <= t < 3.45 else None
        frame = _draw_page(width, height, scroll, caret_on, clicked)
        _draw_cursor(frame, x, y, size=int(22 * sx))
        writer.write(frame)
        truth.append((t, x, y))
    writer.release()

    args = ["-i", str(raw)]
    if with_audio:
        # speech-like tone with a 1.5 s silence gap so silence removal has something to cut
        args += [
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=330:sample_rate=44100:duration={duration}",
            "-af",
            "volume=enable='between(t,5.0,6.5)':volume=0",
            "-c:a",
            "aac",
        ]
    args += [
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-shortest",
        str(out),
    ]
    ff.run(args)
    raw.unlink(missing_ok=True)
    return Recording(path=out, width=width, height=height, fps=fps, duration=duration, cursor=truth)

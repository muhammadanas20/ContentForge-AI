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


# =========================================================================== v0.3
# A richer, text-heavy synthetic recording used by the video-understanding,
# framing, editor, caption and cover tests.  It behaves like a real laptop
# capture of a student-offers website: browser chrome, a headline, offer cards,
# a form, clicks with visible button states, a scroll, typing and a final
# result reveal followed by dead time.
# ---------------------------------------------------------------------------
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def _font(size: int, bold: bool = False):
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            if bold and "Bold" not in path:
                continue
            try:
                return ImageFont.truetype(path, size)
            except OSError:  # pragma: no cover
                continue
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:  # pragma: no cover
                continue
    return ImageFont.load_default()  # pragma: no cover


@dataclass
class TutorialRecording:
    """A synthetic recording plus the ground truth tests assert against."""

    path: Path
    width: int
    height: int
    fps: int
    duration: float
    cursor: list[tuple[float, float, float]]
    clicks: list[tuple[float, float, float]]  # (t, x_px, y_px)
    scroll: tuple[float, float]
    typing: tuple[float, float]
    reveal: float
    idle: tuple[float, float]
    result_box: tuple[int, int, int, int]  # x, y, w, h in source px
    headline: str
    website: str

    def cursor_at(self, t: float) -> tuple[float, float]:
        i = min(len(self.cursor) - 1, max(0, int(round(t * self.fps))))
        return self.cursor[i][1], self.cursor[i][2]


class _SitePainter:
    """Draws one frame of a fictional student-offers website."""

    BG = (247, 248, 250)
    INK = (24, 26, 32)
    MUTED = (110, 116, 128)
    ACCENT = (255, 122, 0)
    CARD = (255, 255, 255)
    LINE = (223, 226, 232)

    def __init__(self, width: int, height: int, website: str, headline: str):
        self.w, self.h = width, height
        self.website = website
        self.headline = headline
        self.s = width / 1280.0
        self.f_title = _font(int(38 * self.s), bold=True)
        self.f_h2 = _font(int(26 * self.s), bold=True)
        self.f_body = _font(int(19 * self.s))
        self.f_small = _font(int(16 * self.s))
        self.f_btn = _font(int(20 * self.s), bold=True)
        self.f_code = _font(int(44 * self.s), bold=True)

    # -- geometry helpers ----------------------------------------------------
    def sx(self, v: float) -> int:
        return int(v * self.s)

    def card_boxes(self, scroll: int) -> list[tuple[int, int, int, int]]:
        top = self.sx(250) - scroll
        out = []
        for i in range(3):
            x = self.sx(90 + i * 370)
            out.append((x, top, self.sx(330), self.sx(230)))
        return out

    def claim_button(self, scroll: int) -> tuple[int, int, int, int]:
        x, y, w, h = self.card_boxes(scroll)[1]
        return (x + self.sx(24), y + h - self.sx(64), self.sx(180), self.sx(46))

    def email_box(self, scroll: int) -> tuple[int, int, int, int]:
        return (self.sx(90), self.sx(560) - scroll, self.sx(430), self.sx(52))

    def submit_button(self, scroll: int) -> tuple[int, int, int, int]:
        return (self.sx(540), self.sx(560) - scroll, self.sx(210), self.sx(52))

    def result_box(self, scroll: int) -> tuple[int, int, int, int]:
        return (self.sx(90), self.sx(650) - scroll, self.sx(660), self.sx(150))

    # -- painting ------------------------------------------------------------
    def frame(
        self,
        *,
        scroll: int = 0,
        typed: str = "",
        caret: bool = False,
        pressed: str = "",
        result: bool = False,
        page: str = "offers",
    ) -> np.ndarray:
        img = Image.new("RGB", (self.w, self.h), self.BG)
        d = ImageDraw.Draw(img)
        s = self.s

        # ---- page header
        y0 = self.sx(110) - scroll
        d.text((self.sx(90), y0), self.headline, font=self.f_title, fill=self.INK)
        d.text(
            (self.sx(90), y0 + self.sx(52)),
            "Verified student discounts, free software and exam resources in one place.",
            font=self.f_body,
            fill=self.MUTED,
        )

        # ---- offer cards
        titles = ("Free Design Suite", "50% Off Courses", "Free Cloud Storage")
        details = (
            ("Adobe-style editor", "Valid for .edu email", "No credit card"),
            ("Coursera & Udemy", "Yearly student plan", "Instant activation"),
            ("200 GB for 1 year", "Backup assignments", "Renewable"),
        )
        for i, (x, y, w, h) in enumerate(self.card_boxes(scroll)):
            if y > self.h or y + h < self.sx(78):
                continue
            d.rounded_rectangle([x, y, x + w, y + h], radius=int(14 * s), fill=self.CARD, outline=self.LINE, width=2)
            d.text((x + self.sx(20), y + self.sx(18)), titles[i], font=self.f_h2, fill=self.INK)
            for j, line in enumerate(details[i]):
                d.text(
                    (x + self.sx(20), y + self.sx(62) + j * self.sx(26)),
                    f"- {line}",
                    font=self.f_body,
                    fill=self.MUTED,
                )
            bx, by, bw, bh = (x + self.sx(24), y + h - self.sx(64), self.sx(180), self.sx(46))
            is_pressed = pressed == "claim" and i == 1
            d.rounded_rectangle(
                [bx, by, bx + bw, by + bh],
                radius=int(10 * s),
                fill=(214, 96, 0) if is_pressed else self.ACCENT,
            )
            d.text((bx + self.sx(34), by + self.sx(12)), "Claim offer", font=self.f_btn, fill=(255, 255, 255))

        # ---- form
        ex, ey, ew, eh = self.email_box(scroll)
        if -eh < ey < self.h:
            d.text((ex, ey - self.sx(30)), "Student email", font=self.f_small, fill=self.MUTED)
            d.rounded_rectangle([ex, ey, ex + ew, ey + eh], radius=int(10 * s), fill=(255, 255, 255), outline=self.LINE, width=2)
            text = typed or "you@university.edu.pk"
            d.text((ex + self.sx(14), ey + self.sx(14)), text, font=self.f_body, fill=self.INK if typed else (170, 175, 185))
            if caret:
                tw = d.textlength(text, font=self.f_body) if typed else 0
                cx = ex + self.sx(16) + int(tw)
                d.line([cx, ey + self.sx(10), cx, ey + eh - self.sx(10)], fill=self.INK, width=2)
            sx_, sy_, sw_, sh_ = self.submit_button(scroll)
            d.rounded_rectangle(
                [sx_, sy_, sx_ + sw_, sy_ + sh_],
                radius=int(10 * s),
                fill=(20, 100, 60) if pressed == "submit" else (26, 138, 82),
            )
            d.text((sx_ + self.sx(34), sy_ + self.sx(14)), "Get my code", font=self.f_btn, fill=(255, 255, 255))

        # ---- result panel (the payoff)
        if result:
            rx, ry, rw, rh = self.result_box(scroll)
            d.rounded_rectangle([rx, ry, rx + rw, ry + rh], radius=int(14 * s), fill=(232, 248, 238), outline=(26, 138, 82), width=3)
            d.text((rx + self.sx(24), ry + self.sx(18)), "Your student code is ready", font=self.f_h2, fill=(20, 90, 55))
            d.text((rx + self.sx(24), ry + self.sx(60)), "SOC-2025-FREE", font=self.f_code, fill=(16, 70, 44))

        # ---- footer text block (extra page content, gives OCR plenty to see)
        fy = self.sx(830) - scroll
        for i in range(6):
            y = fy + i * self.sx(28)
            if self.sx(78) < y < self.h:
                d.rectangle([self.sx(90), y, self.sx(90 + 520 + (i * 61) % 300), y + self.sx(12)], fill=(215, 218, 226))
        # ---- browser chrome
        d.rectangle([0, 0, self.w, self.sx(78)], fill=(38, 40, 48))
        for i, col in enumerate(((255, 95, 86), (255, 189, 46), (39, 201, 63))):
            d.ellipse(
                [self.sx(18 + i * 26), self.sx(20), self.sx(32 + i * 26), self.sx(34)], fill=col
            )
        d.rounded_rectangle(
            [self.sx(130), self.sx(14), self.w - self.sx(130), self.sx(50)],
            radius=int(18 * s),
            fill=(60, 63, 74),
        )
        d.text(
            (self.sx(150), self.sx(22)), f"https://{self.website}/student-offers", font=self.f_small, fill=(225, 228, 235)
        )
        for i, tab in enumerate(("Home", "Offers", "Verify", "FAQ")):
            d.text(
                (self.sx(140 + i * 110), self.sx(56)),
                tab,
                font=self.f_small,
                fill=(235, 235, 240) if tab.lower() == page else (150, 155, 168),
            )

        return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def make_tutorial_recording(
    out: Path,
    *,
    width: int = 1280,
    height: int = 720,
    fps: int = 24,
    duration: float = 24.0,
    website: str = "studentofferco.com",
    headline: str = "Student offers you can actually claim",
    with_audio: bool = True,
    ffmpeg: FFmpeg | None = None,
) -> TutorialRecording:
    """Render a realistic student-offers screen recording with known ground truth."""
    ff = ffmpeg or FFmpeg()
    painter = _SitePainter(width, height, website, headline)
    raw = out.with_suffix(".raw.mp4")
    writer = cv2.VideoWriter(str(raw), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
    n = int(duration * fps)
    scale = duration / 24.0  # the script below is written for a 24 s clip

    def T(t: float) -> float:
        return t * scale

    scrolled = painter.sx(150)  # page offset after the scroll (see below)
    claim = painter.claim_button(0)
    submit = painter.submit_button(scrolled)
    email = painter.email_box(scrolled)
    click_targets = [
        (T(3.3), painter.sx(360), painter.sx(56), "nav"),  # nav "Offers"
        (T(6.2), claim[0] + claim[2] // 2, claim[1] + claim[3] // 2, "claim"),  # claim offer
        (T(14.4), submit[0] + submit[2] // 2, submit[1] + submit[3] // 2, "submit"),  # get code
    ]
    way = [
        (T(0.0), painter.sx(200), painter.sx(300)),
        (T(1.0), painter.sx(200), painter.sx(300)),
        (T(3.0), click_targets[0][1], click_targets[0][2]),
        (T(3.8), click_targets[0][1], click_targets[0][2]),
        (T(5.6), click_targets[1][1], click_targets[1][2]),
        (T(7.0), click_targets[1][1], click_targets[1][2]),
        (T(8.2), painter.sx(700), painter.sx(420)),
        (T(11.0), painter.sx(700), painter.sx(420)),
        (T(11.6), email[0] + painter.sx(120), email[1] + painter.sx(26)),
        (T(14.0), email[0] + painter.sx(120), email[1] + painter.sx(26)),
        (T(14.3), click_targets[2][1], click_targets[2][2]),
        (T(16.0), click_targets[2][1], click_targets[2][2]),
        (T(17.5), painter.sx(360), painter.sx(690)),
        (T(24.0), painter.sx(360), painter.sx(690)),
    ]
    typed_full = "ayesha@uni.edu.pk"
    truth: list[tuple[float, float, float]] = []
    for i in range(n):
        t = i / fps
        for (t0, x0, y0), (t1, x1, y1) in zip(way, way[1:]):
            if t0 <= t <= t1:
                u = (t - t0) / (t1 - t0) if t1 > t0 else 1.0
                x, y = _ease(x0, x1, u), _ease(y0, y1, u)
                break
        else:
            x, y = way[-1][1], way[-1][2]

        scroll = 0
        if T(8.5) <= t < T(10.5):
            scroll = int(painter.sx(150) * (t - T(8.5)) / (T(10.5) - T(8.5)))
        elif t >= T(10.5):
            scroll = painter.sx(150)
        page = "home" if t < T(3.4) else "offers"
        typed = ""
        caret = False
        if T(11.6) <= t < T(14.2):
            k = int(len(typed_full) * min(1.0, (t - T(11.6)) / (T(13.9) - T(11.6))))
            typed = typed_full[:k]
            caret = int(t * 3) % 2 == 0
        elif t >= T(14.2):
            typed = typed_full
        pressed = ""
        for ct, _cx, _cy, name in click_targets:
            if ct <= t < ct + T(0.35):
                pressed = name
        result = t >= T(15.0)
        frame = painter.frame(
            scroll=scroll, typed=typed, caret=caret, pressed=pressed, result=result, page=page
        )
        _draw_cursor(frame, x, y, size=int(22 * painter.s))
        writer.write(frame)
        truth.append((t, x, y))
    writer.release()

    args = ["-i", str(raw)]
    if with_audio:
        args += [
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=290:sample_rate=44100:duration={duration}",
            "-af",
            f"volume=enable='between(t,{T(18.2)},{duration})':volume=0,"
            f"volume=enable='between(t,{T(9.0)},{T(10.6)})':volume=0",
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
    rb = painter.result_box(painter.sx(150))
    return TutorialRecording(
        path=out,
        width=width,
        height=height,
        fps=fps,
        duration=duration,
        cursor=truth,
        clicks=[(ct, cx, cy) for ct, cx, cy, _name in click_targets],
        scroll=(T(8.5), T(10.5)),
        typing=(T(11.6), T(14.2)),
        reveal=T(15.0),
        idle=(T(18.2), duration),
        result_box=rb,
        headline=headline,
        website=website,
    )

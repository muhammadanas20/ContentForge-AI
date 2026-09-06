"""Lightweight, pluggable text detection ("OCR") for screen recordings.

The framing planner, the script planner and the quality gates all need to know
*where the text is* and, when possible, *what it says*.  Running a real OCR
engine on every frame of a 1080p recording would make an 8 GB laptop unusable,
so this module is built around three ideas:

1. **Backends are pluggable.**  :class:`TesseractOCR` is used when
   ``pytesseract`` and the ``tesseract`` binary are installed (real strings +
   boxes).  Otherwise :class:`HeuristicTextDetector` - pure OpenCV morphology -
   still returns accurate *bounding boxes* of text-like content, which is all
   the composition and quality logic strictly needs.
2. **Everything is normalised.**  Boxes come back as
   :class:`~contentforge.models.schemas.TextBox` in 0..1 frame coordinates.
3. **Sampling is configurable.**  Callers decide how often OCR runs
   (``ocr.every_seconds``); the heuristic pass is cheap enough for every
   sampled frame.
"""

from __future__ import annotations

import os
import shutil
from typing import Protocol

import cv2
import numpy as np

from contentforge.log import get_logger
from contentforge.models.schemas import Region, TextBox

log = get_logger("ocr")


class OCRBackend(Protocol):
    """Anything that can turn a BGR frame into text boxes."""

    name: str

    def available(self) -> bool: ...

    def detect(self, frame: np.ndarray) -> list[TextBox]: ...


# --------------------------------------------------------------------------- heuristic
class HeuristicTextDetector:
    """Text-*region* detector: morphological gradient + line grouping.

    No recognition, no model, ~2 ms on a 960 px wide frame.  It finds the
    horizontal runs of high-frequency dark-on-light (or light-on-dark) content
    that make up UI labels, headings and paragraphs.
    """

    name = "heuristic"

    def __init__(
        self,
        min_height: float = 0.008,
        max_height: float = 0.22,
        min_width: float = 0.010,
        min_fill: float = 0.12,
        max_boxes: int = 120,
    ):
        self.min_height = min_height
        self.max_height = max_height
        self.min_width = min_width
        self.min_fill = min_fill
        self.max_boxes = max_boxes

    def available(self) -> bool:  # always
        return True

    def detect(self, frame: np.ndarray) -> list[TextBox]:
        if frame is None or frame.size == 0:
            return []
        h, w = frame.shape[:2]
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
        # Morphological gradient highlights glyph edges regardless of polarity.
        grad = cv2.morphologyEx(gray, cv2.MORPH_GRADIENT, cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2)))
        _, bw = cv2.threshold(grad, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
        # Join glyphs of one line into a blob (wide, short kernel).
        kx = max(6, int(w * 0.012))
        connected = cv2.morphologyEx(
            bw, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (kx, 2))
        )
        contours, _ = cv2.findContours(connected, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        boxes: list[TextBox] = []
        for c in contours:
            x, y, bw_, bh_ = cv2.boundingRect(c)
            fh, fw = bh_ / h, bw_ / w
            if not (self.min_height <= fh <= self.max_height):
                continue
            if fw < self.min_width or fw > 0.99:
                continue
            if bw_ < bh_:  # vertical blobs are icons/scrollbars, not text lines
                continue
            roi = bw[y : y + bh_, x : x + bw_]
            fill = float(np.count_nonzero(roi)) / max(1, roi.size)
            if fill < self.min_fill or fill > 0.95:
                continue
            score = min(1.0, fill * 2.0)
            boxes.append(
                TextBox(
                    region=Region.from_px(x, y, bw_, bh_, w, h, kind="text", score=score),
                    text="",
                    confidence=score,
                    source=self.name,
                )
            )
        boxes.sort(key=lambda b: -b.region.area)
        return boxes[: self.max_boxes]


# --------------------------------------------------------------------------- tesseract
class TesseractOCR:
    """Real OCR through ``pytesseract`` (optional dependency).

    Falls back to :class:`HeuristicTextDetector` transparently via
    :func:`get_ocr_backend` when the binary or the python wrapper is missing.
    """

    name = "tesseract"

    def __init__(self, lang: str = "eng", min_confidence: float = 45.0, psm: int = 11):
        self.lang = lang
        self.min_confidence = min_confidence
        self.psm = psm
        self._pytesseract = None

    def available(self) -> bool:
        try:
            import pytesseract  # type: ignore
        except Exception:
            return False
        binary = os.environ.get("TESSERACT_BINARY") or shutil.which("tesseract")
        if not binary:
            return False
        pytesseract.pytesseract.tesseract_cmd = binary
        self._pytesseract = pytesseract
        return True

    def detect(self, frame: np.ndarray) -> list[TextBox]:
        if self._pytesseract is None and not self.available():
            return []
        pt = self._pytesseract
        h, w = frame.shape[:2]
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
        try:
            data = pt.image_to_data(  # type: ignore[union-attr]
                gray,
                lang=self.lang,
                config=f"--psm {self.psm}",
                output_type=pt.Output.DICT,  # type: ignore[union-attr]
            )
        except Exception as exc:  # pragma: no cover - environment dependent
            log.warning("Tesseract failed (%s); using heuristic boxes for this frame", exc)
            return []
        boxes: list[TextBox] = []
        n = len(data.get("text", []))
        for i in range(n):
            text = (data["text"][i] or "").strip()
            try:
                conf = float(data["conf"][i])
            except (TypeError, ValueError):
                conf = -1.0
            if not text or conf < self.min_confidence:
                continue
            x, y, bw_, bh_ = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
            if bw_ <= 1 or bh_ <= 1:
                continue
            boxes.append(
                TextBox(
                    region=Region.from_px(x, y, bw_, bh_, w, h, kind="text", score=conf / 100.0),
                    text=text,
                    confidence=conf / 100.0,
                    source=self.name,
                )
            )
        return merge_text_lines(boxes)


# --------------------------------------------------------------------------- helpers
def merge_text_lines(boxes: list[TextBox], gap: float = 0.02, y_tol: float = 0.012) -> list[TextBox]:
    """Merge word boxes that sit on the same line into readable phrases."""
    if not boxes:
        return []
    ordered = sorted(boxes, key=lambda b: (round(b.region.cy / max(y_tol, 1e-6)), b.region.x))
    merged: list[TextBox] = []
    for b in ordered:
        if merged:
            prev = merged[-1]
            same_line = abs(prev.region.cy - b.region.cy) <= y_tol
            close = b.region.x - prev.region.x2 <= gap
            if same_line and close:
                region = prev.region.union(b.region)
                region.kind = "text"
                text = f"{prev.text} {b.text}".strip()
                merged[-1] = TextBox(
                    region=region,
                    text=text,
                    confidence=(prev.confidence + b.confidence) / 2,
                    source=prev.source,
                )
                continue
        merged.append(b)
    return merged


def detect_ui_regions(frame: np.ndarray, max_regions: int = 12) -> list[Region]:
    """Rectangular UI elements (buttons, cards, inputs) - cheap contour pass."""
    if frame is None or frame.size == 0:
        return []
    h, w = frame.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
    edges = cv2.Canny(cv2.GaussianBlur(gray, (3, 3), 0), 40, 140)
    edges = cv2.dilate(edges, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)), iterations=1)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out: list[Region] = []
    for c in contours:
        peri = cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, 0.02 * peri, True)
        if len(approx) < 4 or len(approx) > 6:
            continue
        x, y, bw_, bh_ = cv2.boundingRect(approx)
        if bw_ * bh_ < 0.001 * w * h or bw_ * bh_ > 0.6 * w * h:
            continue
        if bh_ > bw_ * 3:
            continue
        out.append(Region.from_px(x, y, bw_, bh_, w, h, kind="ui", score=0.6))
    out.sort(key=lambda r: -r.area)
    return out[:max_regions]


def text_mass(boxes: list[TextBox]) -> float:
    """Fraction of the frame covered by text (overlaps counted once, approximately)."""
    if not boxes:
        return 0.0
    return min(1.0, sum(b.region.area for b in boxes))


def get_ocr_backend(engine: str = "auto", *, lang: str = "eng", min_confidence: float = 45.0) -> OCRBackend:
    """Return the best available backend.

    ``engine``: ``auto`` (tesseract if installed, else heuristic), ``tesseract``
    (raises nothing - degrades with a warning) or ``heuristic``/``none``.
    """
    engine = (engine or "auto").lower()
    if engine in ("heuristic", "none", "off"):
        return HeuristicTextDetector()
    tess = TesseractOCR(lang=lang, min_confidence=min_confidence)
    if tess.available():
        log.info("OCR backend: tesseract (%s)", lang)
        return tess
    if engine == "tesseract":
        log.warning(
            "OCR backend 'tesseract' requested but pytesseract/tesseract is not installed - "
            "falling back to the heuristic text-region detector (boxes only, no strings)"
        )
    else:
        log.info("OCR backend: heuristic text-region detector (install tesseract for real text)")
    return HeuristicTextDetector()

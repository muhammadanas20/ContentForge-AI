"""Cover generation: pick the strongest frame, then design a branded cover.

v0.2 grabbed a frame at a fixed percentage of the video and pasted a title on
it - which regularly produced a blurred mid-scroll frame with no information.

v0.3 *scores* candidate moments using the video understanding (result reveals,
text density, sharpness, stillness, how much of the page is legible), renders
several cover concepts, and keeps the one that measures best.  Everything is
Pillow + FFmpeg; no models, no network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from contentforge.log import get_logger
from contentforge.models.schemas import EditPlan, GroundedScript, Region, VideoUnderstanding
from contentforge.thumbnails.generator import _load_font, _wrap_for_width
from contentforge.utils.ffmpeg import FFmpeg

log = get_logger("cover")


@dataclass
class CoverStyle:
    width: int = 1080
    height: int = 1920
    font: str = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    font_regular: str = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    title_size: int = 104
    kicker_size: int = 46
    brand_size: int = 40
    accent: str = "#FF7A00"
    background: str = "#0F0F14"
    text: str = "#FFFFFF"
    brand: str = "StudentTools.pk"
    max_title_words: int = 7


@dataclass
class CoverCandidate:
    t: float
    score: float
    reasons: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"t": round(self.t, 3), "score": round(self.score, 4), "reasons": self.reasons}


@dataclass
class CoverResult:
    path: Path
    frame_time: float
    concept: str
    candidates: list[CoverCandidate] = field(default_factory=list)
    concepts: dict[str, float] = field(default_factory=dict)
    title: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "frame_time": round(self.frame_time, 3),
            "concept": self.concept,
            "title": self.title,
            "concept_scores": {k: round(v, 4) for k, v in self.concepts.items()},
            "candidates": [c.to_dict() for c in self.candidates[:8]],
        }


# --------------------------------------------------------------------- scoring
def score_frames(u: VideoUnderstanding, plan: EditPlan | None = None) -> list[CoverCandidate]:
    """Rank sampled frames as cover material. Highest score first."""
    if not u.frames:
        return []
    reveal_times = [a.mid for a in u.actions if a.kind in ("reveal", "navigate")]
    idle_spans = [(a.start, a.end) for a in u.actions if a.kind == "idle"]
    kept: list[tuple[float, float]] = []
    if plan is not None:
        kept = [(s.src_start, s.src_end) for s in plan.shots]
    motions = [f.motion for f in u.frames]
    max_motion = max(motions) if motions else 1.0
    out: list[CoverCandidate] = []
    for f in u.frames:
        reasons: dict[str, float] = {}
        # text/UI density: a cover should show a page with content on it
        text_area = sum(b.region.area for b in f.text_boxes)
        reasons["text"] = float(min(1.0, text_area / 0.28)) * 1.10
        # the moment right after a result appears is the money shot
        near_reveal = min((abs(f.t - r) for r in reveal_times), default=99.0)
        reasons["reveal"] = float(max(0.0, 1.0 - near_reveal / 3.0)) * 1.60
        # stillness: no motion blur, no half-drawn scroll
        reasons["still"] = float(1.0 - min(1.0, f.motion / max(1e-6, max_motion))) * 0.70
        # but not a dead frame at the very start/end
        rel = f.t / max(1e-6, u.duration)
        reasons["position"] = float(0.55 if 0.12 <= rel <= 0.95 else 0.0)
        if any(a <= f.t <= b for a, b in idle_spans):
            reasons["settled"] = 0.30  # nothing moving: the page is fully drawn
        if kept and any(a <= f.t <= b for a, b in kept):
            reasons["in_edit"] = 0.45  # a frame the viewer actually sees
        if f.scene_change:
            reasons["scene_change"] = -0.40
        out.append(CoverCandidate(f.t, float(sum(reasons.values())), reasons))
    out.sort(key=lambda c: -c.score)
    return out


# ------------------------------------------------------------------ generator
class CoverGenerator:
    """Designs a 1080x1920 Instagram cover from the recording."""

    CONCEPTS = ("card", "banner", "split")

    def __init__(self, style: CoverStyle | None = None, ffmpeg: FFmpeg | None = None):
        self.st = style or CoverStyle()
        self.ff = ffmpeg or FFmpeg()

    # ------------------------------------------------------------------ api
    def generate(
        self,
        src: Path,
        dst: Path,
        *,
        understanding: VideoUnderstanding | None = None,
        plan: EditPlan | None = None,
        script: GroundedScript | None = None,
        title: str = "",
        subtitle: str = "",
        work_dir: Path | None = None,
        concepts: tuple[str, ...] | None = None,
    ) -> CoverResult:
        work = work_dir or dst.parent / "cover_work"
        work.mkdir(parents=True, exist_ok=True)
        candidates = score_frames(understanding, plan) if understanding else []
        t = candidates[0].t if candidates else 1.0
        frame_path = work / "cover_frame.png"
        self._grab(src, t, frame_path)
        if not frame_path.exists() and candidates:
            for c in candidates[1:4]:
                self._grab(src, c.t, frame_path)
                if frame_path.exists():
                    t = c.t
                    break

        headline = title or (script.title if script else "") or self.st.brand
        headline = _hook_text(script, headline, self.st.max_title_words)
        sub = subtitle or (script.website if script else "")

        # Optional Canva Connect API cover generation
        try:
            from contentforge.integrations.canva import CanvaClient, detect_canva_capability
            canva_cap = detect_canva_capability()
            if canva_cap.can_generate_covers and frame_path.exists():
                client = CanvaClient()
                canva_out = work / "cover_canva.png"
                res = client.generate_cover(frame_path, headline, sub, canva_out)
                if res and canva_out.exists():
                    log.info("Rendered cover via Canva Connect API")
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    Image.open(canva_out).convert("RGB").save(dst, quality=95)
                    return CoverResult(dst, t, "canva_connect", candidates, {"canva_connect": 1.0}, headline)
        except Exception as exc:
            log.warning("Canva cover generation attempt failed: %s - using Pillow fallback", exc)

        focus = None
        if understanding is not None:
            focus = understanding.content_region
        best: tuple[float, str, Path] | None = None
        scores: dict[str, float] = {}
        for concept in concepts or self.CONCEPTS:
            out = work / f"cover_{concept}.png"
            try:
                self._render_concept(concept, frame_path, out, headline, sub, focus)
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("Cover concept %s failed: %s", concept, exc)
                continue
            score = self._score_design(out)
            scores[concept] = score
            if best is None or score > best[0]:
                best = (score, concept, out)
        if best is None:
            raise RuntimeError("no cover concept could be rendered")
        dst.parent.mkdir(parents=True, exist_ok=True)
        Image.open(best[2]).convert("RGB").save(dst, quality=93)
        for f in work.glob("cover_*.png"):
            f.unlink(missing_ok=True)
        log.info(
            "Cover: frame at %.2fs, concept '%s' (scores: %s)",
            t,
            best[1],
            ", ".join(f"{k}={v:.2f}" for k, v in scores.items()),
        )
        return CoverResult(dst, t, best[1], candidates, scores, headline)

    # -------------------------------------------------------------- drawing
    def _grab(self, src: Path, t: float, dst: Path) -> None:
        try:
            self.ff.run(["-ss", f"{max(0.0, t):.3f}", "-i", str(src), "-frames:v", "1", "-y", str(dst)])
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("Could not grab cover frame at %.2fs: %s", t, exc)

    def _render_concept(
        self, concept: str, frame_path: Path, out: Path, title: str, subtitle: str, focus: Region | None
    ) -> Path:
        st = self.st
        W, H = st.width, st.height
        shot = Image.open(frame_path).convert("RGB") if frame_path.exists() else Image.new("RGB", (16, 9), st.background)
        # show the *page*, not the desktop around it
        shot = _crop_region(shot, focus)
        canvas = Image.new("RGB", (W, H), st.background)
        draw = ImageDraw.Draw(canvas)

        # blurred, darkened backdrop from the same frame
        bg = _cover_fill(shot, W, H).filter(ImageFilter.GaussianBlur(38))
        canvas.paste(bg, (0, 0))
        canvas = Image.blend(canvas, Image.new("RGB", (W, H), st.background), 0.45)
        draw = ImageDraw.Draw(canvas)

        title_font = _fit_font(st.font, st.title_size, title.upper(), W - 140)
        kicker_font = _load_font(st.font_regular, st.kicker_size)
        brand_font = _load_font(st.font, st.brand_size)

        if concept == "split":
            card_top = int(H * 0.44)
            card_h = int(H * 0.40)
            card = _fit_width(shot, W - 96)
            card = card.crop((0, 0, card.width, min(card.height, card_h)))
            canvas.paste(card, (48, card_top))
            draw.rectangle([48, card_top, 48 + card.width, card_top + card.height], outline=st.accent, width=7)
            text_y = int(H * 0.14)
            _block(draw, title, title_font, st, y=text_y, max_w=W - 140)
            draw.text((70, text_y - 78), subtitle.upper(), font=kicker_font, fill=st.accent)
        elif concept == "banner":
            card = _fit_width(shot, W)
            top = int(H * 0.30)
            canvas.paste(card, (0, top))
            band_h = 8
            draw.rectangle([0, top - band_h, W, top], fill=st.accent)
            draw.rectangle([0, top + card.height, W, top + card.height + band_h], fill=st.accent)
            _block(draw, title, title_font, st, y=int(H * 0.055), max_w=W - 120)
            draw.text(
                (70, top + card.height + 60), subtitle.upper(), font=kicker_font, fill=st.text
            )
        else:  # "card"
            card = _fit_width(shot, W - 120)
            card_y = int(H * 0.30)
            shadow = Image.new("RGBA", (card.width + 40, card.height + 40), (0, 0, 0, 0))
            ImageDraw.Draw(shadow).rectangle(
                [20, 20, card.width + 20, card.height + 20], fill=(0, 0, 0, 150)
            )
            canvas.paste(Image.alpha_composite(canvas.convert("RGBA").crop(
                (45, card_y - 20, 45 + shadow.width, card_y - 20 + shadow.height)
            ), shadow).convert("RGB"), (45, card_y - 20))
            canvas.paste(card, (65, card_y))
            draw = ImageDraw.Draw(canvas)
            draw.rectangle([65, card_y, 65 + card.width, card_y + card.height], outline=st.accent, width=8)
            _block(draw, title, title_font, st, y=int(H * 0.085), max_w=W - 140)
            draw.text((70, card_y + card.height + 55), subtitle.upper(), font=kicker_font, fill=st.text)

        # brand strip
        strip_h = 96
        draw.rectangle([0, H - strip_h, W, H], fill=st.accent)
        bw = draw.textlength(st.brand.upper(), font=brand_font)
        draw.text(((W - bw) / 2, H - strip_h + 26), st.brand.upper(), font=brand_font, fill="#101014")
        canvas.save(out)
        return out

    def _score_design(self, path: Path) -> float:
        """Cheap legibility metric: contrast in the title band + overall detail."""
        img = np.asarray(Image.open(path).convert("L"), dtype=np.float32) / 255.0
        h = img.shape[0]
        title_band = img[int(h * 0.04) : int(h * 0.30)]
        body = img[int(h * 0.30) : int(h * 0.92)]
        title_contrast = float(title_band.std())
        body_detail = float(body.std())
        # a good cover has punchy text and a visible (not washed out) screenshot
        brightness_penalty = abs(float(img.mean()) - 0.42)
        return 2.0 * title_contrast + 1.2 * body_detail - 0.8 * brightness_penalty


# ---------------------------------------------------------------- helpers
def _hook_text(script: GroundedScript | None, fallback: str, max_words: int) -> str:
    if script is not None:
        for seg in script.segments:
            if seg.role == "hook" and seg.text.strip():
                return _clip_words(seg.text, max_words)
        if script.title:
            return _clip_words(script.title, max_words)
    return _clip_words(fallback, max_words)


def _clip_words(text: str, max_words: int) -> str:
    words = [w for w in text.replace("\n", " ").split() if w]
    return " ".join(words[:max_words]).strip(" .,:;-")


def _crop_region(img: Image.Image, region: Region | None, pad: float = 0.012) -> Image.Image:
    """Crop a frame down to the detected page/content area."""
    if region is None or region.w <= 0.2 or region.h <= 0.2:
        return img
    r = region.expanded(pad).clamped()
    box = (
        int(r.x * img.width),
        int(r.y * img.height),
        int(min(img.width, (r.x + r.w) * img.width)),
        int(min(img.height, (r.y + r.h) * img.height)),
    )
    if box[2] - box[0] < 32 or box[3] - box[1] < 32:
        return img
    return img.crop(box)


def _fit_font(path: str, size: int, text: str, max_w: int):
    """Largest font size at which the longest word still fits the column."""
    longest = max(text.split(), key=len, default=text)
    font = _load_font(path, size)
    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    while size > 42 and probe.textlength(longest, font=font) > max_w:
        size = int(size * 0.92)
        font = _load_font(path, size)
    return font


def _cover_fill(img: Image.Image, W: int, H: int) -> Image.Image:
    ratio = max(W / img.width, H / img.height)
    resized = img.resize((int(img.width * ratio) + 1, int(img.height * ratio) + 1), Image.LANCZOS)
    left = (resized.width - W) // 2
    top = (resized.height - H) // 2
    return resized.crop((left, top, left + W, top + H))


def _fit_width(img: Image.Image, width: int) -> Image.Image:
    height = max(1, int(img.height * width / max(1, img.width)))
    return img.resize((width, height), Image.LANCZOS)


def _block(draw: ImageDraw.ImageDraw, text: str, font, st: CoverStyle, *, y: int, max_w: int) -> int:
    """Draw the headline as left-aligned lines with an accent bar."""
    lines = _wrap_for_width(draw, text.upper(), font, max_w)
    line_h = int(getattr(font, "size", st.title_size) * 1.16)
    draw.rectangle([56, y - 6, 68, y + line_h * len(lines) - 8], fill=st.accent)
    for i, line in enumerate(lines):
        ly = y + i * line_h
        for dx, dy in ((-3, 0), (3, 0), (0, -3), (0, 3)):
            draw.text((92 + dx, ly + dy), line, font=font, fill="#000000")
        draw.text((92, ly), line, font=font, fill=st.text)
    return y + line_h * len(lines)

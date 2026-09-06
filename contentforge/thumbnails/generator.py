"""Thumbnail generation: image (Pillow) + Canva brief (prompt, text, colours).

The image thumbnail uses a frame from the video, darkens it, adds a brand
accent bar, a large wrapped title and the brand handle - consistent with the
in-video branding so the feed looks uniform. The Canva brief lets a human
produce a designed version quickly when desired.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from contentforge.config.schema import BrandingConfig, ThumbnailConfig
from contentforge.log import get_logger
from contentforge.utils import atomic_write_json, atomic_write_text, hex_to_rgb

log = get_logger("thumbnail")

FALLBACK_FONTS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",  # Fedora
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/liberation-sans/LiberationSans-Bold.ttf",
]


@dataclass
class ThumbnailBrief:
    """Everything a designer (or Canva) needs to build a thumbnail by hand."""

    title: str
    subtitle: str
    text_suggestions: list[str]
    prompt: str
    colors: dict[str, str]
    font_suggestion: str
    size: str
    style_notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_markdown(self) -> str:
        lines = [
            f"# Thumbnail brief: {self.title}",
            "",
            f"**Size:** {self.size}  ",
            f"**Font:** {self.font_suggestion}  ",
            "**Colours:** " + ", ".join(f"{k} `{v}`" for k, v in self.colors.items()),
            "",
            "## Text options",
            *[f"{i}. {t}" for i, t in enumerate(self.text_suggestions, 1)],
            "",
            "## AI image prompt",
            "",
            "```",
            self.prompt,
            "```",
            "",
            "## Canva workflow",
            "1. Create a 1080 x 1920 design (Instagram Reel cover).",
            "2. Upload `thumbnail_frame.png` as the background, apply a 40-50 % dark overlay.",
            "3. Add the title text (option 1) in the bold font above, white with a thick black outline.",
            "4. Add the accent bar / brand handle in the primary colour.",
            "5. Export as PNG and place it next to the video as `cover.png` (optional override).",
            "",
            "## Style notes",
            *[f"- {n}" for n in self.style_notes],
        ]
        return "\n".join(lines) + "\n"


class ThumbnailGenerator:
    def __init__(
        self, config: ThumbnailConfig, branding: BrandingConfig, brand: str = "StudentTools.pk"
    ):
        self.config = config
        self.branding = branding
        self.brand = brand

    # -------------------------------------------------------------- brief
    def build_brief(
        self, title: str, hook: str, keywords: list[str], website: str = ""
    ) -> ThumbnailBrief:
        kw = ", ".join(keywords[:5]) or "student tools"
        short = _shorten(title, 5)
        suggestions = [
            short.upper(),
            _shorten(hook, 7),
            f"FREE {keywords[0].upper()} TOOL" if keywords else "FREE STUDENT TOOL",
            f"Every student needs this ({website or self.brand})",
            "Stop wasting time. Use this.",
        ]
        prompt = (
            f"Vertical 9:16 Instagram Reel cover for a Pakistani student-tools page. Bold, high-contrast, "
            f"clean tech aesthetic. Background: a slightly blurred laptop screen showing a website about {kw}, "
            f"dark overlay for readability. Large white sans-serif headline with a thick black outline reading "
            f"'{short}'. Accent colour {self.config.accent_color} used for an underline bar and the handle "
            f"'{self.brand}'. Bright, energetic, curiosity-driven, no clutter, no small text, no watermark."
        )
        return ThumbnailBrief(
            title=short,
            subtitle=hook,
            text_suggestions=suggestions,
            prompt=prompt,
            colors={
                "accent": self.config.accent_color,
                "text": self.config.text_color,
                "background": self.branding.background_color,
                "secondary": self.branding.secondary_color,
            },
            font_suggestion="Montserrat ExtraBold / Poppins Bold (Canva) - DejaVu Sans Bold (auto)",
            size=f"{self.config.width}x{self.config.height}",
            style_notes=[
                "Keep the headline under 6 words; readers see it as a 2 cm tile.",
                "Same accent colour and handle placement on every video for brand recognition.",
                "Face the visual focus (cursor/button) toward the centre of the frame.",
                "Avoid the bottom 15 % (covered by the Reels UI).",
            ],
        )

    # -------------------------------------------------------------- image
    def render(self, frame_path: Path, out_path: Path, title: str, subtitle: str = "") -> Path:
        cfg = self.config
        W, H = cfg.width, cfg.height
        out_path = Path(out_path)
        img = Image.open(frame_path).convert("RGB")
        img = _cover_resize(img, W, H)
        img = img.filter(ImageFilter.GaussianBlur(1.2))

        overlay = Image.new("RGBA", (W, H), (0, 0, 0, int(255 * cfg.dark_overlay_alpha)))
        # gradient: darker toward the bottom where the title sits
        grad = Image.linear_gradient("L").resize((W, H))
        overlay.putalpha(
            Image.eval(
                grad,
                lambda v: int(
                    255
                    * (
                        cfg.dark_overlay_alpha * 0.6
                        + 0.4 * v / 255 * cfg.dark_overlay_alpha
                        + 0.25 * v / 255
                    )
                ),
            )
        )
        img = Image.alpha_composite(img.convert("RGBA"), overlay)

        draw = ImageDraw.Draw(img)
        accent = hex_to_rgb(cfg.accent_color)
        text_col = hex_to_rgb(cfg.text_color)
        font = _load_font(cfg.font, cfg.title_font_size)
        small = _load_font(cfg.font, int(cfg.title_font_size * 0.42))

        lines = _wrap_for_width(draw, title.upper(), font, W - 160)
        line_h = int(cfg.title_font_size * 1.18)
        block_h = line_h * len(lines)
        y = int(H * 0.58) - block_h // 2
        for line in lines:
            w = draw.textlength(line, font=font)
            x = (W - w) // 2
            _outlined_text(
                draw, (x, y), line, font, text_col, (0, 0, 0), max(4, cfg.title_font_size // 14)
            )
            y += line_h

        # accent bar under title
        bar_w = int(W * 0.28)
        draw.rounded_rectangle(
            [(W - bar_w) // 2, y + 18, (W + bar_w) // 2, y + 34], radius=8, fill=accent
        )

        if subtitle:
            sub_lines = _wrap_for_width(draw, subtitle, small, W - 200)[:2]
            sy = y + 70
            for line in sub_lines:
                w = draw.textlength(line, font=small)
                _outlined_text(draw, ((W - w) // 2, sy), line, small, (235, 235, 235), (0, 0, 0), 3)
                sy += int(cfg.title_font_size * 0.55)

        # brand pill (top-left)
        pill_font = _load_font(cfg.font, int(cfg.title_font_size * 0.36))
        label = self.brand
        pw = draw.textlength(label, font=pill_font) + 48
        draw.rounded_rectangle(
            [60, 80, 60 + pw, 80 + int(cfg.title_font_size * 0.7)], radius=30, fill=accent
        )
        draw.text(
            (84, 80 + int(cfg.title_font_size * 0.14)), label, font=pill_font, fill=(255, 255, 255)
        )

        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.convert("RGB").save(out_path, quality=92)
        log.info("Thumbnail written: %s", out_path)
        return out_path

    def write_brief(self, brief: ThumbnailBrief, out_dir: Path) -> dict[str, Path]:
        return {
            "json": atomic_write_json(out_dir / "thumbnail_brief.json", brief.to_dict()),
            "md": atomic_write_text(out_dir / "thumbnail_brief.md", brief.to_markdown()),
        }


# ------------------------------------------------------------------ helpers
def _load_font(path: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in [path, *FALLBACK_FONTS]:
        try:
            if candidate and Path(candidate).exists():
                return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    log.warning("No TrueType font found; falling back to Pillow default (small text)")
    return ImageFont.load_default()


def _cover_resize(img: Image.Image, W: int, H: int) -> Image.Image:
    ratio = max(W / img.width, H / img.height)
    resized = img.resize((int(img.width * ratio) + 1, int(img.height * ratio) + 1), Image.LANCZOS)
    left = (resized.width - W) // 2
    top = (resized.height - H) // 2
    return resized.crop((left, top, left + W, top + H))


def _wrap_for_width(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if cur and draw.textlength(trial, font=font) > max_w:
            lines.append(cur)
            cur = w
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines[:4] or [""]


def _outlined_text(draw, xy, text, font, fill, outline, width):
    x, y = xy
    for dx in range(-width, width + 1, max(1, width // 2)):
        for dy in range(-width, width + 1, max(1, width // 2)):
            if dx or dy:
                draw.text((x + dx, y + dy), text, font=font, fill=outline)
    draw.text((x, y), text, font=font, fill=fill)


def _shorten(text: str, max_words: int) -> str:
    words = text.replace("\n", " ").split()
    out = " ".join(words[:max_words]).rstrip(".,;:!?")
    return out if out else text

"""Brand system: unified visual identity, voice, and typography.

Defines brand guidelines so that covers, captions, CTA, and voice match
consistently across all published Reels without hardcoded strings.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from contentforge.log import get_logger

log = get_logger("brand")


@dataclass
class BrandTypography:
    primary_font: str = "DejaVuSans-Bold.ttf"
    secondary_font: str = "DejaVuSans.ttf"
    accent_font: str = "DejaVuSans-Bold.ttf"
    headline_case: str = "title"  # title | upper | natural


@dataclass
class BrandPalette:
    primary: str = "#FF5722"       # Vibrant accent (brand color)
    secondary: str = "#2196F3"     # Secondary accent
    background_dark: str = "#121212"
    background_card: str = "#1E1E1E"
    text_primary: str = "#FFFFFF"
    text_muted: str = "#B0B0B0"
    highlight: str = "#FFD700"      # Yellow word highlight for captions


@dataclass
class BrandConfig:
    """Master brand identity definition."""

    name: str = "StudentTools.pk"
    handle: str = "@studenttools.pk"
    tagline: str = "Free tools & websites every student must know."
    target_audience: str = "Pakistani university and college students"
    tone_of_voice: str = "energetic, practical, concise, hype-free"
    default_cta: str = "Follow @studenttools.pk for more free student tools."

    # Visual tokens
    typography: BrandTypography = field(default_factory=BrandTypography)
    palette: BrandPalette = field(default_factory=BrandPalette)

    # Assets
    logo_path: str = ""
    watermark_enabled: bool = False
    watermark_opacity: float = 0.6

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "handle": self.handle,
            "tagline": self.tagline,
            "target_audience": self.target_audience,
            "tone_of_voice": self.tone_of_voice,
            "default_cta": self.default_cta,
            "typography": asdict(self.typography),
            "palette": asdict(self.palette),
            "logo_path": self.logo_path,
            "watermark_enabled": self.watermark_enabled,
            "watermark_opacity": self.watermark_opacity,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BrandConfig:
        typo_data = d.get("typography", {})
        pal_data = d.get("palette", {})
        return cls(
            name=str(d.get("name", "StudentTools.pk")),
            handle=str(d.get("handle", "@studenttools.pk")),
            tagline=str(d.get("tagline", "Free tools & websites every student must know.")),
            target_audience=str(d.get("target_audience", "Pakistani university and college students")),
            tone_of_voice=str(d.get("tone_of_voice", "energetic, practical, concise, hype-free")),
            default_cta=str(d.get("default_cta", "Follow @studenttools.pk for more free student tools.")),
            typography=BrandTypography(**typo_data) if typo_data else BrandTypography(),
            palette=BrandPalette(**pal_data) if pal_data else BrandPalette(),
            logo_path=str(d.get("logo_path", "")),
            watermark_enabled=bool(d.get("watermark_enabled", False)),
            watermark_opacity=float(d.get("watermark_opacity", 0.6)),
        )

    @classmethod
    def load_from_file(cls, path: Path | str) -> BrandConfig:
        p = Path(path)
        if not p.exists():
            return cls()
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            return cls.from_dict(data)
        except Exception as exc:
            log.warning("Failed to load brand file %s: %s, using defaults", p, exc)
            return cls()

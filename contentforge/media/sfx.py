"""Sound effects library with synthesised fallbacks.

Maps action types from the video understanding stage to appropriate sound
effects.  When no SFX files are available, synthesised blips and tones
continue to work (the v0.3 behaviour).

SFX files live in ``data/assets/sfx/`` as short WAV or MP3 clips.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from contentforge.log import get_logger

log = get_logger("sfx")


@dataclass
class SFXEntry:
    """A single sound effect mapping."""

    id: str
    category: str           # click | pop | whoosh | success | notification | typing
    local_path: str = ""    # relative to the SFX directory
    volume: float = 0.15    # default volume (0-1)
    description: str = ""
    fallback_tone: str = "sine"  # sine | none — what to synthesise if no file
    fallback_freq: float = 1200.0  # Hz for synthesised tone
    fallback_duration: float = 0.035  # seconds for synthesised tone

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "local_path": self.local_path,
            "volume": self.volume,
            "description": self.description,
        }


# Built-in synthesised SFX definitions (always available, no files needed)
BUILTIN_SFX: dict[str, SFXEntry] = {
    "click": SFXEntry(
        id="click_default",
        category="click",
        volume=0.16,
        description="Subtle click for mouse clicks",
        fallback_freq=1200.0,
        fallback_duration=0.035,
    ),
    "pop": SFXEntry(
        id="pop_default",
        category="pop",
        volume=0.12,
        description="Light pop for UI elements",
        fallback_freq=800.0,
        fallback_duration=0.05,
    ),
    "whoosh": SFXEntry(
        id="whoosh_default",
        category="whoosh",
        volume=0.10,
        description="Short sweep for transitions",
        fallback_freq=600.0,
        fallback_duration=0.08,
    ),
    "success": SFXEntry(
        id="success_default",
        category="success",
        volume=0.14,
        description="Gentle chime for result reveals",
        fallback_freq=1500.0,
        fallback_duration=0.12,
    ),
    "notification": SFXEntry(
        id="notification_default",
        category="notification",
        volume=0.11,
        description="Soft ping for alerts",
        fallback_freq=1000.0,
        fallback_duration=0.06,
    ),
    "typing": SFXEntry(
        id="typing_default",
        category="typing",
        volume=0.06,
        description="Keyboard click for typing actions",
        fallback_freq=2400.0,
        fallback_duration=0.02,
    ),
}

# Map action types to SFX categories
ACTION_TO_SFX: dict[str, str] = {
    "click": "click",
    "reveal": "success",
    "navigate": "whoosh",
    "type": "typing",
    "scroll": "",  # no SFX for scroll by default
    "idle": "",
}


class SFXLibrary:
    """Sound effects manager with fallback to synthesised tones."""

    def __init__(self, sfx_dir: Path | None = None):
        self.sfx_dir = sfx_dir or Path("data/assets/sfx")
        self.entries: dict[str, SFXEntry] = dict(BUILTIN_SFX)
        self._load_custom()

    def _load_custom(self) -> None:
        """Load custom SFX files from the sfx directory."""
        if not self.sfx_dir.exists():
            return
        for path in sorted(self.sfx_dir.iterdir()):
            if path.suffix.lower() in (".wav", ".mp3", ".ogg"):
                name = path.stem.lower()
                # Categorise by filename convention: click_01.wav → click
                category = name.split("_")[0] if "_" in name else name
                if category in BUILTIN_SFX:
                    # Custom file overrides the built-in synthesised version
                    entry = SFXEntry(
                        id=name,
                        category=category,
                        local_path=str(path.relative_to(self.sfx_dir)),
                        volume=BUILTIN_SFX[category].volume,
                        description=f"Custom SFX: {path.name}",
                    )
                    self.entries[category] = entry
                    log.debug("Custom SFX: %s → %s", category, path.name)

    def get_for_action(self, action_kind: str) -> SFXEntry | None:
        """Return the SFX entry for a given action type, or None."""
        sfx_category = ACTION_TO_SFX.get(action_kind, "")
        if not sfx_category:
            return None
        return self.entries.get(sfx_category)

    def resolve_path(self, entry: SFXEntry) -> Path | None:
        """Return the absolute path to an SFX file, or None for synthesised."""
        if not entry.local_path:
            return None
        path = self.sfx_dir / entry.local_path
        return path if path.exists() else None

    def has_file(self, entry: SFXEntry) -> bool:
        """True if there is a real audio file for this entry."""
        return self.resolve_path(entry) is not None

    @property
    def available_categories(self) -> list[str]:
        """List of SFX categories that have either files or synthesised fallbacks."""
        return list(self.entries.keys())

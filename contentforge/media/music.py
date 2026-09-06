"""Music catalog: licensed tracks with mood-based selection.

Stores metadata about available background music tracks and selects the best
match for a given creative plan.  Tracks live in ``data/assets/music/`` and
their metadata lives in ``catalog.json`` alongside them.

The pipeline works fine without any music — this is a premium layer.
"""

from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from contentforge.log import get_logger

log = get_logger("music")


@dataclass
class MusicTrack:
    """Metadata for a single background music track."""

    id: str
    title: str
    artist: str = ""
    license: str = "CC0"
    license_url: str = ""
    source: str = "local"     # local | pixabay | freesound | custom
    source_url: str = ""
    attribution_required: bool = False
    attribution_text: str = ""
    local_path: str = ""      # relative to the catalog directory
    duration_seconds: float = 0.0
    mood: list[str] = field(default_factory=list)     # tags: energetic, tech, calm...
    tempo_bpm: int = 0
    energy: float = 0.5       # 0=ambient, 1=high energy
    genre: str = ""
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> MusicTrack:
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class MusicSelection:
    """Result of the music selection process."""

    track: MusicTrack | None = None
    reason: str = ""
    alternatives: list[MusicTrack] = field(default_factory=list)
    mood_match: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "track": self.track.to_dict() if self.track else None,
            "reason": self.reason,
            "mood_match": round(self.mood_match, 3),
            "alternatives": len(self.alternatives),
        }


class MusicCatalog:
    """Load, search, and select music from a local catalog."""

    def __init__(self, catalog_dir: Path | None = None):
        self.catalog_dir = catalog_dir or Path("data/assets/music")
        self.tracks: list[MusicTrack] = []
        self._recently_used: list[str] = []
        self._load()

    def _load(self) -> None:
        """Load the catalog from catalog.json."""
        catalog_file = self.catalog_dir / "catalog.json"
        if not catalog_file.exists():
            log.debug("No music catalog at %s", catalog_file)
            return
        try:
            data = json.loads(catalog_file.read_text())
            for td in data.get("tracks", []):
                track = MusicTrack.from_dict(td)
                self.tracks.append(track)
            log.info("Loaded %d tracks from music catalog", len(self.tracks))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Failed to load music catalog: %s", exc)

    def list_tracks(self) -> list[MusicTrack]:
        """Return all registered tracks."""
        return list(self.tracks)

    @property
    def available(self) -> bool:
        """True when at least one track is available."""
        return len(self.tracks) > 0

    def search(
        self,
        query: str = "",
        *,
        mood: str | list[str] | None = None,
        min_duration: float = 0,
        energy_range: tuple[float, float] | None = None,
        genre: str | None = None,
        tags: list[str] | None = None,
    ) -> list[MusicTrack]:
        """Search for tracks matching the given criteria."""
        q = query.strip().lower()
        moods = [mood] if isinstance(mood, str) else (mood or [])
        results = []
        for t in self.tracks:
            if q and not (
                q in t.title.lower()
                or q in t.artist.lower()
                or any(q in m.lower() for m in t.mood)
                or any(q in tag.lower() for tag in t.tags)
            ):
                continue
            # Duration check
            if min_duration > 0 and t.duration_seconds < min_duration:
                continue
            # Mood check
            if moods and not any(m in t.mood for m in moods):
                continue
            # Energy check
            if energy_range and not (energy_range[0] <= t.energy <= energy_range[1]):
                continue
            # Genre check
            if genre and t.genre != genre:
                continue
            # Tags check
            if tags and not any(tag in t.tags for tag in tags):
                continue
            results.append(t)
        return results

    def select(
        self,
        *,
        mood: str = "tech",
        energy: float = 0.7,
        target_energy: float | None = None,
        min_duration: float = 15.0,
        avoid_ids: list[str] | None = None,
    ) -> MusicSelection:
        """Select the best track for the given creative parameters."""
        if target_energy is not None:
            energy = target_energy
        if not self.tracks:
            return MusicSelection(reason="no tracks in catalog")

        avoid = set(avoid_ids or []) | set(self._recently_used[-5:])

        candidates = self.search(
            mood=mood,
            min_duration=min_duration,
            energy_range=(max(0, energy - 0.3), min(1, energy + 0.3)),
        )

        if not candidates:
            # Broaden search
            candidates = self.search(min_duration=min_duration)
        if not candidates:
            candidates = list(self.tracks)

        # Filter out recently used
        fresh = [t for t in candidates if t.id not in avoid]
        if not fresh:
            fresh = candidates  # all used, allow repeats

        # Score candidates
        scored: list[tuple[float, MusicTrack]] = []
        for t in fresh:
            score = 0.0
            # Mood match
            if mood in t.mood:
                score += 1.0
            # Energy match (closer = better)
            score += max(0, 1.0 - abs(t.energy - energy) * 2)
            # Duration fit bonus (prefer tracks that are longer than needed)
            if t.duration_seconds >= min_duration:
                score += 0.3
            # Variety penalty for recently used
            if t.id in self._recently_used:
                score -= 0.5
            scored.append((score, t))

        scored.sort(key=lambda x: -x[0])
        best_score, best_track = scored[0]

        self._recently_used.append(best_track.id)
        if len(self._recently_used) > 10:
            self._recently_used = self._recently_used[-5:]

        return MusicSelection(
            track=best_track,
            reason=f"mood={mood}, energy={energy:.1f}, score={best_score:.2f}",
            alternatives=[t for _, t in scored[1:4]],
            mood_match=min(1.0, best_score / 2.0),
        )

    def resolve_path(self, track: MusicTrack) -> Path | None:
        """Return the absolute path to a track's audio file."""
        if not track.local_path:
            return None
        path = self.catalog_dir / track.local_path
        return path if path.exists() else None

    def save_catalog(self) -> None:
        """Write the catalog back to disk."""
        self.catalog_dir.mkdir(parents=True, exist_ok=True)
        data = {"tracks": [t.to_dict() for t in self.tracks]}
        (self.catalog_dir / "catalog.json").write_text(
            json.dumps(data, indent=2, ensure_ascii=False)
        )

    def add_track(self, track: MusicTrack) -> None:
        """Add a track to the catalog."""
        self.tracks.append(track)
        self.save_catalog()
        log.info("Added track: %s (%s)", track.title, track.id)

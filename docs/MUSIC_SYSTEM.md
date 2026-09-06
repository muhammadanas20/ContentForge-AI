# Music System — ContentForge-AI v0.4

## Design Principles

1. **Never use copyrighted music** — no scraping from Instagram/TikTok/Spotify/YouTube
2. **License metadata required** — every track must have documented licensing
3. **Mood-based selection** — music matches the content automatically
4. **Graceful without music** — pipeline works with narration only

## Music Catalog

### Structure

```
data/assets/music/
    catalog.json          # metadata for all tracks
    energetic-tech-01.mp3
    calm-ambient-01.mp3
    ...
```

### Catalog Schema

```json
{
  "tracks": [
    {
      "id": "energetic-tech-01",
      "title": "Digital Pulse",
      "artist": "Example Artist",
      "license": "CC0",
      "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
      "source": "pixabay",
      "source_url": "https://pixabay.com/music/...",
      "attribution_required": false,
      "attribution_text": "",
      "local_path": "energetic-tech-01.mp3",
      "duration_seconds": 120.0,
      "mood": ["energetic", "tech", "modern"],
      "tempo_bpm": 128,
      "energy": 0.85,
      "genre": "electronic",
      "tags": ["ai", "tool", "fast", "demo"],
      "downloaded_at": "2024-01-15T10:00:00Z"
    }
  ]
}
```

### Mood Categories

| Mood | Use Case |
|---|---|
| `energetic` | Fast AI tool demos, productivity |
| `tech` | Technology, coding, AI |
| `educational` | Tutorials, explainers |
| `motivational` | Study tips, career advice |
| `calm` | Ambient, reading, meditation tools |
| `cinematic` | Premium reveals, dramatic moments |
| `playful` | Fun tools, games, quizzes |
| `minimal` | Clean, modern, understated |

## Track Selection Algorithm

```python
def select_track(creative_plan: CreativePlan, duration: float) -> MusicTrack:
    candidates = catalog.search(
        mood=creative_plan.music_mood,
        min_duration=duration,
        energy_range=(creative_plan.energy - 0.2, creative_plan.energy + 0.2),
    )
    # Score by mood match, duration fit, and variety (avoid repetition)
    return best_candidate
```

## Audio Integration

Music is integrated through the existing `ReelAudioMixer`:

1. **Auto-loop** if track is shorter than the Reel
2. **Fade in** at the start (0.4s)
3. **Fade out** before the end (0.6s)
4. **Duck** under narration using `sidechaincompress`
5. **Volume** set by config (default `music_volume: 0.08`)

## Sound Effects Library

### SFX Categories

| Category | File | Trigger |
|---|---|---|
| `click` | Subtle mechanical click | Click action detected |
| `pop` | Light pop | UI element appears |
| `whoosh` | Short directional sweep | Scene transition |
| `success` | Gentle chime | Result revealed |
| `notification` | Soft ping | Alert/notification |
| `typing` | Keyboard press | Typing detected |

### Fallback

When no SFX files are available, the existing synthesised sine-blip
approach continues to work. SFX are optional enhancements.

## Legal Sources

### Recommended

| Source | License | Attribution |
|---|---|---|
| [Pixabay Music](https://pixabay.com/music/) | Pixabay License (CC0-like) | Not required |
| [Freesound.org](https://freesound.org/) | CC0 / CC-BY | Varies |
| Custom synthesised | Original | None |

### Prohibited

- ❌ Instagram/TikTok audio rips
- ❌ Spotify/YouTube downloads
- ❌ "Free MP3 download" websites
- ❌ Any track without verifiable licensing

## CLI Commands

```bash
contentforge music search "energetic tech"
contentforge music list
contentforge music add <file> --mood tech --tempo 128 --license CC0
contentforge music remove <track_id>
```

# Quality System — ContentForge-AI v0.4

## Architecture

```
Render → Technical QA → Creative QA → Package (or Repair + Re-render)
```

## Technical Quality Gates (Existing — KEEP)

| Gate | Blocking | What it checks |
|---|---|---|
| `output_file` | ✅ | File exists and is large enough |
| `output_readable` | ✅ | FFmpeg can decode it |
| `aspect_9_16` | ✅ | Correct 9:16 ratio |
| `duration` | ✅ | Within min/max bounds |
| `audio_present` | ✅ | Has an audio track |
| `ocr_preserved` | ✅ | Important text regions not destroyed |
| `narration_exists` | ✅ | Narration track was generated |
| `narration_coverage` | ✅ | Narration covers the timeline |
| `narration_duration` | ✅ | Narration matches video duration |
| `captions_exist` | ✅ | Caption chunks present |
| `captions_safe` | ✅ | Captions within safe margins |
| `cover_exists` | ✅ | 1080x1920 cover image |
| `script_grounded` | ✅ | Script segments map to visuals |
| `cursor_visible` | ⚠️ | Cursor visible at click moments |
| `audio_levels` | ⚠️ | Peak/loudness within bounds |

## Creative Quality Scoring (NEW in v0.4)

### Editing Score Dimensions

| Dimension | Description | Weight |
|---|---|---|
| `hook_strength` | First 3 seconds: visual clarity + narration hook + curiosity | 1.5 |
| `first_3s_clarity` | What/why communicated in opening | 1.2 |
| `visual_change_rate` | Dynamic editing (not static) | 0.8 |
| `information_density` | Words/second balanced with visual content | 0.9 |
| `pacing` | Rhythm variation, no dead time | 1.0 |
| `payoff_strength` | Result moment is clear and held | 1.1 |
| `caption_readability` | Mobile-safe, not covering UI | 0.7 |
| `audio_clarity` | Narration clear, music balanced | 0.8 |
| `cta_quality` | CTA present, specific, actionable | 0.6 |
| `visual_coherence` | Consistent framing, no jitter | 0.7 |

### Scoring Method

1. **Rule-based heuristics** (always available):
   - Hook: check first segment role, narration starts within 0.5s
   - Pacing: shot duration variance, no shot > max_shot
   - Payoff: reveal action exists, held for result_hold seconds
   - CTA: last segment has role "cta"

2. **Gemini-assisted** (when available):
   - Send first 3 frames + script to Gemini for hook assessment
   - Evaluate narration-visual alignment quality

### Quality Report Output

```json
{
  "technical": {"passed": true, "errors": 0, "warnings": 1},
  "creative": {
    "hook_strength": 0.91,
    "pacing": 0.84,
    "visual_clarity": 0.95,
    "audio": 0.89,
    "caption_quality": 0.93,
    "cta": 0.82,
    "overall": 0.89
  }
}
```

## Repair Loop

When a quality gate fails and the failure is automatically repairable:

| Failure | Auto-repair |
|---|---|
| Narration too short | Re-time with slower atempo |
| Captions off-screen | Reposition to alternate band |
| Audio clipping | Re-normalize with lower volume |
| Cover too small | Re-render cover |

Failures that cannot be auto-repaired are reported with actionable diagnostics.

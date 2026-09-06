# Architecture Audit — ContentForge-AI v0.3 → v0.4

## Executive Summary

The v0.3 codebase is **production-quality** with 145 tests, typed schemas,
resumable pipeline, 15+ quality gates, and a well-designed module structure.
The core architecture is sound and should be preserved. The upgrades needed
are primarily about **intelligence** (adding Gemini AI) and **content enrichment**
(music, SFX, brand system, creative planning) rather than architectural rework.

---

## Subsystem Audit

### 1. Pipeline Runner (`pipeline/runner.py`) — **KEEP**

- Resumable, retry-capable, step-based architecture
- Clean separation of concerns: runner doesn't know about FFmpeg
- State persistence in `state.json` for crash recovery
- Thread-safe with cancellation support
- **Verdict**: Excellent. No changes needed.

### 2. Config System (`config/schema.py`) — **KEEP + EXTEND**

- Pydantic validation catches typos at startup
- 548 lines, 30+ typed config sections
- Preset system with deep merge
- Env variable overrides
- **Change**: Add `GeminiConfig`, `BrandConfig`, `MusicConfig`, `CreativeConfig` sections
- **Keep**: Everything else

### 3. Video Understanding (`processing/video_understanding.py`) — **KEEP + ENHANCE**

- 453 lines, one decode pass, adaptive sampling
- Detects: clicks, scrolling, typing, navigation, reveals, idle
- Uses cursor tracker + OCR (tesseract or heuristic)
- Bounded for 8 GB RAM (work_width, max_frames)
- **Change**: Add optional Gemini Vision enrichment after the OpenCV pass
- **Keep**: The entire OpenCV analysis pipeline

### 4. OCR System (`processing/ocr.py`) — **KEEP**

- 262 lines, pluggable backend (tesseract + heuristic)
- Normalized bounding boxes
- Heuristic detector is ~2ms per frame
- **Verdict**: Well-designed. No changes needed.

### 5. Content-Aware Framing (`processing/framing.py`) — **KEEP**

- 520 lines, 8-factor scoring function
- Canvas vs fill layout selection
- Sliced-text penalty prevents ugly half-word crops
- Smoothed camera path (deadzone + EMA + max pan speed)
- **Verdict**: This is one of the best-engineered parts. Keep as-is.

### 6. Smart Editor (`processing/editor.py`) — **KEEP**

- 674 lines, produces typed EditPlan
- Dead time removal, action-based cuts, role assignment
- Budget fitting (speedup + drop least interesting)
- Dynamic zooms, emphasis list
- **Verdict**: Well-designed. Keep as-is.

### 7. Script Writer (`ai/script_writer.py`) — **KEEP + ENHANCE**

- 882 lines, both legacy ScriptWriter and GroundedScriptPlanner
- Evidence-bound segments with visual action references
- LLM polish with grounding guard
- **Change**: Add multi-candidate hook generation, Gemini integration
- **Keep**: Grounding logic, evidence binding, fallback system

### 8. LLM Client (`ai/llm.py`) — **CHANGE (ADD GEMINI)**

- 152 lines, supports OpenAI + Ollama
- Clean interface: `complete()` returns `None` on failure
- **Critical change**: Add Gemini provider (user has API keys)
- **Keep**: The None-on-failure contract, provider abstraction

### 9. Narration Builder (`ai/narration.py`) — **KEEP**

- 262 lines, per-segment synthesis fitted to timeline
- atempo adjustment, silence bed, timeline alignment
- **Verdict**: Excellent design. Keep as-is.

### 10. TTS System (`ai/tts/`) — **KEEP**

- 4 engines: Piper, Kokoro, Edge, eSpeak
- Automatic fallback chain
- eSpeak always available (no model download)
- **Verdict**: Very well-designed. Keep as-is.

### 11. Captions (`media/captions.py`) — **KEEP**

- 498 lines, word-highlighted chunks
- Safe-area placement, long-word shrinking
- Click rings, watermark, progress bar, hook/CTA cards
- Single ASS file for one-pass rendering
- **Verdict**: Premium quality. Keep as-is.

### 12. Audio Mixer (`media/audio.py`) — **KEEP + ENHANCE**

- 191 lines, narration + ducked music + click ticks
- sidechaincompress, limiter, loudnorm
- **Change**: Integrate music catalog for automatic track selection
- **Change**: Integrate SFX library for richer sound effects
- **Keep**: FFmpeg filter architecture

### 13. Cover Generator (`media/cover.py`) — **KEEP + ENHANCE**

- 336 lines, scored frame selection, 3 cover concepts
- Branded design with Pillow
- **Change**: Add optional Canva integration for premium designs
- **Keep**: Frame scoring, concept selection, Pillow fallback

### 14. Quality Gates (`pipeline/quality.py`) — **KEEP + EXTEND**

- 285 lines, 15 gates
- Blocks packaging when errors found
- **Change**: Add creative quality scores (editing score)
- **Keep**: All existing gates

### 15. v0.2 Classic Pipeline (`pipeline/steps.py`) — **KEEP**

- 36K lines, fully functional
- Backward compatible
- **Verdict**: Keep as-is. Never break existing functionality.

### 16. Database (`db.py`) — **KEEP**

- SQLite/PostgreSQL, WAL mode
- Jobs, steps, metrics, hashtag history, events
- **Verdict**: Keep as-is.

### 17. Schemas (`models/schemas.py`) — **KEEP + EXTEND**

- 977 lines, plain dataclasses (not Pydantic — intentionally)
- Resolution-independent normalized coordinates
- Round-trip serialization
- **Change**: Add `CreativePlan`, `EditingScore`, `MusicSelection` types
- **Keep**: Everything else

---

## What Should Be REMOVED

| Item | Reason |
|---|---|
| Nothing | The v0.3 codebase has no dead weight. Every module is tested and used. |

The codebase is remarkably clean. There is no code to remove.

---

## What Should Be REFACTORED

| Item | Current | Target |
|---|---|---|
| `ai/llm.py` | OpenAI/Ollama only | Add Gemini provider |
| `ai/script_writer.py` | Single candidate | Multi-candidate with scoring |
| Config presets | 2 presets | 8+ creative presets |

---

## What Should Be ADDED

| Module | Purpose |
|---|---|
| `ai/llm.py` Gemini provider | Use user's Gemini API for intelligence |
| `creative/director.py` | Unified creative planning stage |
| `media/music.py` | Music catalog with mood/tempo selection |
| `media/sfx.py` | Sound effect library |
| `brand.py` | Formal brand configuration |
| `pipeline/creative_qa.py` | Creative quality scoring |
| `integrations/canva/` | Optional Canva Connect integration |
| CLI commands | inspect, debug, rerender, music, canva |

---

## Performance Assessment

| Metric | Value | Acceptable? |
|---|---|---|
| Understanding (6fps, 24s video) | ~4s | ✅ |
| Edit planning | ~6s | ✅ |
| Compose (7 shots) | ~20s | ✅ |
| Captions + mix + burn | ~8s | ✅ |
| Cover + quality + package | ~1.5s | ✅ |
| Total (24s input) | ~40s | ✅ |

Performance is already good. Gemini API calls will add 2-5s per job but
provide dramatically better creative intelligence.

---

## Memory Risk Assessment

- Frames are analysed at `work_width` (960px) — ✅ bounded
- `max_frames` caps analysis — ✅ bounded
- Intermediates deleted early (`delete_intermediates_early`) — ✅ managed
- No large models loaded in memory — ✅ (Whisper is optional)
- Gemini is an API call, not a local model — ✅ no memory impact

**Conclusion**: 8 GB RAM is sufficient. No changes needed.

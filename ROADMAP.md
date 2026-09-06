# Roadmap

Legend: ✅ done · 🔄 in progress · ⏳ planned

## v0.1.0 - Foundation (current)

| Area | Status | Notes |
|---|---|---|
| Typed YAML configuration, local overrides, env overrides, `.env` secrets | ✅ | `contentforge/config` |
| Rich console + dated file logging with retention | ✅ | |
| FFmpeg/ffprobe wrapper with fallback parsing | ✅ | works with static builds lacking ffprobe |
| SQLite job store (PostgreSQL optional) | ✅ | jobs, steps, metrics, hashtag history, events |
| Folder watcher (inotify/polling), stability wait, dedupe | ✅ | |
| Faster-Whisper transcription → TXT/SRT/JSON | ✅ | |
| Grounded script writer (hook/body/CTA), LLM optional with hallucination guard | ✅ | rule-based baseline |
| TTS: Piper, Kokoro, Edge with fallback | ✅ | sentence timings for captions/zoom anchors |
| Silence removal, motion jump cuts, `Timeline` remap | ✅ | |
| Smart 9:16 crop, zoom pulses, fade transitions | ✅ | |
| Narration/video sync (retime / freeze), loudness mix | ✅ | |
| Subtitles (4 styles, keyword highlight), progress bar, watermark, hook/CTA cards | ✅ | single ASS pass |
| Thumbnail + Canva brief | ✅ | |
| Caption / CTA / comment prompt / SEO / YouTube title / rotating hashtags | ✅ | |
| Analytics store, score, suggestions, weekly/monthly reports, CSV import | ✅ | |
| Upload package + manifest, archive, safe cleanup with disk floor | ✅ | |
| Resumable runner with retries, `--from` step, crash resume | ✅ | |
| APScheduler (immediate/scheduled, cleanup, reports, health) + cron/systemd docs | ✅ | |
| Streamlit dashboard (queue, completed, errors, logs, analytics, storage, health) | ✅ | |
| CLI (`run process retry resume jobs steps cleanup analytics doctor dashboard config`) | ✅ | |
| Fedora installer, systemd units, backup/restore scripts | ✅ | |
| Documentation set | ✅ | `docs/` |
| Test suite (78 tests incl. real renders + E2E) | ✅ | |

## v0.2 - Quality of output

- ⏳ Cursor-aware crop: detect the mouse pointer (template match) and weight the crop centre toward it.
- ⏳ Word-level Whisper alignment of the **narration** (run Whisper on the TTS output) for exact karaoke timing instead of even distribution.
- ⏳ Optional B-roll cards: auto-insert a title card per script step when the screen is static for long.
- ⏳ Background music library with auto-ducking under speech (sidechain compress).
- ⏳ Additional subtitle presets (Hormozi-style boxed words, two-tone) and font packaging.
- ⏳ Urdu/Roman-Urdu narration presets (Edge `ur-PK`, Kokoro multilingual) and RTL-safe captions.

## v0.3 - Operations

- ⏳ Web hooks / Telegram notification when a package is ready or a job fails.
- ⏳ Optional official Instagram Graph API / YouTube Data API publishing module (opt-in, credentials via `.env`).
- ⏳ Import of official insights CSV exports with column mapping presets.
- ⏳ Multi-profile config (several brands from one install).
- ⏳ Docker image with ffmpeg + models pre-baked.

## v0.4 - Intelligence

- ⏳ A/B hook generation (two scripts per video, pick via early metrics).
- ⏳ Topic suggestions from analytics ("your PDF videos outperform - here are 5 related tools").
- ⏳ Automatic quality gate: reject renders with low speech coverage or clipped audio before packaging.

## Next recommended task

**Cursor-aware smart crop (v0.2)** - highest visible quality gain for screen recordings: track the pointer with a
small template/colour detector in `processing/analysis.py`, blend its position into `centers_x`, and add a
`video.crop.follow_cursor` switch with tests using a synthetic moving-dot video.

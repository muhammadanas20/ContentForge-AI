# Roadmap

Legend: ✅ done · 🔄 in progress · ⏳ planned

## v0.1.0 - Foundation

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

## v0.2.0 - Screen recording quality (current)

| Area | Status | Notes |
|---|---|---|
| Cursor detection + tracking (`processing/cursor.py`), one decode pass shared with motion analysis | ✅ | frame-diff blob detector with online template, scroll/page-load rejection |
| Dynamic 9:16 crop following the cursor (dead-zone, EMA smoothing, max pan speed, clamped, re-centre on long cuts) | ✅ | FFmpeg `crop` with piecewise-linear `x(t)` in output time; static smart-crop fallback |
| Word-level narration alignment (Whisper on TTS WAV → script alignment → captions/karaoke) | ✅ | `ai/alignment.py`; sentence-level fallback; **real-model verification pending on Fedora** |
| `student_reel` / `fast_preview` presets, `--preset`, `CONTENTFORGE_PRESET`, configurable intro/outro seconds | ✅ | |
| Low-disk design: free-space floor to start, early intermediate deletion, verified-package cleanup | ✅ | |
| Fedora real-system test guide | ✅ | `docs/fedora-real-system-test.md` |
| Synthetic realistic screen-recording generator + ground-truth cursor tests | ✅ | `tests/screen_recording.py` |
| Test suite | ✅ | 92 tests |

Known limitations (honest): the cursor detector is verified on a synthetic GNOME-like recording, not yet on real
OBS/GNOME footage; pointer shapes with very low contrast (thin I-beam on white) may be missed → static crop. Word
alignment is exercised with a realistic ASR stub only - real Whisper/Piper could not be downloaded in the build sandbox.

## v0.2.x - Follow-ups

- ⏳ Verify cursor tracking + word alignment on real GNOME/OBS recordings with real Whisper/Piper (see Fedora guide) and tune thresholds.
- ⏳ Vertical cursor follow (y) for tall pages when the source is 4:3 / portrait.
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

**Real-world validation run (v0.2.x)** - process 3–5 real GNOME/OBS StudentTools recordings on the Fedora laptop
following `docs/fedora-real-system-test.md`; record `cursor_coverage`, `word_alignment` ratios and visual notes in an
issue, then tune `follow_cursor.*` / `word_level.min_match_ratio` defaults from the evidence. After that:
background-music auto-ducking (sidechain) is the next visible quality gain.

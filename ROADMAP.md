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

## v0.2.0 - Screen recording quality

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

## v0.3.0 - Video understanding & content-aware editing (current)

| Area | Status | Notes |
|---|---|---|
| `VideoUnderstanding` stage: sampled frames, OCR boxes, visual change, cursor, action timeline (click / scroll / type / navigate / reveal / idle) | ✅ | `processing/video_understanding.py`, one decode pass, `understanding.json` artefact |
| Pluggable OCR: tesseract when installed, OpenCV-morphology text regions otherwise | ✅ | `processing/ocr.py` - tesseract is never a hard dependency |
| Content-aware 9:16 framing (scored views, sliced-text penalty, max zoom, canvas vs fill layout, smoothed camera path) | ✅ | `processing/framing.py` |
| Smart editor: dead-time removal, action cuts, result hold, roles on real shot boundaries, dynamic zooms, click emphasis | ✅ | `processing/editor.py` |
| Grounded script planner: hook/setup/demo/payoff/CTA bound to visual segments, degraded-minimal fallback, LLM polish behind a grounding guard | ✅ | `ai/script_writer.py::GroundedScriptPlanner` |
| Narration fitted to the edit timeline (per-segment synthesis + `atempo`) | ✅ | `ai/narration.py`; eSpeak NG fallback engine so narration is never skipped |
| Reel captions: word highlighting, safe-area placement, long-word shrinking, click rings, progress bar, hook/CTA cards | ✅ | `media/captions.py` |
| Audio: narration + music ducked with `sidechaincompress` + synthesised click ticks + limiter/loudnorm | ✅ | `media/audio.py` |
| Cover generator: scored frames, three concepts, best-measuring branded 1080x1920 cover | ✅ | `media/cover.py` |
| Quality gates before packaging (15 checks; errors block, warnings recorded) | ✅ | `pipeline/quality.py`, `quality.md/json` in the package |
| `pipeline.mode` (`smart` / `classic`) - the v0.2 pipeline is kept and still tested | ✅ | `pipeline/smart_steps.py` |
| Tests: ground-truth detection, framing/zoom/preservation, script-to-visual alignment, narration duration, caption safe areas, cover, quality gates, smart end-to-end **with visual assertions** | ✅ | 145 tests |

Known limitations (honest): detection thresholds are tuned on the synthetic tutorial recording
(`tests/screen_recording.py`) - real OBS/GNOME footage may need `understanding.sample_fps` or
`framing.weights.*` tuning; without tesseract the script quotes no on-screen text (it describes actions instead);
music ducking is verified with a synthetic bed, not a real music library.

## v0.2.x - Follow-ups

- ⏳ Verify cursor tracking + word alignment on real GNOME/OBS recordings with real Whisper/Piper (see Fedora guide) and tune thresholds.
- ⏳ Vertical cursor follow (y) for tall pages when the source is 4:3 / portrait.
- ⏳ Optional B-roll cards: auto-insert a title card per script step when the screen is static for long.
- ⏳ Background music library with auto-ducking under speech (sidechain compress).
- ⏳ Additional subtitle presets (Hormozi-style boxed words, two-tone) and font packaging.
- ⏳ Urdu/Roman-Urdu narration presets (Edge `ur-PK`, Kokoro multilingual) and RTL-safe captions.

## v0.3.x - Follow-ups

- ⏳ Validate the smart pipeline on real StudentTools recordings (`data/input/studentofferco.com.mp4`) and tune thresholds.
- ⏳ Install tesseract on the production laptop to unlock text-quoting scripts and text-aware caption placement.
- ⏳ Music library with per-track loudness metadata for the ducked bed.

## v0.4 - Operations

- ⏳ Web hooks / Telegram notification when a package is ready or a job fails.
- ⏳ Optional official Instagram Graph API / YouTube Data API publishing module (opt-in, credentials via `.env`).
- ⏳ Import of official insights CSV exports with column mapping presets.
- ⏳ Multi-profile config (several brands from one install).
- ⏳ Docker image with ffmpeg + models pre-baked.

## v0.5 - Intelligence

- ⏳ A/B hook generation (two scripts per video, pick via early metrics).
- ⏳ Topic suggestions from analytics ("your PDF videos outperform - here are 5 related tools").
- ✅ Automatic quality gate: reject renders with low speech coverage or clipped audio before packaging *(shipped in v0.3)*.

## Next recommended task

**Real-world validation of the smart pipeline (v0.3.x)** - process 3–5 real GNOME/OBS StudentTools recordings on the
Fedora laptop (`contentforge process <file> --website <site>`), then read `quality.md` in each package and *watch* the
Reel. Record: action counts from `understanding.json`, `ocr_regions_preserved`, narration coverage, and visual notes.
Tune `understanding.sample_fps`, `framing.weights.text_cut` / `canvas_bias` and `editing.max_duration` from that
evidence, and install `tesseract-ocr` so scripts can quote what is on screen.

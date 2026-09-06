# ContentForge-AI

**An open-source AI Content Factory for [StudentTools.pk](https://StudentTools.pk).**
Drop a 20-60 second screen recording into a folder → get a publish-ready Instagram Reel / YouTube Short
with narration, subtitles, branding, thumbnail, caption and hashtags. Fully automatic, fully offline-capable,
runs on a Fedora laptop.

```
data/input/smallpdf.com - convert pdf.mp4          (you record this)
            │
            ▼  contentforge run  (watches the folder)
probe → extract audio → UNDERSTAND the recording (frames + OCR + cursor + clicks/scroll/typing/reveals)
  → plan a content-aware edit (dead time out, action cuts, 9:16 framing that never slices text, subtle zooms)
  → grounded script (hook / setup / demo / payoff / CTA - every line bound to a real visual moment)
  → narration fitted to the edit timeline (Piper/Kokoro/Edge/eSpeak) → compose the designed 9:16 Reel
  → word-highlighted captions + click rings + branding → narration mix with ducked music
  → branded cover chosen from the strongest frame → 12 quality gates → upload package
  → archive raw → clean temp files
            │
            ▼
data/output/smallpdfcom-convert-pdf-a1b2c3/
    smallpdfcom-convert-pdf-a1b2c3.mp4   cover.jpg   caption.txt   caption.md   social.json
    script.md   script_grounded.json   subtitles.srt/.ass   edit_plan.json   overlay.json
    quality.md   quality.json   narration.wav   manifest.json
```

## Highlights

| Area | What you get |
|---|---|
| **Zero-touch** | Watchdog folder watcher, resumable pipeline, automatic retries, crash recovery |
| **Video understanding** | Samples frames, reads the screen (tesseract OCR or a dependency-free heuristic text detector), tracks the cursor and builds an **action timeline** - clicks, scrolling, typing, page reveals, idle time - before any editing decision is made |
| **Content-aware 9:16** | Framing is *scored*, not guessed: keep the important text, never slice a headline in half, respect a hard max zoom, prefer a designed canvas (the 16:9 recording inside a branded composition) over a context-destroying crop; smoothed camera path |
| **Grounded script** | Hook → setup → demonstration → payoff → CTA, where every line is tied to a real visual segment (`visual_action`, `focus_region`, source timestamps). Never invents features; optional LLM polish behind a grounding guard |
| **AI voice** | Piper (offline), Kokoro, Edge, eSpeak NG fallback - narration is synthesised **per segment and fitted to the edit timeline**, so it is always as long as the Reel |
| **Smart editor** | Dead-time removal (screen *and* audio idle), action-based cuts, result hold, dynamic zooms, cursor emphasis, click rings + ticks, viral structure applied to real shot boundaries |
| **Captions** | Word-highlighted chunks (≤ 4 words) in mobile-safe type, positioned away from the important UI, long words auto-shrunk, plus progress bar, watermark and hook/CTA cards - one libass pass. The 4 classic subtitle styles remain for `pipeline.mode: classic` |
| **Cover** | Candidate frames are scored (result reveal, text density, stillness, in-edit) and three cover concepts are rendered - the best-measuring branded 1080x1920 cover wins |
| **Quality gates** | 9:16, OCR-region preservation, cursor visible at clicks, narration present and timeline-matched, captions present and on-screen, duration, audio levels, cover, disk hygiene - a failing Reel is never packaged |
| **Publishing kit** | Thumbnail + Canva brief, Instagram caption, rotating category-balanced hashtags, CTA & comment prompt, SEO description, YouTube title |
| **Ops** | SQLite/PostgreSQL job store, Streamlit dashboard (queue, errors, logs, analytics, storage, health), APScheduler jobs, low-disk design (early intermediate deletion, verified-package cleanup, free-space floor), dated Rich logs |
| **Presets** | `student_reel` (production look) and `fast_preview`; `contentforge --preset <name>` or `CONTENTFORGE_PRESET` |
| **Quality** | 145 pytest tests incl. real FFmpeg renders, ground-truth detection tests on a synthetic tutorial recording, visual regression on the rendered Reel, and end-to-end runs of both pipelines; typed Pydantic config, `.env` for secrets |

## Quick start (Fedora)

```bash
git clone https://github.com/muhammadanas20/ContentForge-AI && cd ContentForge-AI
scripts/install_fedora.sh          # ffmpeg (RPM Fusion), fonts, venv, python deps
source .venv/bin/activate
contentforge doctor                # verify ffmpeg / whisper / TTS / disk
contentforge run                   # start watching data/input  (Ctrl-C to stop)
# in another terminal:
contentforge dashboard             # http://localhost:8501
```

Now record your screen (OBS, GNOME Screen Recorder, SimpleScreenRecorder...) for 20-60 s while you talk
through the website, save it as `data/input/<website.com> - <topic>.mp4`, and watch the dashboard.
Naming the file after the website (e.g. `smallpdf.com - merge.mp4`) lets the script and caption mention it.

Manual alternatives:

```bash
contentforge process data/input/demo.mp4 --website smallpdf.com   # one file, now
contentforge retry <job_id> --from subtitles                        # re-render after changing styles
contentforge jobs --status failed                                   # what went wrong
contentforge cleanup --dry-run                                      # what cleanup would delete
contentforge analytics add <job_id> --views 1200 --likes 90 --completion 58
contentforge analytics report
```

## Configuration

Everything lives in [`config/config.yaml`](config/config.yaml) (validated by Pydantic - typos fail fast).
Override locally with `config/config.local.yaml` (git-ignored) or environment variables such as
`CONTENTFORGE__TTS__ENGINE=edge`. Secrets go in `.env` (see `.env.example`) - never in YAML.

Commonly tuned keys:

```yaml
pipeline.mode: smart | classic           # v0.3 Reel pipeline (default) or the v0.2 flow
understanding.sample_fps: 6.0            # main CPU knob for the understanding stage
understanding.ocr_engine: auto           # auto | tesseract | heuristic
framing.max_zoom: 3.6                    # hard cap - never a context-destroying crop
framing.weights.text_cut: 2.0            # raise if a headline ever gets sliced
editing.max_duration: 75.0               # Reel budget
quality.block_on_error: true             # never package a Reel that fails a gate
tts.engine: piper | kokoro | edge | espeak
audio.silence.min_duration: 0.6          # lower = more aggressive cuts
pipeline.steps.*: true/false             # disable any stage
```

See [docs/configuration.md](docs/configuration.md) for every key.

## Documentation

| Doc | Contents |
|---|---|
| [docs/installation.md](docs/installation.md) | Fedora / generic Linux setup, Python env, models, optional Kokoro/Edge/Ollama, systemd |
| [docs/configuration.md](docs/configuration.md) | Every YAML key, env overrides, `.env` |
| [docs/smart-pipeline.md](docs/smart-pipeline.md) | **v0.3**: how understanding, content-aware framing, grounded scripting, the smart editor, captions, cover and the quality gates work |
| [docs/architecture.md](docs/architecture.md) | Module map, data flow, design decisions |
| [docs/workflow.md](docs/workflow.md) | Step-by-step pipeline, resume semantics, output package |
| [docs/dashboard.md](docs/dashboard.md) | Dashboard pages and actions |
| [docs/scheduling.md](docs/scheduling.md) | Immediate vs scheduled mode, APScheduler, cron, systemd |
| [docs/analytics.md](docs/analytics.md) | Metrics, scoring, suggestions, CSV import |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Common errors, FAQ |
| [docs/maintenance.md](docs/maintenance.md) | Updating, backup, restore, cleanup policy |
| [docs/testing.md](docs/testing.md) | Running and writing tests; what is unit / integration / real-model tested |
| [docs/fedora-real-system-test.md](docs/fedora-real-system-test.md) | Exact Fedora commands: deps, Whisper/Piper download, TTS + alignment checks, first video, low-disk config, cleanup |
| [ROADMAP.md](ROADMAP.md) | Status and next tasks |

## Project layout

```
contentforge/
  config/       YAML loader + Pydantic schema
  log/          Rich console + dated file logging
  utils/        FFmpeg wrapper, fs helpers, retry, time/colour formatting
  db.py         SQLite/PostgreSQL job & metrics store
  input/        Watchdog folder watcher
  models/       v0.3 schemas: frames, actions, understanding, edit plan, grounded script, quality report
  ai/           transcriber (Faster-Whisper), llm client, script writer (+ grounded planner), narration builder,
                alignment, social writer, tts/ (piper, kokoro, edge, espeak)
  processing/   ocr, video_understanding, framing, editor (v0.3) + segments, analysis, video_editor, audio_mixer (v0.2)
  media/        captions (Reel overlays), audio (mix + ducking), cover (scored, branded)
  subtitles/    caption chunking, ASS overlay renderer, SRT/JSON/TXT writers (classic pipeline)
  thumbnails/   Pillow thumbnail + Canva brief
  analytics/    metrics, scoring, reports, suggestions
  output/       upload package builder
  archive/      archival of raw recordings
  cleanup/      safe disk hygiene
  scheduling/   APScheduler wrapper
  pipeline/     job context, steps (classic), smart_steps (v0.3), quality gates, resumable runner
  dashboard/    Streamlit app
  app.py        application façade   ·   cli.py  `contentforge` command
config/         config.yaml           data/     runtime folders (git-ignored)
docs/           documentation         scripts/  installers, systemd units, backup/restore, cron
tests/          pytest suite
```

## Running the tests

```bash
pip install -r requirements-dev.txt
pytest                # 145 tests, ~2 min with ffmpeg
pytest -m "not ffmpeg"   # pure-python subset
```

## Platform terms

ContentForge-AI prepares content; it never auto-posts, scrapes or automates Instagram/YouTube accounts.
Publishing is a manual step (or your own integration through the official Instagram Graph / YouTube Data
APIs). Analytics are entered manually or imported from the official insights exports.

## License

MIT

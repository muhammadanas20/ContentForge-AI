# ContentForge-AI

**An open-source AI Content Factory for [StudentTools.pk](https://StudentTools.pk).**
Drop a 20-60 second screen recording into a folder → get a publish-ready Instagram Reel / YouTube Short
with narration, subtitles, branding, thumbnail, caption and hashtags. Fully automatic, fully offline-capable,
runs on a Fedora laptop.

```
data/input/smallpdf.com - convert pdf.mp4          (you record this)
            │
            ▼  contentforge run  (watches the folder)
probe → extract audio → transcribe (Faster-Whisper) → viral script → TTS narration (Piper/Kokoro/Edge)
  → silence & jump-cut analysis (FFmpeg + OpenCV) → 9:16 smart crop + zoom + transitions
  → audio sync & loudness mix → subtitles (keyword highlight) + progress bar + branding cards
  → final MP4 → thumbnail + Canva brief → caption / CTA / hashtags → analytics baseline
  → upload package → archive raw → clean temp files
            │
            ▼
data/output/smallpdfcom-convert-pdf-a1b2c3/
    smallpdfcom-convert-pdf-a1b2c3.mp4   cover.jpg   caption.txt   caption.md   social.json
    script.md   subtitles.srt/.ass/.json  transcript.txt/.srt/.json  thumbnail_brief.md  manifest.json
```

## Highlights

| Area | What you get |
|---|---|
| **Zero-touch** | Watchdog folder watcher, resumable pipeline, automatic retries, crash recovery |
| **Speech → script** | Faster-Whisper (TXT/SRT/JSON) → grounded short-form script (hook, steps, CTA). Never invents features. Optional LLM polish (OpenAI / Ollama) with a hallucination guard |
| **AI voice** | Piper (offline), Kokoro, Edge TTS - switch with one YAML line; graceful fallback to original audio |
| **Editing** | Silence removal, motion-based jump cuts, smart 9:16 crop that follows on-screen activity, eased zoom pulses, micro fade transitions, EBU R128 loudness |
| **Captions** | 4 subtitle styles (bold-pop, clean, karaoke, minimal), keyword highlighting, animated progress bar, watermark, hook & CTA cards - all burned in one libass pass |
| **Publishing kit** | Thumbnail + Canva brief, Instagram caption, rotating category-balanced hashtags, CTA & comment prompt, SEO description, YouTube title |
| **Ops** | SQLite/PostgreSQL job store, Streamlit dashboard (queue, errors, logs, analytics, storage, health), APScheduler jobs, safe cleanup with disk-space floor, dated Rich logs |
| **Quality** | 78 pytest tests incl. real FFmpeg renders and end-to-end pipeline runs, typed Pydantic config, `.env` for secrets |

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
tts.engine: piper | kokoro | edge
subtitles.style: bold-pop | clean | karaoke | minimal
video.crop.mode: smart | center | left | right
audio.silence.min_duration: 0.6          # lower = more aggressive cuts
cleanup.delete_raw_after_success: false  # true once you trust the archive
pipeline.steps.*: true/false             # disable any stage
```

See [docs/configuration.md](docs/configuration.md) for every key.

## Documentation

| Doc | Contents |
|---|---|
| [docs/installation.md](docs/installation.md) | Fedora / generic Linux setup, Python env, models, optional Kokoro/Edge/Ollama, systemd |
| [docs/configuration.md](docs/configuration.md) | Every YAML key, env overrides, `.env` |
| [docs/architecture.md](docs/architecture.md) | Module map, data flow, design decisions |
| [docs/workflow.md](docs/workflow.md) | Step-by-step pipeline, resume semantics, output package |
| [docs/dashboard.md](docs/dashboard.md) | Dashboard pages and actions |
| [docs/scheduling.md](docs/scheduling.md) | Immediate vs scheduled mode, APScheduler, cron, systemd |
| [docs/analytics.md](docs/analytics.md) | Metrics, scoring, suggestions, CSV import |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Common errors, FAQ |
| [docs/maintenance.md](docs/maintenance.md) | Updating, backup, restore, cleanup policy |
| [docs/testing.md](docs/testing.md) | Running and writing tests |
| [ROADMAP.md](ROADMAP.md) | Status and next tasks |

## Project layout

```
contentforge/
  config/       YAML loader + Pydantic schema
  log/          Rich console + dated file logging
  utils/        FFmpeg wrapper, fs helpers, retry, time/colour formatting
  db.py         SQLite/PostgreSQL job & metrics store
  input/        Watchdog folder watcher
  ai/           transcriber (Faster-Whisper), llm client, script writer, social writer, tts/ (piper, kokoro, edge)
  processing/   edit-decision logic, OpenCV analysis, FFmpeg video editor, audio mixer
  subtitles/    caption chunking, ASS overlay renderer, SRT/JSON/TXT writers
  thumbnails/   Pillow thumbnail + Canva brief
  analytics/    metrics, scoring, reports, suggestions
  output/       upload package builder
  archive/      archival of raw recordings
  cleanup/      safe disk hygiene
  scheduling/   APScheduler wrapper
  pipeline/     job context, steps, resumable runner
  dashboard/    Streamlit app
  app.py        application façade   ·   cli.py  `contentforge` command
config/         config.yaml           data/     runtime folders (git-ignored)
docs/           documentation         scripts/  installers, systemd units, backup/restore, cron
tests/          pytest suite
```

## Running the tests

```bash
pip install -r requirements-dev.txt
pytest                # 78 tests, ~30 s with ffmpeg
pytest -m "not ffmpeg"   # pure-python subset
```

## Platform terms

ContentForge-AI prepares content; it never auto-posts, scrapes or automates Instagram/YouTube accounts.
Publishing is a manual step (or your own integration through the official Instagram Graph / YouTube Data
APIs). Analytics are entered manually or imported from the official insights exports.

## License

MIT

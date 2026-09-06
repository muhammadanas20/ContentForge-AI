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
| **AI Creative Director (v0.4)** | Evaluates visual recording signals to produce a unified master `CreativePlan` shaping pacing, layout preference, narrative arc, captions, music, and cover |
| **Gemini LLM & Vision (v0.4)** | Native Google Gemini multimodal integration (`gemini-2.5-flash`) for deep semantic video understanding and vision-grounded script writing |
| **Multi-Candidate Hooks (v0.4)** | Generates multi-style hooks (curiosity, problem, shock, question, secret) with retention & punchiness scoring algorithms |
| **Music & SFX Library (v0.4)** | Mood-based background music selection from licensed catalog with automatic ducking under narration and tactile UI action SFX |
| **Canva Integration (v0.4)** | Optional Canva Connect API cover generation with automated capability detection and 100% offline Pillow fallback |
| **Creative Presets (v0.4)** | 8 tailored presets: `student_reel`, `ai_tool`, `productivity`, `coding`, `website_discovery`, `tutorial`, `premium_minimal`, and `fast_preview` |
| **Brand Identity (v0.4)** | Formal brand configuration (`config/brand.yaml`) governing typography, color palette, handles, and CTA across the pipeline |
| **Creative Quality QA (v0.4)** | 10-dimension artistic score (hook strength, 3s clarity, pacing, payoff, readability) complementing the 15+ technical quality gates |
| **Video understanding** | Samples frames, reads the screen (tesseract or heuristic OCR), tracks cursor and builds an action timeline before editing decisions |
| **Content-aware 9:16** | 8-factor scoring function: keep important text, avoid slicing headlines, smoothed camera pan, canvas vs fill framing |
| **AI Voice & Timing** | Piper (offline), Kokoro, Edge, eSpeak NG - narration synthesised per-segment and timeline-fitted to the Reel duration |
| **Smart Editor** | Dead-time removal (screen and audio idle), action-based cuts, result hold, dynamic zooms, cursor emphasis, click rings |
| **Operations & CLI** | `contentforge inspect`, `quality`, `music`, `canva`, `templates`, `doctor`, and Streamlit dashboard |

## Quick start (Fedora)

```bash
git clone https://github.com/muhammadanas20/ContentForge-AI && cd ContentForge-AI
scripts/install_fedora.sh          # ffmpeg (RPM Fusion), fonts, venv, python deps
source .venv/bin/activate
contentforge doctor                # verify ffmpeg / whisper / TTS / disk / Gemini
contentforge run                   # start watching data/input  (Ctrl-C to stop)
# in another terminal:
contentforge dashboard             # http://localhost:8501
```

Now record your screen (OBS, GNOME Screen Recorder, SimpleScreenRecorder...) for 20-60 s while you talk
through the website, save it as `data/input/<website.com> - <topic>.mp4`, and watch the dashboard.
Naming the file after the website (e.g. `smallpdf.com - merge.mp4`) lets the script and caption mention it.

Manual alternatives:

```bash
contentforge process data/input/demo.mp4 --website smallpdf.com --preset ai_tool  # one file with preset
contentforge inspect <job_id>                                                     # inspect artifacts & plan
contentforge quality <job_id>                                                     # check quality gates & score
contentforge retry <job_id> --from subtitles                                      # re-render after changing styles
contentforge jobs --status failed                                                 # what went wrong
contentforge cleanup --dry-run                                                    # what cleanup would delete
contentforge analytics add <job_id> --views 1200 --likes 90 --completion 58
contentforge analytics report
```

## Configuration

Everything lives in [`config/config.yaml`](config/config.yaml) (validated by Pydantic - typos fail fast).
Override locally with `config/config.local.yaml` (git-ignored) or environment variables such as
`CONTENTFORGE__TTS__ENGINE=edge`. Secrets go in `.env` (see `.env.example`) - never in YAML.

Commonly tuned keys:

```yaml
pipeline.mode: smart | classic           # v0.4 Reel pipeline (default) or the v0.2 flow
preset: "student_reel"                   # student_reel, ai_tool, coding, productivity...
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
| [docs/VISION_V04.md](docs/VISION_V04.md) | **v0.4 Vision**: The premium AI creative production engine |
| [docs/CREATIVE_ENGINE.md](docs/CREATIVE_ENGINE.md) | **Creative Engine**: Creative Director, presets, multi-candidate hooks |
| [docs/ARCHITECTURE_AUDIT.md](docs/ARCHITECTURE_AUDIT.md) | Subsystem architecture audit (KEEP/CHANGE/REMOVE/ADD) |
| [docs/EDITING_ENGINE.md](docs/EDITING_ENGINE.md) | Smart editor cuts, zooms, safe-margins, framing |
| [docs/QUALITY_SYSTEM.md](docs/QUALITY_SYSTEM.md) | Technical quality gates & 10-dimension creative QA scoring |
| [docs/MUSIC_SYSTEM.md](docs/MUSIC_SYSTEM.md) | Music catalog, licensing rules, mood matching & SFX library |
| [docs/CANVA_INTEGRATION.md](docs/CANVA_INTEGRATION.md) | Canva Connect API integration, OAuth & Pillow fallback |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | CLI guide (`inspect`, `quality`, `music`, `canva`, `templates`) |
| [docs/installation.md](docs/installation.md) | Fedora / generic Linux setup, Python env, models, optional Kokoro/Edge/Ollama, systemd |
| [docs/configuration.md](docs/configuration.md) | Every YAML key, env overrides, `.env` |
| [docs/smart-pipeline.md](docs/smart-pipeline.md) | Core pipeline architecture and stage execution |
| [docs/testing.md](docs/testing.md) | Running and writing tests; unit, integration & real-model |
| [ROADMAP.md](ROADMAP.md) | Status and roadmap |

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

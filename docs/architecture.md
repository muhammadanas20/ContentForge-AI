# Architecture

## Design goals

1. **Zero-touch** - the only human action is dropping a recording into a folder.
2. **Resumable** - every step persists its outputs; a crash or a config change never forces a full re-run.
3. **Offline-first** - Whisper, Piper, OpenCV, FFmpeg and the rule-based writers need no network. LLMs and Edge TTS are optional upgrades.
4. **Composable** - each stage is a class with one job; the runner only knows the step list.
5. **Boring infrastructure** - SQLite, YAML, subprocess-driven FFmpeg. Nothing exotic to operate on a laptop.

## Module map

```
                 ┌──────────────┐        ┌──────────────┐
  data/input ───▶│ input.watcher│──path─▶│   app.py     │◀──── cli.py / dashboard
                 └──────────────┘        │ (façade)     │
                                         └──────┬───────┘
                                                │ create_job / run
                                         ┌──────▼───────┐
                                         │ pipeline.    │  JobContext (state.json)
                                         │ runner       │  retry + resume + skip
                                         └──────┬───────┘
      ┌──────────┬──────────┬────────────┬──────┴─────┬───────────┬──────────┬──────────┐
      ▼          ▼          ▼            ▼            ▼           ▼          ▼          ▼
  processing   ai.*      subtitles   thumbnails   ai.social   output    archive    cleanup
  (ffmpeg,   (whisper,   (captions,  (Pillow,     (caption,  (package  (move raw) (safe rm)
   opencv)    script,     ASS)        brief)       hashtags)  folder)
              tts)
      └──────────┴──────────┴────────────┴────────────┴───────────┴──────────┴──────────┘
                                                │
                                         ┌──────▼───────┐
                                         │    db.py     │  jobs · job_steps · metrics · hashtag_history · events
                                         └──────────────┘
                                         scheduling (APScheduler): cleanup · reports · health · deferred queue
```

| Package | Responsibility | Key classes |
|---|---|---|
| `config` | Load YAML + local override + env, validate | `Settings`, `load_settings` |
| `log` | Rich console, dated files, pruning | `setup_logging`, `DailyFileHandler` |
| `utils` | FFmpeg/ffprobe wrapper, fs helpers, retry, time/colour | `FFmpeg`, `retry`, `wait_until_stable` |
| `db` | Persistence (SQLite default, PostgreSQL optional) | `Database` |
| `input` | Detect new recordings, wait for stability, queue | `InputWatcher` |
| `ai.transcriber` | Faster-Whisper → `Transcript` (words, segments, SRT/TXT/JSON) | `Transcriber`, `Transcript` |
| `ai.llm` | Optional OpenAI/Ollama chat client; returns `None` on any failure | `LLMClient` |
| `ai.script_writer` | Transcript → hook/body/CTA script; grounding guard | `ScriptWriter`, `Script` |
| `ai.tts` | Piper / Kokoro / Edge behind `TTSEngine`; sentence timings | `get_tts_engine` |
| `ai.social` | Caption, CTA, comment prompt, SEO, hashtags with rotation | `SocialWriter`, `HashtagGenerator` |
| `processing.segments` | Pure edit-decision maths: silences → keep-list, `Timeline` remap | `build_keep_ranges`, `Timeline` |
| `processing.analysis` | OpenCV motion + activity centroid sampling | `analyse_video` |
| `processing.video_editor` | Two-pass FFmpeg render: cut/crop/zoom, then overlays+mux | `VideoEditor`, `plan_crop` |
| `processing.audio_mixer` | Cut original audio, mix narration/original/music, loudnorm | `AudioMixer` |
| `subtitles` | Word timings → caption chunks → ASS (subs + progress bar + branding + cards) | `build_captions`, `AssRenderer` |
| `thumbnails` | Frame → branded cover; Canva brief | `ThumbnailGenerator` |
| `analytics` | Metrics, composite score, suggestions, reports | `AnalyticsService` |
| `output` | Upload package folder + manifest + README | `UploadPackager` |
| `archive` | Dated archive of raw recordings | `Archiver` |
| `cleanup` | Retention + free-space floor with guards | `Cleaner` |
| `scheduling` | APScheduler wrapper | `ForgeScheduler` |
| `pipeline` | `JobContext`, `Step` classes, `PipelineRunner` | |
| `dashboard` | Streamlit UI | |
| `app` / `cli` | Façade and command line | `ContentForgeApp` |

## Data flow of one job

1. **Watcher** sees `input/x.mp4`, waits until its size is stable, calls `app.process_file`.
2. **Dedupe** - SHA-1 of the first 64 MB; identical content already processed → skipped, failed → resumed.
3. **JobContext** created: `work/<slug>/state.json`, DB row `jobs` (status `queued`).
4. **Runner** iterates `DEFAULT_STEPS`; each step gets `retries` attempts with backoff. After success:
   `state.json` updated, `job_steps` row written. On failure: job `failed`, error stored, everything before it kept.
5. **Package** copies deliverables to `output/<slug>/`; **archive** moves the raw file to `archive/YYYY-MM/<slug>/`;
   **cleanup_work** empties the work dir (state.json kept for traceability).

## Timing model

Three clocks exist and the code is explicit about which one it uses:

* **Source time** - the original recording. Whisper timestamps and silence intervals are here.
* **Output time** - after cuts. `Timeline.to_output()` maps source → output; captions from original speech are remapped this way.
* **Final time** - after the video is retimed (`setpts`) or extended (`tpad`) to match narration length.
  Narration sentence timings are natively in this clock, so narration captions need no remap.

## Why these technical choices

* **ASS via libass for every overlay** - subtitles, per-word highlight, animated progress bar (a vector rectangle with an
  animated `\fscx`), watermark, hook/CTA cards all render in one filter. `drawtext`/`drawbox` are not compiled into
  every ffmpeg and are far harder to animate.
* **`zoompan` for zoom pulses** - the only stock filter that evaluates per-frame expressions for zoom; `crop` cannot use `t`.
* **Fades applied per segment before `concat`** - a fade-out on the concatenated stream leaks black frames into the
  next segment (found by a test); per-segment fades are exact.
* **Retime vs freeze** - if narration is within 0.75-1.30× of the cut video, the video is gently sped/slowed; if much
  longer, the last frame is held; if much shorter, the video keeps its length and narration ends early.
* **Rule-based generators as the baseline** - deterministic, testable, no cost; LLM output only replaces them when it
  parses and passes the grounding ratio.
* **SQLite in WAL mode** - concurrent reads from the dashboard while a worker writes; PostgreSQL via the same DAO.

## Extending

* **New step** - subclass `pipeline.steps.Step`, set `name`, implement `run(ctx)`, insert into `DEFAULT_STEPS`. Add the
  artefact key to `PipelineRunner._artifacts_present` if resume should verify it.
* **New TTS engine** - subclass `ai.tts.base.TTSEngine`, register in `ai.tts.factory`.
* **New subtitle style** - extend `SubtitleConfig.style` literal and `AssRenderer._styles/caption_events`.
* **Publishing integration** - consume `output/<slug>/manifest.json` and `social.json`; use official APIs only.

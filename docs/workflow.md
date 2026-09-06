# Workflow

## Human workflow (the only manual part)

1. Find a website worth showing students.
2. Record the screen for 20-60 s while talking through it (any recorder; 16:9 is fine - it will be cropped to 9:16).
3. Save it as `data/input/<website.com> - <topic>.mp4` (naming with the domain lets the script/caption mention it).
4. Later: open `data/output/<slug>/`, upload the MP4, paste `caption.txt`, optionally use `cover.jpg`.
5. After ~48 h, enter views/likes/… in the dashboard **Analytics** tab.

## Automated pipeline steps

| # | Step | Input → Output | Notes |
|---|---|---|---|
| 1 | `probe` | source → media info | Rejects files without video, < 0.5 s or > 3× `max_duration_seconds` |
| 2 | `extract_audio` | source → `source_audio.wav` (16 kHz mono) | Silent track synthesised if the recording has no audio |
| 3 | `transcribe` | wav → `transcript.txt/.srt/.json` | Faster-Whisper with word timestamps + VAD |
| 4 | `script` | transcript → `script.json/.md` | Hook + steps + CTA, keywords, title; LLM optional |
| 5 | `narration` | script → `narration.wav` + sentence timings | Piper/Kokoro/Edge; on failure the original audio is used |
| 6 | `analyse` | wav + frames → edit plan | Silence detection, motion sampling, keep-list, crop centre, duration cap |
| 7 | `render_cut` | source → `cut.mp4` (silent, 9:16) | trim/concat, crop, `zoompan` pulses, per-segment fades |
| 8 | `sync_audio` | cut + narration → `synced.mp4`, `mix.wav` | Retime/freeze to narration length; mix + loudnorm |
| 9 | `subtitles` | timings → `subtitles.srt/.json/.txt`, `overlay.ass` | Captions + progress bar + watermark + hook/CTA cards |
| 10 | `render_final` | synced + mix + ass → `final.mp4` | libx264, faststart; optional PNG logo overlay |
| 11 | `thumbnail` | frame → `thumbnail.jpg`, `thumbnail_brief.md/.json` | Pillow render + Canva brief |
| 12 | `social` | script → `social.json`, `caption.md` | Caption, CTA, comment prompt, hashtags (rotation-aware), SEO, YT title |
| 13 | `package` | everything → `output/<slug>/` + `manifest.json` + `README.md` | |
| 14 | `analytics` | — | Registers zero-baseline metrics per platform |
| 15 | `archive` | source → `archive/YYYY-MM/<slug>/` | Moves raw recording, copies transcript/script |
| 16 | `cleanup_work` | work dir emptied | Only `state.json` remains |

Disable any stage in `pipeline.steps`. Disabled steps are recorded as `skipped`.

## Resume & retry semantics

* Each step is retried `pipeline.retries` times with exponential backoff before the job is marked `failed`.
* Completed steps are recorded in `work/<slug>/state.json` and the `job_steps` table.
* `contentforge retry <job_id>` (or the dashboard **Errors → Retry**) resumes from the failed step; earlier artefacts are reused.
* `contentforge retry <job_id> --from subtitles` re-runs from a chosen step (e.g. after changing `subtitles.style`).
* On daemon start, jobs left in `processing`/`queued` are resumed automatically (`resume_incomplete`).
* If a required artefact was deleted, the step is re-run even if it was marked done.
* Dropping a file with identical content again is ignored (SHA-1 dedupe) unless `--force`.

## Output package

```
output/<slug>/
  <slug>.mp4            1080x1920 H.264 + AAC, -14 LUFS
  cover.jpg             thumbnail
  caption.txt           Instagram caption + CTA + comment prompt + hashtags (paste as-is)
  caption.md            caption, YouTube title, SEO description, CTA, prompt, hashtags
  social.json           same, machine-readable
  script.md / .json     hook, body, CTA, keywords, estimated seconds
  subtitles.srt/.ass/.json/.txt
  transcript.txt/.srt/.json
  narration.wav         raw TTS track (for re-edits)
  thumbnail_brief.md/.json   Canva instructions, text options, AI image prompt, colours
  manifest.json         everything + edit plan + sync method + settings snapshot
  README.md             upload checklist
```

## Scheduled vs immediate

`scheduler.mode: immediate` (default) processes each file as soon as it is stable.
`scheduler.mode: scheduled` collects files and processes them at `process_cron` (e.g. overnight on a slow laptop).
Cleanup, weekly analytics and monthly reports run on their crons in both modes. See `docs/scheduling.md`.

# Configuration reference

All configuration is in `config/config.yaml`. It is validated by the Pydantic models in
`contentforge/config/schema.py`; unknown keys or wrong types abort start-up with a clear message.

## Override layers (later wins)

1. Built-in defaults (schema).
2. `config/config.yaml`.
3. The active **preset** (`preset: student_reel` → `presets.student_reel`, a named partial config deep-merged on top).
   Select with `preset:` in YAML, `CONTENTFORGE_PRESET=<name>` or `contentforge --preset <name>`; `none` disables.
   Unknown names are a hard error. Shipped presets: `student_reel` (production look), `fast_preview` (720p, fast, tiny Whisper).
4. `config/config.local.yaml` - optional, git-ignored, deep-merged. Ideal for machine-specific tweaks (may also define presets).
5. Environment variables `CONTENTFORGE__SECTION__KEY=value` (double underscore = nesting, values YAML-coerced).
   Example: `CONTENTFORGE__TTS__ENGINE=edge`, `CONTENTFORGE__VIDEO__CRF=18`, `CONTENTFORGE__WATCHER__ENABLED=false`.
6. `CONTENTFORGE_CONFIG=/path/to/other.yaml` selects a different file; `CONTENTFORGE_DATA_DIR=/mnt/media` relocates every
   `data/...` path at once.

Inspect the effective result: `contentforge config` or `contentforge config video`.

## Secrets (`.env`)

Never put keys into YAML. Copy `.env.example` to `.env`:

| Variable | Purpose |
|---|---|
| `CONTENTFORGE_LLM_PROVIDER` | `none` (default, offline) / `openai` / `ollama` |
| `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_BASE_URL` | OpenAI-compatible endpoint |
| `OLLAMA_BASE_URL`, `OLLAMA_MODEL` | Local Ollama |
| `CONTENTFORGE_DATABASE_URL` | PostgreSQL URL (empty = SQLite) |
| `CONTENTFORGE_LOG_LEVEL` | Overrides `logging.level` |
| `FFMPEG_BINARY`, `FFPROBE_BINARY` | Explicit binary paths |

## Sections

### `project`
`name`, `brand` (used in CTA/watermark/hashtags), `website`, `timezone` (scheduler cron timezone, e.g. `Asia/Karachi`).

### `paths`
`data_dir`, `input`, `work`, `output`, `archive`, `logs`, `assets`, `models`, `db`. Relative paths are anchored at the repo root.

### `watcher`
| Key | Default | Meaning |
|---|---|---|
| `enabled` | true | Start the folder watcher in `contentforge run` |
| `extensions` | `.mp4 .mkv .mov .webm .avi` | Files considered recordings |
| `stable_seconds` | 5 | File size must be unchanged this long before processing (recorder finished) |
| `poll_interval` | 2 | Seconds between size checks |
| `process_existing_on_start` | true | Queue files already in `input/` at start-up |
| `recursive` | false | Watch sub-folders |

### `pipeline`
`mode` (**`smart`** = v0.3 understanding-driven Reel pipeline, `classic` = v0.2 transcript-driven pipeline),
`max_workers` (parallel videos; 1 recommended on laptops), `retries` (per step), `retry_backoff_seconds`,
`cleanup_work_on_success`, `delete_intermediates_early` (drop `cut.mp4`/`synced.mp4` as soon as they are superseded),
`min_free_disk_gb_to_start` (refuse to start a job below this much free space; 0 = off), and `steps.*` boolean switches:
smart - `understand, plan_edit, compose, captions, mix, cover, quality`;
shared/classic - `transcribe, script, narration, subtitles, silence_removal, jump_cuts, vertical_crop, auto_zoom,
progress_bar, branding, thumbnail, social, analytics, package, archive`.

### `understanding` *(v0.3)*
What the pipeline sees before it edits. `sample_fps` (6; the main CPU knob - 4 is faster, 8 catches more clicks),
`work_width` (960; analysis resolution), `ocr_every_seconds` (1.0), `ocr_engine` (`auto|tesseract|heuristic`),
`ocr_languages`, `max_frames` (hard cap for long recordings), `track_cursor`, `website_url` / `website_context`
(grounding metadata; per job use `contentforge process ... --website studentofferco.com`).
Without tesseract the heuristic backend returns text *regions* only - framing and the quality gates still work.

### `framing` *(v0.3)*
Content-aware 9:16 composition. `target_width/height`, `max_zoom` (3.6 hard cap on `source_width / view_width`),
`canvas_bias` (how much the designed canvas layout may lose by before a destructive crop is chosen),
`focus_boost` / `focus_falloff` (importance of text near the current action), `samples_per_shot`,
`smoothing_deadzone` / `smoothing_ema` / `max_pan_per_second` (camera path), and `weights.*`
(`text_kept, text_cut, cursor, action, prominence, content, legibility, zoom_penalty`).
Raise `weights.text_cut` if headlines ever get sliced; lower `canvas_bias` for tighter, more zoomed framing.

### `editing` *(v0.3)*
`max_duration` (Reel budget), `min_shot` / `max_shot`, `remove_dead_time`, `min_dead_gap`, `dead_padding`,
`max_speedup`, `dynamic_zoom` + `zoom_max` (1.07 = subtle push-in), the structure knobs
`hook_seconds / setup_seconds / cta_seconds / payoff_fraction`, `result_hold` (hold on the result after it appears),
`transitions`, `click_effects`, `click_sfx`, `card_offset`, `zoom_headroom`.

### `reel_captions` *(v0.3)*
Burned-in Reel captions: `font`, `font_size`, `outline`, `max_words` / `max_chars` per chunk, `highlight_color`,
`text_color`, `safe_bottom` / `safe_top` (mobile UI safe areas), `word_level` (Whisper word timing when available,
estimated otherwise), `hook_card`, `progress_bar`, `click_rings`.

### `cover` *(v0.3)*
`width`, `height`, `title_size`, `max_title_words`, `concepts` (`card`, `banner`, `split` - all are rendered
internally and the best-measuring one is kept).

### `quality` *(v0.3)*
`enabled`, `block_on_error` (a failing Reel is never packaged), `min_text_keep`, `min_narration_coverage`,
`narration_slack_seconds`, `min_duration` / `max_duration`, `max_peak_db`, `min_mean_db`.

### `audio`
* `sample_rate`, `channels` - ASR extraction format.
* `silence.threshold_db` (-35), `silence.min_duration` (0.6 s), `silence.keep_padding` (0.15 s) - what counts as dead air.
* `loudness.target_lufs` (-14), `true_peak` (-1.5), `lra` (11) - EBU R128 normalisation of the final mix.
* `mix.original_volume` (0 = mute screen audio when narration exists), `narration_volume`, `music_volume`, `background_music` (optional file).

### `transcription`
`model_size` (`tiny|base|small|medium|large-v3`), `device` (`cpu|cuda`), `compute_type` (`int8|float16|float32`),
`language` (`en`, or `null` to auto-detect - Urdu/English mixes work best with `small`+auto), `beam_size`, `vad_filter`,
`word_timestamps`, `download_root`.

### `script`
`target_words_min/max` (60-140 ≈ 25-55 s of speech), `max_duration_seconds`, `hook_styles`
(`question, shock, problem, secret, curiosity` - one is picked at random), `cta_default`, `speaking_rate_wps`,
`strict_grounding` (reject LLM scripts that introduce too many words absent from the transcript).

### `tts`
`engine` (`piper|kokoro|edge|espeak`), `speed`, `output_sample_rate`, plus per-engine blocks:
`piper.voice`, `piper.models_dir`, `piper.auto_download`, `piper.length_scale/noise_scale/noise_w`;
`kokoro.voice`, `kokoro.lang_code`; `edge.voice`, `edge.rate`, `edge.pitch`;
`espeak.voice`, `espeak.words_per_minute` (offline fallback through `libespeak-ng`, no model download).
If the configured engine is unavailable the next available one is used; if none is, the original audio is kept.

### `video`
* Output: `width` 1080, `height` 1920, `fps` 30, `crf` 20, `preset` medium, `pix_fmt`, `audio_bitrate`, `max_duration_seconds` 90.
* `crop.mode`: `smart` (follow on-screen activity), `center`, `left`, `right`; `sample_fps`, `smoothing`.
* `crop.follow_cursor` (only with `smart`): `enabled`, `sample_fps` 10 (tracker rate), `work_width` 960 (analysis
  resolution), `min_size_px`/`max_size_px` (cursor bounding box at source scale), `deadzone` 0.3 (fraction of the window
  in which the pointer may move without panning), `smoothing` 0.8 (0 = instant … 0.95 = very lazy), `max_speed` 1.5
  (frame-widths per second), `min_detections` 8 and `min_coverage` 0.15 (below either → static smart crop),
  `keyframe_tolerance`, `snap_gap_seconds` 1.0 (a cut removing ≥ this re-centres instead of panning).
  Reported in `manifest.json` → `crop.mode: cursor|smart`, `dynamic`, `x_min/x_max`.
* `zoom`: `enabled`, `max_zoom` 1.12, `interval_seconds` 6, `duration_seconds` 2.5, `ease`. With narration, pulses align to sentence starts.
* `jump_cuts`: `enabled`, `min_gap_seconds` 1.2, `motion_threshold` 2.0 (mean pixel change per sample; lower = stricter).
* `transitions`: `enabled`, `type` fade|none, `duration` 0.08.
* `progress_bar`: `enabled`, `height`, `color`, `background` (8-digit hex = alpha), `position` top|bottom.
* `branding`: `watermark_text`, `watermark_position`, `watermark_opacity`, `font_size`, `logo_path` (PNG in `data/assets`),
  `logo_width`, `intro_title` (hook card), `outro_cta` (CTA card), `intro_seconds` 2.2, `outro_seconds` 2.5,
  `primary_color`, `secondary_color`, `background_color`.

### `subtitles`
`enabled`, `style` (`bold-pop|clean|karaoke|minimal`), `font` (fontconfig family name), `font_size`, `max_chars_per_line`,
`max_words_per_caption`, `position_v` (0 top … 1 bottom), `primary_color`, `highlight_color`, `outline_color`,
`outline_width`, `shadow`, `highlight_keywords`, `uppercase`, `formats` (`srt ass json txt`).

`word_level` - Whisper is run on the **narration WAV** (`word_timestamps=True`) and the script words are aligned to it
so every caption word appears when it is actually spoken (all four styles benefit; `karaoke` sweeps per word).
`enabled`, `min_match_ratio` 0.6 (fewer script words matched → sentence-level fallback), `min_word_seconds` 0.06.
The step output shows `word_alignment: 91%` / `fallback-sentence` / `disabled`.

### `thumbnail`
`enabled`, `width`, `height`, `frame_position` (fraction of video used as background), `overlay_title`, `font` (TTF path),
`title_font_size`, `dark_overlay_alpha`, `accent_color`, `text_color`, `canva_brief`.

### `social`
`platform_defaults` (analytics baselines), `caption_max_length` 2200, `hashtags.count`, `hashtags.categories`
(`brand students pakistan productivity tech education viral ai`), `hashtags.banned`, `hashtags.rotation_memory`
(how many previous sets to avoid repeating), `cta_variants`, `comment_prompts`.

### `analytics`
`enabled`, `benchmarks.*` (thresholds for suggestions), `report_dir`.

### `cleanup`
| Key | Default | Meaning |
|---|---|---|
| `delete_raw_after_success` | false | Remove raw recordings left in `input/` after success (archive step already moves them) |
| `delete_temp_after_success` | true | Remove work dirs of finished jobs |
| `archive_retention_days` | 30 | Delete archived raw recordings older than this (0 = keep) |
| `output_retention_days` | 0 | Delete upload packages older than this (0 = keep forever) |
| `min_free_disk_gb` | 5 | Below this, oldest archives then outputs are reclaimed |
| `min_age_minutes` | 30 | Never touch anything modified more recently |
| `logs_retention_days` | 30 | Dated log files |

Unfinished work (`processing`/`queued` jobs, recently modified folders) is never deleted.

### `scheduler`
`enabled`, `mode` (`immediate` = process as soon as a file is stable; `scheduled` = collect and process on `process_cron`),
`process_cron`, `daily_cleanup_cron`, `weekly_analytics_cron`, `monthly_report_cron` (5-field crontab syntax).

### `database`, `logging`, `dashboard`
`database.backend` sqlite|postgres; `logging.level/console/rich_tracebacks/file_rotation/retention_days/json_lines`;
`dashboard.host/port/refresh_seconds/log_tail_lines`.

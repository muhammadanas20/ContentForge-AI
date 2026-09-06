# The smart pipeline (v0.3)

> Understanding first, editing second, rendering last.

v0.2 treated a screen recording as *audio with pictures*: transcribe, write a
script from the transcript, then crop the video around the mouse pointer. That
produces a technically valid 9:16 file whose content is frequently unreadable -
half a headline, a button sliced down the middle, narration that talks about
nothing you can see.

v0.3 inverts the flow. The pipeline **watches the recording first**, builds an
action timeline, plans an edit from that timeline, and only then writes
narration - which is bound, segment by segment, to the visual moment it
describes. Nothing is packaged until twelve quality gates agree that the result
is postable.

```
probe → extract_audio → understand → transcribe → plan_edit → script → narration
      → compose → captions → mix → render_final → cover → social → quality
      → package → analytics → archive → cleanup_work
```

Select it with `pipeline.mode: smart` (the shipped default). `pipeline.mode:
classic` restores the complete v0.2 flow, which is untouched.

---

## 1. `understand` - watching the recording

`contentforge/processing/video_understanding.py`

One OpenCV decode pass samples frames at `understanding.sample_fps` (default 6)
at `work_width` pixels wide, and for each sampled frame records:

| Signal | How | Used by |
|---|---|---|
| text / UI boxes | OCR every `ocr_every_seconds` (tesseract when installed, otherwise an OpenCV morphology text-region detector) | framing, script, quality gates |
| visual change | frame differencing → bounding box + area + motion score | actions, cover scoring |
| cursor position | the v0.2 blob tracker (`processing/cursor.py`) | framing, click detection, gates |
| scene change | large-area change + high motion | shot boundaries |

Those observations are then turned into an **action timeline**:

| Action | Detection rule (summarised) |
|---|---|
| `click` | small, wide change with the pointer inside it, pointer arriving fast and settling; narrow "slivers" (pointer trails) rejected; 0.6 s debounce |
| `scroll` | consistent vertical displacement of the page content across several frames |
| `type` | a run of tiny changes sharing one text baseline while the pointer stays parked |
| `scroll`/`navigate`/`reveal` | significance-ranked large changes; `reveal` when it follows a click, `navigate` otherwise |
| `idle` | motion below the idle threshold for at least 0.8 s |

Everything is serialised to `understanding.json` so a job can resume without
re-analysing, and `understanding.summary()` goes into the job record.

**OCR is optional by design.** Without tesseract the heuristic backend still
returns boxes (`ocr_backend: heuristic`), so framing, caption placement and the
"important text preserved" gate keep working - only literal strings are missing,
and the script planner then describes *actions* instead of quoting text.

## 2. `plan_edit` - deciding the edit

`contentforge/processing/editor.py` + `processing/framing.py`

1. **Keep ranges** - dead time is removed only where the screen is idle *and*
   the audio is silent (`min_dead_gap`, `dead_padding`). The moment right after
   a result appears is protected by `editing.result_hold`.
2. **Budget** - if the material still exceeds `editing.max_duration`, the plan
   speeds up (≤ `max_speedup`) and then drops the least interesting ranges.
3. **Cuts** - shot boundaries are placed on action starts, clamped to
   `min_shot`/`max_shot`, and split on the structural boundaries so hook and CTA
   really exist.
4. **Roles** - the viral structure (0-2 s hook, setup, demo, payoff at 80 % or
   at the strongest reveal, final CTA) is mapped onto *actual* shot boundaries,
   never forced blindly.
5. **Framing** - for every shot, several sample times are scored over a grid of
   candidate views:

   ```
   score =  1.0·text_kept − 2.0·text_cut + 0.5·cursor + 0.9·action
          + 0.6·prominence + 0.35·content + 1.2·legibility − 0.25·zoom
   ```

   * `text_cut` peaks when a text box is ~50 % inside the view: a sliced
     headline is worse than an absent one, which is what stops the classic
     "half a word" crop.
   * `legibility` rewards views that end up at ≥ 1:1 pixel scale on a 1080-wide
     canvas; `zoom` penalises tightness; `framing.max_zoom` is a hard cap.
   * Candidates include both **fill** (9:16 crop) and **canvas** layouts (the
     16:9 recording scaled into a designed 9:16 composition). When the page
     cannot survive a crop, the canvas wins - `canvas_bias` sets how much it
     may lose by first.
   * Text near the current action is weighted up (`focus_boost`), so a tight
     framing can win *when it does not destroy context*.

   The per-sample winners are then smoothed into a camera path (dead zone, EMA,
   `max_pan_per_second`), so the frame never jitters.
6. **Zooms & emphasis** - subtle push-ins on hooks/clicks (`zoom_max`, default
   1.07), pull-outs on the payoff, and click positions remapped into output time
   for the on-screen rings.

## 3. `script` - grounded narration

`contentforge/ai/script_writer.py` (`GroundedScriptPlanner`)

Shots are grouped into **beats**; every beat produces one line whose facts can
only come from: OCR strings actually seen, action labels, the website name, and
the operator-supplied `--website` / `website_context`. Each segment carries
`{start, end, text, role, visual_action, focus_region, evidence, source_start,
source_end}` - i.e. it is *provably* tied to a visual moment, which the quality
gate re-checks.

Lines are chosen from several phrasings so that they fit the shot's spoken-word
budget; the CTA is never truncated. With no understanding available the planner
falls back to a short grounded-minimal script (marked `degraded`) instead of
inventing a feature list. An optional LLM pass may rephrase lines, but a line
that introduces unknown content words is discarded.

## 4. `narration` - fitted to the timeline

`contentforge/ai/narration.py`

Each segment is synthesised separately, gently time-fitted with `atempo`
(≤ 1.22×), and placed at its slot start on a silent bed. The narration is
therefore exactly as long as the Reel - the "13-second voice-over on a
76-second video" failure mode is structurally impossible. Any
`TTSEngine` works; eSpeak NG (`ai/tts/espeak_engine.py`, ctypes, no model
download) is the last-resort engine so narration is never silently skipped.

## 5. `compose`, `captions`, `mix`, `render_final`

* **compose** renders one file per shot (crop → layout → eased `zoompan`) and
  concatenates them with stream copy. In the canvas layout the zoom happens
  *inside the card*, so the composition and its accent border never get clipped.
* **captions** plans word-highlighted chunks (≤ 4 words), picks a band that does
  not cover the focus region, and adds click rings, watermark, progress bar and
  hook/CTA cards. Long words shrink instead of running off-screen.
* **mix** puts narration on top, ducks music under it (`sidechaincompress`), adds
  a synthesised click tick per click, then limits and loudness-normalises.
* **render_final** burns the overlays and muxes the audio in a single pass.

## 6. `cover` and `quality`

`media/cover.py` scores every sampled frame (result reveals, text density,
stillness, position, whether the frame is even in the edit), renders three cover
concepts, and keeps the one that measures best - a branded 1080x1920 JPEG.

`pipeline/quality.py` then runs the gates:

| Gate | Blocking |
|---|---|
| output file exists / decodes | yes |
| 9:16 resolution | yes |
| duration inside the window | yes |
| audio track present | yes |
| important OCR regions preserved (`min_text_keep`) | yes |
| narration exists | yes |
| narration covers the timeline / matches its duration | yes |
| captions exist / fit the safe width | yes |
| cover is a 1080x1920 image | yes |
| script segments all map to visuals | yes (warning when degraded) |
| cursor visible at click moments | warning |
| audio peak / loudness | warning |
| work directory clean | warning |

The report is written as `quality.json` + `quality.md` and shipped inside the
upload package. With `quality.block_on_error: true` a failing Reel is never
packaged - the job fails loudly instead of publishing something broken.

## Performance

Measured on the CI sandbox (2 vCPU, no GPU) for a 24 s 1280x720 recording:

| Stage | Time |
|---|---|
| understand (6 fps, OCR 1 s) | ~4 s |
| plan_edit (framing search) | ~6 s |
| compose (7 shots, 1080x1920) | ~20 s |
| captions + mix + burn-in | ~8 s |
| cover + quality + package | ~1.5 s |
| **total** | **~40 s** |

Tuning knobs, in order of impact: `understanding.sample_fps`,
`understanding.ocr_every_seconds`, `framing.samples_per_shot`, `video.preset`.
The `fast_preview` preset sets all of them low and renders 720x1280.

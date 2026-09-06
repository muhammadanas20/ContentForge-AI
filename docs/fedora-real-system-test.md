# Fedora real-system test guide (low-RAM / low-disk laptop)

This is the exact procedure to verify ContentForge-AI **with real models** on a Fedora
laptop. Everything the automated test-suite cannot prove in a sandbox (Whisper accuracy,
Piper voice quality, word-level caption timing on real speech, cursor tracking on a real
GNOME/OBS recording) is verified here, step by step, with the command to run and what
you should see.

Budget: ~2.5 GB disk (venv 1.2 GB, Whisper `base` 150 MB, Piper voice 65 MB, scratch
≤ 600 MB per job), ~1.5 GB RAM peak for a 60 s 1080p job with `base`/`int8`.

---

## 0. Before you start (5 min)

```bash
# CPU, RAM and disk you have to work with
nproc; free -h; df -h ~

# Fedora version (39–42 tested by the install script)
cat /etc/fedora-release
```

If `df -h ~` shows less than **4 GB** free, clean first (`dnf clean all`, `journalctl --vacuum-size=200M`,
`pip cache purge`) — the pipeline refuses to start a job below `pipeline.min_free_disk_gb_to_start`
(1 GB by default) and cleanup kicks in below `cleanup.min_free_disk_gb` (5 GB by default;
lower it in `config/config.local.yaml` if your disk is small, see §8).

## 1. System packages

```bash
# RPM Fusion (free) is required for a full ffmpeg with libx264/libass
sudo dnf install -y "https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm"
sudo dnf install -y --allowerasing ffmpeg python3 python3-pip python3-devel gcc \
     dejavu-sans-fonts dejavu-sans-mono-fonts liberation-sans-fonts fontconfig inotify-tools

# Verify: must list libass, libx264 and drawtext support
ffmpeg -hide_banner -version | head -1
ffmpeg -hide_banner -buildconf | grep -E "enable-(libass|libx264|libfreetype)"
ffmpeg -hide_banner -filters | grep -E "^ ... (ass|zoompan|loudnorm|silencedetect) "
```

Expected: ffmpeg ≥ 6.0, four `--enable-…` lines, and four filter lines. If `ass` is missing you have the
Fedora-native (crippled) ffmpeg — re-run the `dnf install --allowerasing ffmpeg` line.

## 2. Python environment

```bash
git clone https://github.com/muhammadanas20/ContentForge-AI && cd ContentForge-AI
python3 -m venv .venv && source .venv/bin/activate
pip install --upgrade pip wheel
pip install -r requirements.txt
pip install -e ".[dev]"
cp -n .env.example .env
contentforge --version          # contentforge 0.2.0
contentforge doctor             # ✔ ffmpeg, ✔ fonts, ✔ disk … TTS "piper: model missing" is OK for now
```

Low-RAM tip: `pip install` of `torch` is **not** needed (Piper and Faster-Whisper are ONNX/CTranslate2). Do not
install the Kokoro extra unless you have ≥ 8 GB RAM.

## 3. Faster-Whisper model (one-time, needs internet)

```bash
source .venv/bin/activate
# base/int8 = 150 MB, ~1× real-time on 2 cores. Use "tiny" on very weak machines, "small" if you have 8 GB RAM.
python - <<'EOF'
from faster_whisper import WhisperModel
m = WhisperModel("base", device="cpu", compute_type="int8", download_root="data/models/whisper")
print("ok, model dir:", m.model_size_or_path if hasattr(m, "model_size_or_path") else "data/models/whisper")
EOF
du -sh data/models/whisper
```

Expected: a `models--Systran--faster-whisper-base` folder of ~150 MB, no error. If your laptop is offline
afterwards this cache is all that is needed.

## 4. Piper voice (one-time, needs internet)

```bash
source .venv/bin/activate
python - <<'EOF'
from contentforge.config import load_settings
from contentforge.ai.tts import PiperTTS
tts = PiperTTS(load_settings().tts.piper)
tts.ensure_model()
print("voice ready:", tts.voice, "available:", tts.is_available())
EOF
ls -la data/models/piper/           # en_US-lessac-medium.onnx (+ .json), ~65 MB
```

### TTS smoke test (listen to it!)

```bash
python - <<'EOF'
from pathlib import Path
from contentforge.config import load_settings
from contentforge.ai.tts import get_tts_engine
eng = get_tts_engine(load_settings().tts)
r = eng.synthesize("This free website StudentTools.pk converts PDF files to Word in five seconds.", Path("/tmp/tts_test.wav"))
print(r.engine, r.voice, round(r.duration, 2), "s;", len(r.sentence_timings), "sentence timings")
EOF
ffplay -autoexit -nodisp /tmp/tts_test.wav   # or: mpv /tmp/tts_test.wav
```

Expected: a clear voice, duration ≈ 5–6 s, `1 sentence timings`. Check that “StudentTools dot P K” is pronounced,
not spelled as a URL.

### Word-alignment smoke test (the v0.2 feature, real models)

```bash
python - <<'EOF'
from contentforge.config import load_settings
from contentforge.ai import Transcriber
from contentforge.ai.alignment import align_script
s = load_settings()
text = "This free website StudentTools.pk converts PDF files to Word in five seconds."
asr = Transcriber(s.transcription).transcribe("/tmp/tts_test.wav")
print("ASR heard:", asr.text)
al = align_script(text, asr, duration=asr.duration)
print("match ratio:", None if al is None else round(al.match_ratio, 2))
for w in (al.words if al else []):
    print(f"  {w.word:18s} {w.start:5.2f}-{w.end:5.2f} {'ASR' if w.matched else 'interp'}")
EOF
```

Expected: match ratio ≥ 0.8; word times strictly increasing; “StudentTools.pk” spans ~1 s. **If the ratio is
below 0.6 the pipeline automatically falls back to sentence-level timing** (you will see
`word_alignment: fallback-sentence` in `contentforge jobs <job-id>`), so a low ratio degrades, never breaks.

## 5. Record a real screen recording (20–60 s)

Use GNOME's built-in recorder (`Ctrl+Alt+Shift+R`, saved to `~/Videos/Screencasts/*.webm`) or OBS. Requirements
for a good result:

* 16:9 desktop, 1080p or 720p, **mouse cursor visible** (GNOME: on by default; OBS: enable *Capture Cursor*).
* Speak while recording (the transcript becomes the script). Mention only real features of the tool.
* Name the file after the website so the script/caption can mention it, e.g. `studenttools.pk - pdf to word.webm`.

## 6. First video

```bash
source .venv/bin/activate
cp ~/Videos/Screencasts/"studenttools.pk - pdf to word.webm" data/input/
contentforge process "data/input/studenttools.pk - pdf to word.webm"
```

What to watch in the log (INFO):

| Step | Expected log line | Meaning |
|---|---|---|
| `transcribe` | `Transcribed 38.2s of audio: 6 segments, 92 words (lang=en)` | Whisper worked |
| `narration` | `done … word_alignment=91%` | Piper + word-level alignment OK (`fallback-sentence` = degraded, still fine) |
| `analyse` | `Cursor track: 380 samples, 214 detections (56% coverage)` | cursor tracker found the pointer |
| `render_cut` | `Cursor follow: 41 keyframes over 3 segments, x range 120..1230 px` | moving crop is active (`-> static crop` = fallback) |
| `render_final` | `Deleted superseded intermediate cut.mp4` | low-disk design working |
| `cleanup_work` | `done … removed=21` | package verified, scratch removed |

Total time on a 2-core laptop: ~1.5–3× the clip length.

## 7. Where the output is, and what to inspect

```bash
contentforge jobs --limit 3
ls data/output/studenttoolspk-pdf-to-word-*/
#   <slug>.mp4 cover.jpg caption.txt caption.md social.json script.md script.json
#   subtitles.srt subtitles.ass subtitles.json transcript.* thumbnail_brief.md manifest.json README.md narration.wav
mpv data/output/studenttoolspk-pdf-to-word-*/*.mp4
```

Check-list while watching:

1. **Cursor crop** – the 9:16 window pans smoothly to keep the pointer in view; no jitter, no jumps, never shows
   black borders. When you scrolled the page, the window should hold still.
2. **Captions** – each word highlights exactly as it is spoken (`bold-pop` style); no word lags > 150 ms.
3. **Hook card** (first 2.2 s) and **CTA card** (last 2.5 s) readable; progress bar at the top; watermark top-right.
4. **Audio** – narration clear, original audio muted (`audio.mix.original_volume: 0`), no clipping (loudness −14 LUFS).
5. `manifest.json` → `"crop": {"mode": "cursor", "dynamic": true, …}` and `"narration": {"alignment": {"match_ratio": …}}`.

If something looks off, tune and re-render only the affected part:

```bash
# e.g. camera too nervous -> more smoothing, then re-run from the crop step
cat >> config/config.local.yaml <<'EOF'
video:
  crop:
    follow_cursor:
      smoothing: 0.9
      deadzone: 0.4
EOF
contentforge retry <job-id> --from render_cut
```

## 8. Low-disk / low-RAM configuration

`config/config.local.yaml` (git-ignored) for a 4 GB-RAM / small-SSD laptop:

```yaml
preset: student_reel            # or fast_preview for 720p quick checks
transcription:
  model_size: "base"            # "tiny" if RAM < 4 GB
pipeline:
  max_workers: 1
  delete_intermediates_early: true
  min_free_disk_gb_to_start: 1.0
cleanup:
  min_free_disk_gb: 2           # cleanup triggers below 2 GB free
  archive_retention_days: 7     # raw recordings kept one week
  output_retention_days: 30     # finished packages kept a month (0 = forever)
  delete_temp_after_success: true
  min_age_minutes: 30
video:
  preset: "veryfast"            # x264 speed/quality trade-off; "medium" when idle overnight
  crop:
    follow_cursor:
      sample_fps: 6             # 10 -> 6 halves cursor-analysis time
      work_width: 640
```

What the low-disk design guarantees (all covered by tests in `tests/test_pipeline.py::test_low_disk_design_*`
and `tests/test_cleanup.py`):

| Rule | Where |
|---|---|
| Jobs do not start below `min_free_disk_gb_to_start` (clear error, job `failed`, retry later) | `ProbeStep` |
| `cut.mp4` deleted right after `final.mp4` is verified; `synced.mp4` after the thumbnail frame grab | `RenderFinalStep`, `ThumbnailStep` |
| Work dir emptied **only after** the package exists, `manifest.json` is present and the packaged MP4 size equals the rendered one | `CleanupWorkStep` |
| A resume after early deletion re-renders exactly what is missing (nothing else) | `PipelineRunner._artifacts_present` |
| Cleanup never touches `processing`/`queued` jobs or anything younger than `min_age_minutes` | `Cleaner` |
| Below `cleanup.min_free_disk_gb`: oldest archives first, then oldest outputs, never the newest | `Cleaner.ensure_free_space` |
| Retention: archives `archive_retention_days`, outputs `output_retention_days` (0 = never), logs `logs_retention_days` | `Cleaner` |

## 9. Cleaning temp files

```bash
contentforge cleanup --dry-run     # shows what WOULD be removed and why
contentforge cleanup               # do it
du -sh data/*                      # work/ should be ~0, archive/ = raw recordings, output/ = packages
rm -rf /tmp/tts_test.wav
```

Complete reset (keeps models):

```bash
rm -rf data/work/* data/output/* data/archive/* data/logs/* data/db/*
```

## 10. Run the automated suite on the laptop

```bash
source .venv/bin/activate
pytest -q                          # ~75 s on 2 cores; all tests use small 540x960 renders
pytest -q -m "not ffmpeg"          # pure-python subset in ~5 s
```

## 11. Report back

When filing an issue or a result, include:

```bash
contentforge doctor
contentforge jobs <job-id>                        # per-step outputs incl. word_alignment / cursor_detections
tail -n 100 data/logs/contentforge-$(date +%F).log
cat data/output/<slug>/manifest.json | python -m json.tool | head -80
```

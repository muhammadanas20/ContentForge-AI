# Troubleshooting & FAQ

Run `contentforge doctor` first - it checks ffmpeg, Python deps, TTS engines, the Whisper cache, disk and the LLM provider.
Logs: `data/logs/contentforge-YYYY-MM-DD.log` (DEBUG level always written to file) or the dashboard **Logs** page.
Set `CONTENTFORGE_LOG_LEVEL=DEBUG` to see every ffmpeg command line in the console.

## Common errors

| Symptom | Cause | Fix |
|---|---|---|
| `ffmpeg binary not found` | ffmpeg not installed / not on PATH | Fedora: enable RPM Fusion, `sudo dnf install ffmpeg`; or `pip install imageio-ffmpeg`; or `FFMPEG_BINARY=/path` |
| `No such filter: 'ass'` / `Unknown filter 'zoompan'` | Minimal ffmpeg build (e.g. Fedora's `ffmpeg-free`) | Install the RPM Fusion `ffmpeg` (`--allowerasing`) |
| Subtitles render with boxes/wrong font | `subtitles.font` family not installed | `sudo dnf install dejavu-sans-fonts` or set another family (`fc-list : family`) |
| Thumbnail text tiny | TTF path in `thumbnail.font` missing | Point to an existing `.ttf` (see `FALLBACK_FONTS` in `thumbnails/generator.py`) |
| `LocalEntryNotFoundError` / `SSL` when loading Whisper | No internet for the first model download | Pre-download on another machine (see installation §5) and copy `data/models/whisper/` |
| `Failed to download Piper voice` | Offline / HF blocked | Download `.onnx` + `.onnx.json` manually from `huggingface.co/rhasspy/piper-voices` into `data/models/piper/` |
| Job finished but no narration; event "Narration unavailable" | No TTS engine usable | `pip install piper-tts` (offline) or `edge-tts` (online); check `contentforge doctor` |
| Edge TTS `Cannot connect to host speech.platform.bing.com` | Network/firewall | Use Piper or Kokoro (offline) |
| `Invalid configuration ... extra fields not permitted` | Typo / unknown key in YAML | Compare with `docs/configuration.md`; key names are case-sensitive |
| `... is longer than 270s; record shorter clips` | Recording far longer than target | Keep recordings ≤ 60-90 s or raise `video.max_duration_seconds` |
| Video is all black | Very old ffmpeg or broken source | Update ffmpeg; run `ffmpeg -i file -f null -` to validate the input |
| Crop shows the wrong part of the screen | Smart crop followed cursor noise | Set `video.crop.mode: center` (or `left`/`right`), or lower `crop.smoothing` |
| Too many / too few cuts | Silence thresholds | Adjust `audio.silence.threshold_db` (-30 stricter … -40 looser) and `min_duration` |
| Narration faster than the video | Video was retimed | Lower `tts.speed`, or let the video hold frames by lengthening the recording |
| Whisper very slow | `medium`/`large` on CPU | Use `base`/`small` with `compute_type: int8`, or `device: cuda` |
| `database is locked` | Two processes writing simultaneously with old SQLite | WAL is enabled; upgrade SQLite ≥ 3.7 or switch to PostgreSQL |
| Watcher never triggers | Network/USB mount without inotify | `contentforge run --polling` |
| Job stuck in `processing` after a crash | Daemon killed mid-step | Restart `contentforge run` (auto-resume) or `contentforge resume` |
| Dashboard `ModuleNotFoundError: streamlit` | Dashboard extra not installed | `pip install streamlit pandas` |
| `Low disk space` warnings | Under `cleanup.min_free_disk_gb` | Cleanup runs automatically; lower `archive_retention_days` or move `data/` to a bigger disk (`CONTENTFORGE_DATA_DIR`) |

## FAQ

**Do I need an OpenAI key?** No. Everything runs offline with rule-based script/caption generation. An LLM only polishes wording.

**Can it post to Instagram automatically?** Deliberately not - automated posting/scraping breaks platform terms. The output
folder is a copy-paste kit; you can integrate the official Graph API yourself if your account qualifies.

**Which recording tool should I use?** Anything producing MP4/MKV/MOV/WebM: OBS Studio, GNOME's built-in recorder
(Ctrl+Shift+Alt+R), SimpleScreenRecorder, Kooha. Speak while recording - the transcript drives everything.

**My voice is in Urdu/Roman Urdu.** Set `transcription.language: null` (auto) or `ur` and use Whisper `small` or better.
For narration pick an Urdu Edge voice (`ur-PK-UzmaNeural`) or keep the original audio by disabling `pipeline.steps.narration`.

**How do I change the look?** `subtitles.style`, colours in `subtitles.*`/`video.branding.*`, then
`contentforge retry <job_id> --from subtitles` to re-render an existing job without redoing transcription.

**Can I add background music?** Put a royalty-free file in `data/assets/` and set `audio.mix.background_music` + `music_volume`.

**How do I process files on a schedule instead of immediately?** `scheduler.mode: scheduled` and `process_cron`.

**Where are my raw recordings?** After success they are moved to `data/archive/YYYY-MM/<slug>/` and deleted after
`archive_retention_days`. Set `pipeline.steps.archive: false` to leave them in `input/`.

**Is GPU used?** Only if you set `transcription.device: cuda`. Video encoding is CPU (libx264) for maximum compatibility.

**Can I run several videos in parallel?** `pipeline.max_workers: 2+` - each worker runs Whisper + FFmpeg, so watch RAM/CPU.

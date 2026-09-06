# Testing

```bash
pip install -r requirements-dev.txt
pytest                      # full suite (~40 s; needs ffmpeg)
pytest -m "not ffmpeg"      # pure-python tests only (config, segments, captions, AI, db, analytics)
pytest tests/test_pipeline.py -k resume -vv
pytest --cov=contentforge --cov-report=term-missing
ruff check contentforge tests
```

## Layout

| File | Covers |
|---|---|
| `test_config.py` | YAML loading, local override, env overrides, validation errors, data-dir rebasing |
| `test_utils.py` | slugify, atomic writes, stability wait, guarded delete, retry, time/colour formats, ffmpeg probe/silence/frames |
| `test_db.py` | job/step lifecycle, metrics latest-per-job, hashtag history, events |
| `test_segments.py` | silence → keep-list maths, jump cuts, `Timeline` remapping |
| `test_subtitles.py` | caption chunking rules, all four ASS styles, overlays, writers |
| `test_video_processing.py` | crop planning, zoom expressions, OpenCV analysis, **real FFmpeg render** (cut/zoom/mix/overlay, progress-bar pixel check), retime/freeze |
| `test_ai.py` | transcript cleaning, rule-based script, LLM acceptance + hallucination rejection, LLM client request shape, hashtags rotation/bans, social package, transcript I/O |
| `test_thumbnail.py` | thumbnail render + Canva brief |
| `test_cleanup_archive_output.py` | cleanup safety rules, low-disk reclaim, archiver, packager, analytics report + CSV import |
| `test_pipeline.py` | **end-to-end runs** with stubbed Whisper/TTS: full flow, no-narration fallback, crash → resume, `--from` step, bad input |
| `test_watcher_scheduler_app.py` | watcher detection/dedupe/partial files, scheduler registration, app façade dedupe/retry/reports, CLI commands |
| `test_dashboard.py` | Every Streamlit page renders via `AppTest` |

Fixtures (`conftest.py`): `settings` (config anchored in a temp data dir), `ffmpeg`, `sample_video` (synthetic 6 s
1280×720 clip with a 2 s silence gap), `fake_transcript`.

Whisper and TTS models are never downloaded in tests - `FakeTranscriber` / `FakeTTS` in `test_pipeline.py` stand in.
To exercise the real engines manually: `contentforge process data/input/<file>.mp4 --log-level DEBUG`.

## What the suite proves - and what it does not

| Level | Covered by | Notes |
|---|---|---|
| Unit | `test_cursor_crop.py` (follow_path, keyframes, expression, planner bounds/fallbacks), `test_alignment.py` (normalisation, alignment, rejection), `test_config.py` (presets) | pure python, < 5 s |
| Integration (real FFmpeg/OpenCV) | `test_cursor_crop.py::test_detect_cursor_track_on_generated_recording`, `::test_cursor_crop_render_keeps_cursor_visible` - a synthetic but realistic tutorial recording (`tests/screen_recording.py`: page, sidebar, arrow cursor, clicks, blinking caret, scroll) is tracked against ground truth (median error < 20 px), rendered with the panning crop and compared frame-by-frame with the source | |
| End-to-end pipeline | `test_pipeline.py`, `test_alignment.py::test_pipeline_word_level_captions_and_fallback`, `test_cursor_crop.py::test_pipeline_uses_cursor_crop_and_falls_back` | Whisper and TTS are **stubbed** (`FakeTranscriber`, `AligningTranscriber`, `FakeTTS`) |
| Real models | **not** in CI - follow `docs/fedora-real-system-test.md` | Whisper accuracy, Piper voice and true word timing can only be judged on a real machine |

Never claim a model-dependent feature works from the stubbed tests alone.

## Writing a test for a new step

```python
def test_my_step(fast_settings, ffmpeg, sample_video, patched):
    runner = PipelineRunner(
        fast_settings, Database(fast_settings.paths.db), ffmpeg, steps=[ProbeStep, MyStep]
    )
    ctx = runner.create_job(sample_video)
    assert runner.run(ctx).status == "completed"
```

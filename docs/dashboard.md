# Dashboard

Start with `contentforge dashboard` (or `streamlit run contentforge/dashboard/app.py`) and open http://localhost:8501.
Host/port come from `dashboard.host/port`.

| Page | Shows | Actions |
|---|---|---|
| **Overview** | Job counters, free disk, processing queue with per-job step progress, recent events, files waiting in `input/` | — |
| **Completed** | Table of finished videos; select one to preview the MP4, cover, caption, manifest, files and step timings | Re-run from any step |
| **Errors** | Failed jobs with the failing step and error, error events | Retry (resume) · Mark cancelled |
| **Logs** | Tail of any dated log file with level filter | — |
| **Analytics** | Totals, per-video table with composite score, views chart, improvement suggestions | Record metrics form · Generate report |
| **Storage** | Disk usage bar, per-folder sizes, cleanup policy summary | Dry run · Run cleanup now |
| **System health** | ffmpeg, python deps, TTS engines, Whisper model, LLM provider, CPU/RAM/load, last daemon event, effective config | — |

Toggle **Auto-refresh** in the sidebar to poll every `dashboard.refresh_seconds`.

The dashboard reads the same SQLite database the daemon writes (WAL mode), so it can run alongside `contentforge run`.
Actions call the same service classes as the CLI - there is no separate code path.

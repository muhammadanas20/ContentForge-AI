"""Streamlit dashboard for ContentForge-AI.

Run with ``contentforge dashboard`` or
``streamlit run contentforge/dashboard/app.py``.

The dashboard is read-mostly: it inspects the SQLite database, the log
directory and the data folders. Actions (retry, cleanup, add metrics) call the
same service classes as the CLI.
"""

from __future__ import annotations

import json
import os
import platform
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from contentforge import __version__  # noqa: E402
from contentforge.analytics import AnalyticsService  # noqa: E402
from contentforge.cleanup import Cleaner, disk_usage  # noqa: E402
from contentforge.config import load_settings  # noqa: E402
from contentforge.db import Database  # noqa: E402
from contentforge.log import list_log_files, tail_log  # noqa: E402
from contentforge.utils import human_size  # noqa: E402

st.set_page_config(page_title="ContentForge-AI", page_icon="🎬", layout="wide")


# ------------------------------------------------------------------ helpers
@st.cache_resource
def _settings():
    s = load_settings()
    s.ensure_directories()
    return s


def _db() -> Database:
    return Database(_settings().paths.db)


def _jobs_df(db: Database, status: str | None = None) -> pd.DataFrame:
    jobs = db.list_jobs(status=status, limit=500)
    if not jobs:
        return pd.DataFrame(
            columns=[
                "id",
                "status",
                "current_step",
                "title",
                "source",
                "duration_seconds",
                "updated_at",
            ]
        )
    df = pd.DataFrame(jobs)
    df["source"] = df["source_path"].map(lambda p: Path(p).name)
    return df[
        [
            "id",
            "status",
            "current_step",
            "title",
            "source",
            "duration_seconds",
            "attempts",
            "created_at",
            "updated_at",
            "error",
            "output_dir",
        ]
    ]


def _status_badge(status: str) -> str:
    return (
        {
            "completed": "🟢",
            "archived": "🟢",
            "processing": "🟡",
            "queued": "⚪",
            "failed": "🔴",
        }.get(status, "⚫")
        + " "
        + status
    )


# ------------------------------------------------------------------- pages
def page_overview(db: Database) -> None:
    s = _settings()
    counts = db.count_by_status()
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Queued", counts.get("queued", 0))
    c2.metric("Processing", counts.get("processing", 0))
    c3.metric("Completed", counts.get("completed", 0) + counts.get("archived", 0))
    c4.metric("Failed", counts.get("failed", 0))
    total, used, free = disk_usage(s.paths.data_dir)
    c5.metric(
        "Free disk",
        f"{free / 1024**3:.1f} GB",
        delta=None if free / 1024**3 > s.cleanup.min_free_disk_gb else "LOW",
        delta_color="inverse",
    )

    st.subheader("Processing queue")
    active = pd.concat([_jobs_df(db, "processing"), _jobs_df(db, "queued")])
    if active.empty:
        st.info(f"Queue is empty. Drop a recording into: `{s.paths.input}`")
    else:
        st.dataframe(
            active[["id", "status", "current_step", "source", "attempts", "updated_at"]],
            use_container_width=True,
            hide_index=True,
        )
        for _, row in active.iterrows():
            steps = db.get_steps(row["id"])
            done = sum(1 for v in steps.values() if v["status"] in ("done", "skipped"))
            st.progress(
                min(1.0, done / 16),
                text=f"{row['id']} - {row['current_step'] or 'starting'} ({done}/16 steps)",
            )

    st.subheader("Recent events")
    events = db.recent_events(limit=15)
    if events:
        st.dataframe(
            pd.DataFrame(events)[["created_at", "level", "job_id", "message"]],
            use_container_width=True,
            hide_index=True,
        )

    st.subheader("Input folder")
    files = sorted(Path(s.paths.input).glob("*"))
    media = [f for f in files if f.suffix.lower() in s.watcher.extensions]
    st.write(
        f"{len(media)} recording(s) waiting in `{s.paths.input}`"
        if media
        else "No files in input folder."
    )
    for f in media[:20]:
        st.caption(
            f"• {f.name} ({human_size(f.stat().st_size)}, modified {datetime.fromtimestamp(f.stat().st_mtime):%Y-%m-%d %H:%M})"
        )


def page_completed(db: Database) -> None:
    df = pd.concat([_jobs_df(db, "completed"), _jobs_df(db, "archived")])
    if df.empty:
        st.info("No completed videos yet.")
        return
    st.dataframe(
        df[["id", "status", "title", "source", "duration_seconds", "created_at", "output_dir"]],
        use_container_width=True,
        hide_index=True,
    )
    job_id = st.selectbox("Inspect job", df["id"].tolist())
    if not job_id:
        return
    job = db.get_job(job_id)
    out_dir = Path(job["output_dir"]) if job and job.get("output_dir") else None
    if out_dir and out_dir.exists():
        col1, col2 = st.columns([1, 2])
        videos = list(out_dir.glob("*.mp4"))
        if videos:
            col1.video(str(videos[0]))
        cover = out_dir / "cover.jpg"
        if cover.exists():
            col1.image(str(cover), caption="cover.jpg", width=220)
        cap = out_dir / "caption.txt"
        if cap.exists():
            col2.text_area("Instagram caption (copy-paste)", cap.read_text(), height=320)
        manifest = out_dir / "manifest.json"
        if manifest.exists():
            with col2.expander("manifest.json"):
                st.json(json.loads(manifest.read_text()))
        with st.expander("Files"):
            for f in sorted(out_dir.iterdir()):
                st.caption(f"{f.name} - {human_size(f.stat().st_size)}")
    else:
        st.warning("Output directory missing (deleted by cleanup?)")
    with st.expander("Step timings"):
        steps = db.get_steps(job_id)
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "step": k,
                        **{
                            kk: v
                            for kk, v in vv.items()
                            if kk in ("status", "attempts", "duration_seconds")
                        },
                    }
                    for k, vv in steps.items()
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )
    st.markdown("**Re-run from a step** (e.g. after changing subtitle style)")
    from contentforge.pipeline import DEFAULT_STEPS

    step = st.selectbox("Step", [c.name for c in DEFAULT_STEPS], index=8)
    if st.button("Re-run", type="primary"):
        _run_retry(job_id, step)


def page_errors(db: Database) -> None:
    df = _jobs_df(db, "failed")
    if df.empty:
        st.success("No failed jobs.")
    else:
        st.dataframe(
            df[["id", "current_step", "source", "attempts", "error", "updated_at"]],
            use_container_width=True,
            hide_index=True,
        )
        job_id = st.selectbox("Retry job", df["id"].tolist())
        c1, c2 = st.columns(2)
        if c1.button("Retry (resume)", type="primary"):
            _run_retry(job_id, None)
        if c2.button("Mark as cancelled"):
            db.update_job(job_id, status="cancelled")
            st.rerun()
    st.subheader("Error events")
    errs = db.recent_events(limit=100, level="error")
    if errs:
        st.dataframe(
            pd.DataFrame(errs)[["created_at", "job_id", "message"]],
            use_container_width=True,
            hide_index=True,
        )


def _run_retry(job_id: str, from_step: str | None) -> None:
    from contentforge.app import ContentForgeApp

    with st.spinner(f"Running job {job_id}..."):
        app = ContentForgeApp(_settings(), console_logging=False)
        try:
            result = app.retry_job(job_id, from_step=from_step)
        except Exception as exc:  # surfaced to the operator
            st.error(str(exc))
            return
        finally:
            app.close()
    (st.success if result.status in ("completed", "archived") else st.error)(
        f"{result.status}: {result.error or result.output_dir}"
    )


def page_logs() -> None:
    s = _settings()
    files = list_log_files(s.paths.logs)
    if not files:
        st.info("No log files yet.")
        return
    f = st.selectbox("Log file", files, format_func=lambda p: p.name)
    n = st.slider("Lines", 50, 2000, s.dashboard.log_tail_lines, step=50)
    level = st.multiselect("Filter level", ["DEBUG", "INFO", "WARNING", "ERROR"], default=[])
    lines = tail_log(f, n)
    if level:
        lines = [ln for ln in lines if any(f"| {lv}" in ln for lv in level)]
    st.code("".join(lines) or "(empty)", language="log")


def page_analytics(db: Database) -> None:
    s = _settings()
    svc = AnalyticsService(s.analytics, db)
    perfs = svc.performances()
    if perfs:
        df = pd.DataFrame([p.to_dict() for p in perfs])
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total views", f"{int(df['views'].sum()):,}")
        c2.metric("Avg completion", f"{df['completion_rate'].mean() * 100:.0f}%")
        c3.metric("Avg like rate", f"{df['like_rate'].mean() * 100:.1f}%")
        c4.metric("Follower growth", int(df["follower_growth"].sum()))
        st.dataframe(
            df[
                [
                    "title",
                    "platform",
                    "views",
                    "likes",
                    "comments",
                    "shares",
                    "saves",
                    "completion_rate",
                    "score",
                    "recorded_at",
                ]
            ].sort_values("score", ascending=False),
            use_container_width=True,
            hide_index=True,
        )
        if len(df) > 1:
            st.bar_chart(df.set_index("title")[["views"]])
        st.subheader("Suggestions")
        for tip in svc.suggestions(perfs):
            st.markdown(f"- {tip}")
    else:
        st.info("No metrics yet. Record them below after publishing.")

    st.subheader("Record metrics")
    jobs = db.list_jobs(limit=300)
    options = {f"{j.get('title') or j['slug']} ({j['id']})": j["id"] for j in jobs}
    with st.form("metrics"):
        job_label = st.selectbox("Video", list(options) or ["(no jobs)"])
        platform_name = st.selectbox(
            "Platform", ["instagram", "youtube_shorts", "tiktok", "facebook"]
        )
        cols = st.columns(4)
        views = cols[0].number_input("Views", 0, step=1)
        likes = cols[1].number_input("Likes", 0, step=1)
        comments = cols[2].number_input("Comments", 0, step=1)
        shares = cols[3].number_input("Shares", 0, step=1)
        cols = st.columns(4)
        saves = cols[0].number_input("Saves", 0, step=1)
        completion = cols[1].number_input("Completion rate %", 0.0, 100.0, step=1.0)
        avg_watch = cols[2].number_input("Avg watch (s)", 0.0, step=0.5)
        followers = cols[3].number_input("Follower growth", -100000, 100000, 0, step=1)
        notes = st.text_input("Notes")
        if st.form_submit_button("Save", type="primary") and options:
            svc.record(
                options[job_label],
                platform_name,
                views=views,
                likes=likes,
                comments=comments,
                shares=shares,
                saves=saves,
                completion_rate=completion,
                avg_watch_seconds=avg_watch,
                follower_growth=followers,
                notes=notes,
            )
            st.success("Saved.")
            st.rerun()
    if st.button("Generate report now"):
        rep = svc.build_report("manual")
        files = svc.write_report(rep)
        st.markdown(rep.to_markdown())
        st.caption(f"Written to {files['md']}")


def page_storage(db: Database) -> None:
    s = _settings()
    cleaner = Cleaner(s.cleanup, s.paths, db)
    total, used, free = disk_usage(s.paths.data_dir)
    st.progress(
        used / total if total else 0.0,
        text=f"Disk: {human_size(used)} used of {human_size(total)} ({human_size(free)} free)",
    )
    overview = cleaner.storage_overview()
    df = pd.DataFrame({"folder": list(overview), "bytes": list(overview.values())})
    df["size"] = df["bytes"].map(human_size)
    st.dataframe(df[["folder", "size"]], hide_index=True)
    st.bar_chart(df.set_index("folder")["bytes"])
    st.subheader("Cleanup")
    st.caption(
        f"Policy: temp after success={s.cleanup.delete_temp_after_success}, raw after success={s.cleanup.delete_raw_after_success}, "
        f"archive retention={s.cleanup.archive_retention_days}d, min free={s.cleanup.min_free_disk_gb} GB, "
        f"min age={s.cleanup.min_age_minutes} min"
    )
    c1, c2 = st.columns(2)
    if c1.button("Dry run"):
        rep = cleaner.run(dry_run=True)
        st.code(
            rep.summary()
            + "\n"
            + "\n".join(rep.removed_work_dirs + rep.removed_archives + rep.removed_outputs)
        )
    if c2.button("Run cleanup now", type="primary"):
        rep = cleaner.run()
        st.success(rep.summary())


def page_health(db: Database) -> None:
    s = _settings()
    from contentforge.ai.tts import available_engines
    from contentforge.utils import ffmpeg_available
    from contentforge.utils.ffmpeg import FFmpeg

    rows = []
    rows.append(("ffmpeg", "OK " + FFmpeg().version() if ffmpeg_available() else "MISSING"))
    for mod in ("faster_whisper", "cv2", "PIL", "watchdog", "apscheduler"):
        try:
            __import__(mod)
            rows.append((mod, "OK"))
        except ImportError:
            rows.append((mod, "MISSING"))
    engines = available_engines(s.tts)
    rows.append(("TTS engines", ", ".join(engines) or "none"))
    rows.append(("TTS configured", s.tts.engine))
    rows.append(("Whisper model", s.transcription.model_size))
    rows.append(("LLM provider", os.environ.get("CONTENTFORGE_LLM_PROVIDER", "none")))
    rows.append(("Python", platform.python_version()))
    rows.append(("OS", platform.platform()))
    rows.append(("Version", __version__))
    try:
        import psutil

        rows.append(("CPU %", f"{psutil.cpu_percent(interval=0.2):.0f}"))
        rows.append(("RAM", f"{psutil.virtual_memory().percent:.0f}% used"))
        rows.append(("Load avg", ", ".join(f"{x:.2f}" for x in os.getloadavg())))
    except Exception:
        pass
    st.table(pd.DataFrame(rows, columns=["check", "value"]))
    last = db.recent_events(limit=1)
    if last:
        st.caption(f"Last event: {last[0]['created_at']} - {last[0]['message']}")
    daemon_events = [e for e in db.recent_events(limit=200) if "Daemon" in e["message"]]
    if daemon_events:
        st.info(f"Daemon: {daemon_events[0]['message']} at {daemon_events[0]['created_at']}")
    with st.expander("Effective configuration"):
        st.json(json.loads(json.dumps(s.model_dump(mode="json"), default=str)))


# --------------------------------------------------------------------- main
def main() -> None:
    s = _settings()
    st.sidebar.title("🎬 ContentForge-AI")
    st.sidebar.caption(f"{s.project.brand} · v{__version__}")
    page = st.sidebar.radio(
        "Page", ["Overview", "Completed", "Errors", "Logs", "Analytics", "Storage", "System health"]
    )
    auto = st.sidebar.toggle("Auto-refresh", value=False)
    st.sidebar.caption(f"Input: `{s.paths.input}`")
    db = _db()
    try:
        st.title(page)
        {
            "Overview": lambda: page_overview(db),
            "Completed": lambda: page_completed(db),
            "Errors": lambda: page_errors(db),
            "Logs": page_logs,
            "Analytics": lambda: page_analytics(db),
            "Storage": lambda: page_storage(db),
            "System health": lambda: page_health(db),
        }[page]()
    finally:
        db.close()
    if auto:
        time.sleep(s.dashboard.refresh_seconds)
        st.rerun()


main()

"""Command-line interface.

Examples::

    contentforge doctor                     # check ffmpeg, models, TTS engines, disk
    contentforge run                        # start the watcher daemon (auto-processing)
    contentforge process data/input/x.mp4   # process one file now
    contentforge retry <job_id> --from subtitles
    contentforge jobs --status failed
    contentforge cleanup --dry-run
    contentforge analytics add <job_id> --views 1200 --likes 90
    contentforge analytics report
    contentforge dashboard
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from contentforge import __version__
from contentforge.config import ConfigError, load_settings, reset_settings

console = Console()


def _settings(args: argparse.Namespace):
    if getattr(args, "preset", None):
        os.environ["CONTENTFORGE_PRESET"] = args.preset
    try:
        s = load_settings(args.config) if getattr(args, "config", None) else load_settings()
    except ConfigError as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        sys.exit(2)
    if getattr(args, "log_level", None):
        s.logging.level = args.log_level
    reset_settings(s)
    return s


def _app(args: argparse.Namespace):
    from contentforge.app import ContentForgeApp

    return ContentForgeApp(_settings(args), console_logging=not getattr(args, "quiet", False))


# ----------------------------------------------------------------- commands
def cmd_run(args: argparse.Namespace) -> int:
    app = _app(args)
    app.start(block=True, use_polling=args.polling)
    return 0


def cmd_process(args: argparse.Namespace) -> int:
    app = _app(args)
    code = 0
    for f in args.files:
        p = Path(f)
        if not p.exists():
            console.print(f"[red]Not found:[/red] {p}")
            code = 1
            continue
        result = app.process_file(p, website_hint=args.website or "", force=args.force)
        _print_result(result)
        code = code or (0 if result.status in ("completed", "archived") else 1)
    app.close()
    return code


def cmd_retry(args: argparse.Namespace) -> int:
    app = _app(args)
    try:
        result = app.retry_job(args.job_id, from_step=args.from_step)
    except (ValueError, FileNotFoundError) as exc:
        console.print(f"[red]{exc}[/red]")
        return 1
    _print_result(result)
    return 0 if result.status in ("completed", "archived") else 1


def cmd_resume(args: argparse.Namespace) -> int:
    app = _app(args)
    results = app.runner.resume_incomplete()
    if not results:
        console.print("Nothing to resume.")
    for r in results:
        _print_result(r)
    return 0


def cmd_jobs(args: argparse.Namespace) -> int:
    from contentforge.db import Database

    s = _settings(args)
    db = Database(s.paths.db)
    jobs = db.list_jobs(status=args.status, limit=args.limit)
    if args.json:
        print(json.dumps(jobs, indent=2, default=str))
        return 0
    table = Table(title=f"Jobs ({len(jobs)})")
    for col in ("id", "status", "step", "title", "source", "updated"):
        table.add_column(col)
    for j in jobs:
        colour = {
            "completed": "green",
            "archived": "green",
            "failed": "red",
            "processing": "yellow",
        }.get(j["status"], "white")
        table.add_row(
            j["id"],
            f"[{colour}]{j['status']}[/{colour}]",
            j.get("current_step") or "-",
            (j.get("title") or "-")[:40],
            Path(j["source_path"]).name[:40],
            (j["updated_at"] or "")[:19],
        )
    console.print(table)
    if args.job_id:
        job = db.get_job(args.job_id)
        console.print_json(
            json.dumps({"job": job, "steps": db.get_steps(args.job_id)}, default=str)
        )
    return 0


def cmd_steps(args: argparse.Namespace) -> int:
    from contentforge.pipeline import DEFAULT_STEPS

    for i, cls in enumerate(DEFAULT_STEPS, 1):
        console.print(
            f"{i:2d}. [bold]{cls.name}[/bold] - {(cls.__doc__ or '').strip().splitlines()[0] if cls.__doc__ else ''}"
        )
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    app = _app(args)
    rep = app.cleaner.run(dry_run=args.dry_run)
    console.print(rep.summary())
    if args.verbose:
        for k in (
            "removed_work_dirs",
            "removed_archives",
            "removed_outputs",
            "removed_raw",
            "skipped",
        ):
            for item in getattr(rep, k):
                console.print(f"  {k}: {item}")
    return 0


def cmd_analytics(args: argparse.Namespace) -> int:
    app = _app(args)
    svc = app.analytics
    if args.analytics_cmd == "add":
        svc.record(
            args.job_id,
            args.platform,
            views=args.views,
            likes=args.likes,
            comments=args.comments,
            shares=args.shares,
            saves=args.saves,
            completion_rate=args.completion,
            avg_watch_seconds=args.avg_watch,
            follower_growth=args.followers,
            notes=args.notes or "",
        )
        console.print("[green]Recorded.[/green]")
    elif args.analytics_cmd == "import":
        n = svc.import_csv(Path(args.csv))
        console.print(f"Imported {n} rows.")
    elif args.analytics_cmd == "report":
        rep = svc.build_report(args.period)
        files = svc.write_report(rep)
        console.print(rep.to_markdown())
        console.print(f"[dim]Written to {files['md']}[/dim]")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from contentforge.ai.tts import available_engines
    from contentforge.utils import disk_free_gb, ffmpeg_available
    from contentforge.utils.ffmpeg import FFmpeg

    s = _settings(args)
    ok = True
    console.print(f"[bold]ContentForge-AI {__version__}[/bold] - root {s.root} - preset {s.preset}")
    if ffmpeg_available():
        ff = FFmpeg()
        console.print(f"[green]✔[/green] ffmpeg {ff.version()} ({ff.ffmpeg_bin})")
        console.print(
            f"[green]✔[/green] ffprobe {ff.ffprobe_bin}"
            if ff.ffprobe_bin
            else "[yellow]![/yellow] ffprobe not found (fallback parser in use)"
        )
    else:
        console.print("[red]✘[/red] ffmpeg not found - sudo dnf install ffmpeg")
        ok = False
    for mod, label in (
        ("faster_whisper", "faster-whisper"),
        ("cv2", "opencv"),
        ("PIL", "pillow"),
        ("watchdog", "watchdog"),
        ("apscheduler", "apscheduler"),
        ("streamlit", "streamlit (dashboard)"),
    ):
        try:
            __import__(mod)
            console.print(f"[green]✔[/green] {label}")
        except ImportError:
            console.print(f"[red]✘[/red] {label} missing")
            ok = ok and mod in ("streamlit",)
    engines = available_engines(s.tts)
    if engines:
        mark = "green]✔" if s.tts.engine in engines else "yellow]!"
        console.print(
            f"[{mark}[/] TTS engines available: {', '.join(engines)} (configured: {s.tts.engine})"
        )
    else:
        console.print(
            "[yellow]![/yellow] No TTS engine available - narration will be skipped (pip install piper-tts)"
        )
    whisper_dir = Path(s.transcription.download_root)
    cached = any(whisper_dir.rglob("model.bin")) if whisper_dir.exists() else False
    console.print(
        f"[{'green]✔' if cached else 'yellow]!'}[/] Whisper model '{s.transcription.model_size}' "
        f"{'cached' if cached else 'will download on first run'} ({whisper_dir})"
    )
    free = disk_free_gb(s.paths.data_dir)
    console.print(
        f"[{'green]✔' if free > s.cleanup.min_free_disk_gb else 'red]✘'}[/] Free disk: {free:.1f} GB "
        f"(minimum {s.cleanup.min_free_disk_gb} GB)"
    )
    provider = os.environ.get("CONTENTFORGE_LLM_PROVIDER", "none")
    console.print(
        f"[green]✔[/green] LLM provider: {provider}"
        + (" (rule-based script writer)" if provider == "none" else "")
    )
    console.print(f"[green]✔[/green] Input folder: {s.paths.input}")
    return 0 if ok else 1


def cmd_dashboard(args: argparse.Namespace) -> int:
    s = _settings(args)
    app_path = Path(__file__).parent / "dashboard" / "app.py"
    cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(app_path),
        "--server.port",
        str(args.port or s.dashboard.port),
        "--server.address",
        args.host or s.dashboard.host,
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
    ]
    env = dict(os.environ)
    if args.config:
        env["CONTENTFORGE_CONFIG"] = str(Path(args.config).resolve())
    return subprocess.call(cmd, env=env)


def cmd_config(args: argparse.Namespace) -> int:
    s = _settings(args)
    data = s.model_dump(mode="json")
    if args.section:
        data = data.get(args.section, {})
    console.print_json(json.dumps(data, default=str))
    return 0


# ------------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="contentforge", description="AI Content Factory for StudentTools.pk"
    )
    p.add_argument("--version", action="version", version=f"contentforge {__version__}")
    p.add_argument("-c", "--config", help="path to config.yaml (default: config/config.yaml)")
    p.add_argument(
        "-p",
        "--preset",
        help="config preset to apply (see 'presets' in config.yaml; 'none' to disable)",
    )
    p.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p.add_argument("-q", "--quiet", action="store_true", help="disable console logging")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("run", help="start the folder watcher daemon")
    sp.add_argument(
        "--polling", action="store_true", help="use polling instead of inotify (network drives)"
    )
    sp.set_defaults(fn=cmd_run)

    sp = sub.add_parser("process", help="process one or more files now")
    sp.add_argument("files", nargs="+")
    sp.add_argument("--website", help="website shown in the recording (e.g. smallpdf.com)")
    sp.add_argument(
        "--force", action="store_true", help="reprocess even if identical content was done before"
    )
    sp.set_defaults(fn=cmd_process)

    sp = sub.add_parser("retry", help="retry/resume a job")
    sp.add_argument("job_id")
    sp.add_argument("--from", dest="from_step", help="re-run from this step (see `steps`)")
    sp.set_defaults(fn=cmd_retry)

    sp = sub.add_parser("resume", help="resume all interrupted jobs")
    sp.set_defaults(fn=cmd_resume)

    sp = sub.add_parser("jobs", help="list jobs")
    sp.add_argument("job_id", nargs="?")
    sp.add_argument("--status")
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_jobs)

    sp = sub.add_parser("steps", help="list pipeline steps")
    sp.set_defaults(fn=cmd_steps)

    sp = sub.add_parser("cleanup", help="run cleanup now")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("-v", "--verbose", action="store_true")
    sp.set_defaults(fn=cmd_cleanup)

    sp = sub.add_parser("analytics", help="record metrics / build reports")
    asub = sp.add_subparsers(dest="analytics_cmd", required=True)
    a = asub.add_parser("add")
    a.add_argument("job_id")
    a.add_argument("--platform", default="instagram")
    for name in ("views", "likes", "comments", "shares", "saves", "followers"):
        a.add_argument(f"--{name}", type=int, default=0)
    a.add_argument("--completion", type=float, default=0.0, help="completion rate (0-1 or percent)")
    a.add_argument("--avg-watch", dest="avg_watch", type=float, default=0.0)
    a.add_argument("--notes")
    i = asub.add_parser("import")
    i.add_argument("csv")
    r = asub.add_parser("report")
    r.add_argument("--period", default="all-time")
    sp.set_defaults(fn=cmd_analytics)

    sp = sub.add_parser("doctor", help="check the environment")
    sp.set_defaults(fn=cmd_doctor)

    sp = sub.add_parser("dashboard", help="launch the Streamlit dashboard")
    sp.add_argument("--port", type=int)
    sp.add_argument("--host")
    sp.set_defaults(fn=cmd_dashboard)

    sp = sub.add_parser("config", help="print the effective configuration")
    sp.add_argument("section", nargs="?")
    sp.set_defaults(fn=cmd_config)
    return p


def _print_result(result) -> None:
    colour = "green" if result.status in ("completed", "archived") else "red"
    console.print(
        f"[{colour}]{result.status.upper()}[/{colour}] job {result.job_id} ({result.slug}) in {result.total_seconds:.1f}s"
    )
    if result.output_dir:
        console.print(f"  output: {result.output_dir}")
    if result.error:
        console.print(f"  error: {result.error}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.fn(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

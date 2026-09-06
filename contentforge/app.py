"""Application façade: wires config, DB, pipeline, watcher, scheduler and cleanup.

Both the CLI (``contentforge`` command) and the Streamlit dashboard use this
class so behaviour is identical everywhere.
"""

from __future__ import annotations

import signal
import threading
import time
from pathlib import Path

from contentforge.analytics import AnalyticsService
from contentforge.cleanup import Cleaner
from contentforge.config import Settings, get_settings
from contentforge.db import Database
from contentforge.input.watcher import InputWatcher
from contentforge.log import get_logger, setup_logging
from contentforge.pipeline import JobResult, PipelineRunner
from contentforge.scheduling.scheduler import ForgeScheduler
from contentforge.utils import FFmpeg, disk_free_gb

log = get_logger("app")


class ContentForgeApp:
    def __init__(self, settings: Settings | None = None, *, console_logging: bool = True):
        self.settings = settings or get_settings()
        self.settings.ensure_directories()
        setup_logging(
            self.settings.paths.logs,
            self.settings.logging.level,
            console=console_logging and self.settings.logging.console,
            rich_tracebacks=self.settings.logging.rich_tracebacks,
            json_lines=self.settings.logging.json_lines,
            retention_days=self.settings.logging.retention_days,
        )
        self.db = Database(self.settings.paths.db)
        self.ffmpeg = FFmpeg()
        self.runner = PipelineRunner(self.settings, self.db, self.ffmpeg)
        self.cleaner = Cleaner(self.settings.cleanup, self.settings.paths, self.db)
        self.analytics = AnalyticsService(self.settings.analytics, self.db)
        self.scheduler = ForgeScheduler(self.settings.scheduler, self.settings.project.timezone)
        self.watcher: InputWatcher | None = None
        self._stop = threading.Event()
        self._deferred: list[Path] = []
        self._deferred_lock = threading.Lock()

    # ------------------------------------------------------------ single
    def process_file(self, path: Path, *, website_hint: str = "", force: bool = False) -> JobResult:
        """Run the full pipeline for one recording (dedupes unless ``force``)."""
        path = Path(path)
        if not force:
            existing = self.runner.find_existing(path)
            if existing and existing["status"] in ("completed", "archived"):
                log.info(
                    "Skipping %s: identical content already processed as job %s",
                    path.name,
                    existing["id"],
                )
                return JobResult(
                    existing["id"],
                    existing["slug"],
                    existing["status"],
                    Path(existing["output_dir"]) if existing.get("output_dir") else None,
                )
            if existing and existing["status"] in ("failed", "processing", "queued"):
                ctx = self.runner.load_job(existing["id"])
                if ctx and ctx.source.exists():
                    log.info("Resuming existing job %s for %s", existing["id"], path.name)
                    return self.runner.run(ctx)
        ctx = self.runner.create_job(path, website_hint=website_hint)
        return self.runner.run(ctx)

    def retry_job(self, job_id: str, from_step: str | None = None) -> JobResult:
        ctx = self.runner.load_job(job_id)
        if ctx is None:
            raise ValueError(f"Unknown job {job_id}")
        if not ctx.source.exists():
            raise FileNotFoundError(f"Source for job {job_id} no longer exists: {ctx.source}")
        return self.runner.run(ctx, force_from=from_step)

    # ------------------------------------------------------------ daemon
    def _on_new_file(self, path: Path) -> None:
        if self.settings.scheduler.mode == "scheduled":
            with self._deferred_lock:
                self._deferred.append(path)
            log.info("Queued %s for the next scheduled run", path.name)
            return
        self.process_file(path)

    def process_deferred(self) -> list[JobResult]:
        with self._deferred_lock:
            batch, self._deferred = list(self._deferred), []
        results = []
        for p in batch:
            if p.exists():
                results.append(self.process_file(p))
        return results

    def health_check(self) -> dict:
        free = disk_free_gb(self.settings.paths.data_dir)
        status = {
            "free_gb": round(free, 2),
            "queue": self.watcher.queue_size if self.watcher else 0,
            "jobs": self.db.count_by_status(),
        }
        if free < self.settings.cleanup.min_free_disk_gb:
            log.warning("Low disk space (%.1f GB); running cleanup", free)
            self.cleaner.run()
        return status

    def weekly_report(self) -> dict:
        rep = self.analytics.build_report("weekly")
        files = self.analytics.write_report(rep, f"weekly-{time.strftime('%Y-%m-%d')}")
        self.db.add_event("info", f"Weekly analytics report written: {files['md']}")
        return {k: str(v) for k, v in files.items()}

    def monthly_report(self) -> dict:
        rep = self.analytics.build_report("monthly")
        files = self.analytics.write_report(rep, f"monthly-{time.strftime('%Y-%m')}")
        self.db.add_event("info", f"Monthly analytics report written: {files['md']}")
        return {k: str(v) for k, v in files.items()}

    def start(self, *, block: bool = True, use_polling: bool = False) -> None:
        """Start watcher + scheduler; optionally block until SIGINT/SIGTERM."""
        s = self.settings
        log.info(
            "ContentForge-AI starting (brand=%s, ffmpeg=%s)", s.project.brand, self.ffmpeg.version()
        )
        resumed = self.runner.resume_incomplete()
        if resumed:
            log.info("Resumed %d interrupted job(s)", len(resumed))
        if s.watcher.enabled:
            self.watcher = InputWatcher(
                s.watcher,
                s.paths.input,
                self._on_new_file,
                workers=s.pipeline.max_workers,
                use_polling=use_polling,
            )
            self.watcher.start()
        self.scheduler.register(
            process_queue=self.process_deferred,
            cleanup=self.cleaner.run,
            weekly_analytics=self.weekly_report,
            monthly_report=self.monthly_report,
            health=self.health_check,
        )
        self.scheduler.start()
        self.db.add_event("info", "Daemon started")
        if block:
            self._block()

    def _block(self) -> None:
        def _handler(signum, _frame):
            log.info("Signal %s received - shutting down", signum)
            self._stop.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handler)
            except ValueError:  # not main thread
                pass
        try:
            while not self._stop.is_set():
                time.sleep(0.5)
        finally:
            self.stop()

    def stop(self) -> None:
        if self.watcher:
            self.watcher.stop()
        self.scheduler.stop()
        self.db.add_event("info", "Daemon stopped")
        log.info("ContentForge-AI stopped")

    def close(self) -> None:
        self.db.close()

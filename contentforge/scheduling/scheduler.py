"""APScheduler-based orchestration for the long-running daemon.

Jobs:

* **process queue** - in ``scheduled`` mode the watcher only collects files and
  this cron job triggers processing; in ``immediate`` mode processing starts
  as soon as a file is stable and this job is not registered.
* **daily cleanup** - :class:`~contentforge.cleanup.Cleaner`.
* **weekly analytics** - writes a weekly performance report.
* **monthly report** - writes a monthly report.
* **health** - every 5 min: disk space check + resume of stuck jobs.

The equivalent cron lines for people who prefer system cron are documented in
``docs/scheduling.md``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from contentforge.config.schema import SchedulerConfig
from contentforge.log import get_logger

log = get_logger("scheduler")


class ForgeScheduler:
    def __init__(self, config: SchedulerConfig, timezone: str = "UTC"):
        self.config = config
        self.timezone = timezone
        self._sched = BackgroundScheduler(timezone=timezone)
        self._jobs: dict[str, str] = {}

    def _add_cron(self, job_id: str, cron: str, fn: Callable[[], object]) -> None:
        trigger = CronTrigger.from_crontab(cron, timezone=self.timezone)
        self._sched.add_job(
            _safe(fn, job_id),
            trigger,
            id=job_id,
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=3600,
        )
        self._jobs[job_id] = cron
        log.info("Scheduled %-18s cron='%s'", job_id, cron)

    def register(
        self,
        *,
        process_queue: Callable[[], object] | None = None,
        cleanup: Callable[[], object] | None = None,
        weekly_analytics: Callable[[], object] | None = None,
        monthly_report: Callable[[], object] | None = None,
        health: Callable[[], object] | None = None,
        health_interval_minutes: int = 5,
    ) -> None:
        if not self.config.enabled:
            log.info("Scheduler disabled in config")
            return
        if process_queue and self.config.mode == "scheduled":
            self._add_cron("process_queue", self.config.process_cron, process_queue)
        if cleanup:
            self._add_cron("daily_cleanup", self.config.daily_cleanup_cron, cleanup)
        if weekly_analytics:
            self._add_cron("weekly_analytics", self.config.weekly_analytics_cron, weekly_analytics)
        if monthly_report:
            self._add_cron("monthly_report", self.config.monthly_report_cron, monthly_report)
        if health:
            self._sched.add_job(
                _safe(health, "health"),
                IntervalTrigger(minutes=health_interval_minutes),
                id="health",
                replace_existing=True,
                coalesce=True,
                max_instances=1,
            )
            self._jobs["health"] = f"every {health_interval_minutes} min"

    def start(self) -> None:
        if self.config.enabled and not self._sched.running:
            self._sched.start()

    def stop(self) -> None:
        if self._sched.running:
            self._sched.shutdown(wait=False)

    def next_runs(self) -> dict[str, datetime | None]:
        if not self._sched.running:
            return {k: None for k in self._jobs}
        return {j.id: j.next_run_time for j in self._sched.get_jobs()}

    @property
    def jobs(self) -> dict[str, str]:
        return dict(self._jobs)


def _safe(fn: Callable[[], object], name: str) -> Callable[[], None]:
    def wrapper() -> None:
        log.info("Scheduled task '%s' starting", name)
        try:
            fn()
            log.info("Scheduled task '%s' finished", name)
        except Exception as exc:
            log.exception("Scheduled task '%s' failed: %s", name, exc)

    return wrapper

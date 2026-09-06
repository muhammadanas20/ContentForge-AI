"""Disk hygiene with safety rails.

Rules (all configurable in ``cleanup:``):

* Work directories of **completed** jobs are removed (temp files).
* Work directories of failed/unknown jobs are kept unless older than
  ``archive_retention_days`` *and* not in progress.
* Archived source recordings older than ``archive_retention_days`` are deleted.
* Output packages older than ``output_retention_days`` (0 = never) are deleted.
* Dated log files older than ``logs_retention_days`` are deleted.
* If free disk space drops below ``min_free_disk_gb`` the oldest archives are
  removed first, then oldest outputs, until the threshold is met.
* **Never** touches anything modified in the last ``min_age_minutes`` or any
  job whose status is ``processing``/``queued``.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from contentforge.config.schema import CleanupConfig, PathsConfig
from contentforge.db import Database
from contentforge.log import get_logger
from contentforge.log.logger import prune_old_logs
from contentforge.utils import dir_size_bytes, disk_free_gb, human_size, safe_rmtree

log = get_logger("cleanup")


@dataclass
class CleanupReport:
    removed_work_dirs: list[str] = field(default_factory=list)
    removed_archives: list[str] = field(default_factory=list)
    removed_outputs: list[str] = field(default_factory=list)
    removed_raw: list[str] = field(default_factory=list)
    removed_logs: int = 0
    freed_bytes: int = 0
    skipped: list[str] = field(default_factory=list)
    free_gb_before: float = 0.0
    free_gb_after: float = 0.0
    dry_run: bool = False

    def summary(self) -> str:
        return (
            f"work dirs: {len(self.removed_work_dirs)}, archives: {len(self.removed_archives)}, "
            f"outputs: {len(self.removed_outputs)}, raw: {len(self.removed_raw)}, logs: {self.removed_logs}, "
            f"freed: {human_size(self.freed_bytes)}, free: {self.free_gb_before:.1f} -> {self.free_gb_after:.1f} GB"
            + (" [dry-run]" if self.dry_run else "")
        )


class Cleaner:
    def __init__(self, config: CleanupConfig, paths: PathsConfig, db: Database):
        self.config = config
        self.paths = paths
        self.db = db

    # ---------------------------------------------------------- helpers
    def _too_young(self, path: Path) -> bool:
        try:
            newest = max(
                (f.stat().st_mtime for f in [path, *path.rglob("*")] if f.exists()), default=0
            )
        except OSError:
            return True
        return time.time() - newest < self.config.min_age_minutes * 60

    def _active_job_dirs(self) -> set[str]:
        active = set()
        for status in ("processing", "queued"):
            for j in self.db.list_jobs(status=status, limit=10000):
                if j.get("work_dir"):
                    active.add(Path(j["work_dir"]).name)
        return active

    def _remove(
        self, path: Path, guard: Path, report_list: list[str], report: CleanupReport
    ) -> None:
        size = (
            dir_size_bytes(path) if path.is_dir() else (path.stat().st_size if path.exists() else 0)
        )
        if report.dry_run:
            report_list.append(str(path))
            report.freed_bytes += size
            return
        if safe_rmtree(path, guard):
            report_list.append(str(path))
            report.freed_bytes += size
            log.info("Removed %s (%s)", path, human_size(size))
        else:
            report.skipped.append(f"{path}: outside guard {guard}")

    # ---------------------------------------------------------- actions
    def clean_work_dirs(self, report: CleanupReport) -> None:
        work = Path(self.paths.work)
        if not work.exists():
            return
        active = self._active_job_dirs()
        completed = {
            Path(j["work_dir"]).name
            for j in self.db.list_jobs(status="completed", limit=10000)
            if j.get("work_dir")
        }
        archived = {
            Path(j["work_dir"]).name
            for j in self.db.list_jobs(status="archived", limit=10000)
            if j.get("work_dir")
        }
        stale_cutoff = timedelta(days=self.config.archive_retention_days)
        for d in work.iterdir():
            if not d.is_dir():
                continue
            if d.name in active:
                report.skipped.append(f"{d}: job active")
                continue
            if self._too_young(d):
                report.skipped.append(f"{d}: modified recently")
                continue
            is_done = d.name in completed or d.name in archived
            if is_done and self.config.delete_temp_after_success:
                self._remove(d, work, report.removed_work_dirs, report)
            elif (
                not is_done
                and datetime.fromtimestamp(d.stat().st_mtime) < datetime.now() - stale_cutoff
            ):
                # failed/unknown but ancient -> safe to reclaim
                self._remove(d, work, report.removed_work_dirs, report)
            else:
                report.skipped.append(f"{d}: unfinished work kept")

    def clean_archives(self, report: CleanupReport) -> None:
        self._clean_by_age(
            Path(self.paths.archive),
            self.config.archive_retention_days,
            report.removed_archives,
            report,
            depth=2,
        )

    def clean_outputs(self, report: CleanupReport) -> None:
        if self.config.output_retention_days > 0:
            self._clean_by_age(
                Path(self.paths.output),
                self.config.output_retention_days,
                report.removed_outputs,
                report,
                depth=1,
            )

    def _clean_by_age(
        self, root: Path, days: int, report_list: list[str], report: CleanupReport, depth: int
    ) -> None:
        if days <= 0 or not root.exists():
            return
        cutoff = datetime.now() - timedelta(days=days)
        pattern = "/".join(["*"] * depth)
        for d in sorted(root.glob(pattern)):
            if not d.is_dir() or d.name == "reports":
                continue
            if datetime.fromtimestamp(d.stat().st_mtime) < cutoff and not self._too_young(d):
                self._remove(d, root, report_list, report)
        # remove empty month folders
        if depth == 2 and not report.dry_run:
            for m in root.glob("*"):
                if m.is_dir() and not any(m.iterdir()):
                    m.rmdir()

    def clean_raw_inputs(self, report: CleanupReport) -> None:
        """Delete raw recordings whose job completed (only when configured)."""
        if not self.config.delete_raw_after_success:
            return
        inp = Path(self.paths.input)
        for j in self.db.list_jobs(status="completed", limit=10000) + self.db.list_jobs(
            status="archived", limit=10000
        ):
            src = Path(j["source_path"])
            if src.exists() and inp in src.resolve().parents and not self._too_young(src):
                self._remove(src, inp, report.removed_raw, report)

    def ensure_free_space(self, report: CleanupReport) -> None:
        target = self.config.min_free_disk_gb
        data_dir = Path(self.paths.data_dir)
        if disk_free_gb(data_dir) >= target:
            return
        log.warning(
            "Free disk %.1f GB below minimum %.1f GB - reclaiming oldest archives/outputs",
            disk_free_gb(data_dir),
            target,
        )
        candidates: list[Path] = []
        arch = Path(self.paths.archive)
        if arch.exists():
            candidates += sorted(
                (p for p in arch.glob("*/*") if p.is_dir()), key=lambda p: p.stat().st_mtime
            )
        out = Path(self.paths.output)
        if out.exists():
            candidates += sorted(
                (p for p in out.iterdir() if p.is_dir() and p.name != "reports"),
                key=lambda p: p.stat().st_mtime,
            )
        for d in candidates:
            if disk_free_gb(data_dir) >= target:
                break
            if self._too_young(d):
                continue
            guard = arch if arch in d.parents else out
            lst = report.removed_archives if guard == arch else report.removed_outputs
            self._remove(d, guard, lst, report)

    # -------------------------------------------------------------- run
    def run(self, *, dry_run: bool = False) -> CleanupReport:
        report = CleanupReport(dry_run=dry_run)
        report.free_gb_before = disk_free_gb(self.paths.data_dir)
        if not self.config.enabled:
            log.info("Cleanup disabled in config")
            return report
        self.clean_work_dirs(report)
        self.clean_archives(report)
        self.clean_outputs(report)
        self.clean_raw_inputs(report)
        self.ensure_free_space(report)
        if not dry_run:
            report.removed_logs = prune_old_logs(
                Path(self.paths.logs), self.config.logs_retention_days
            )
        report.free_gb_after = disk_free_gb(self.paths.data_dir)
        log.info("Cleanup finished: %s", report.summary())
        self.db.add_event("info", f"Cleanup: {report.summary()}")
        return report

    def storage_overview(self) -> dict[str, int]:
        return {
            name: dir_size_bytes(getattr(self.paths, name))
            for name in ("input", "work", "output", "archive", "logs", "models")
        }


def disk_usage(path: Path) -> tuple[int, int, int]:
    """(total, used, free) bytes for the filesystem containing ``path``."""
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    u = shutil.disk_usage(p)
    return u.total, u.used, u.free

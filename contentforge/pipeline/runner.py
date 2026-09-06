"""Pipeline runner: executes steps in order with retries, persistence and resume.

Resume semantics:

* Every completed step is recorded both in ``state.json`` (work dir) and in
  the ``job_steps`` table.
* When a job is (re)started, steps whose name is in ``completed_steps`` and
  whose artefacts still exist are skipped.
* ``--from-step`` / ``force_from`` lets the operator re-run from a given step
  (e.g. after changing subtitle style) without redoing transcription.
"""

from __future__ import annotations

import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from contentforge.config.schema import Settings
from contentforge.db import Database
from contentforge.log import get_logger
from contentforge.pipeline.context import JobContext
from contentforge.pipeline.steps import DEFAULT_STEPS, Step
from contentforge.utils import FFmpeg, RetryError, retry

log = get_logger("pipeline")


class PipelineError(RuntimeError):
    def __init__(self, step: str, message: str):
        super().__init__(f"[{step}] {message}")
        self.step = step


@dataclass
class JobResult:
    job_id: str
    slug: str
    status: str
    output_dir: Path | None = None
    error: str | None = None
    step_times: dict[str, float] = field(default_factory=dict)
    total_seconds: float = 0.0


ProgressCallback = Callable[[str, str, str], None]  # (job_id, step, status)


class PipelineRunner:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        ffmpeg: FFmpeg | None = None,
        steps: list[type[Step]] | None = None,
        on_progress: ProgressCallback | None = None,
    ):
        self.settings = settings
        self.db = db
        self.ff = ffmpeg or FFmpeg()
        self.step_classes = steps or DEFAULT_STEPS
        self.on_progress = on_progress
        self._lock = threading.Lock()
        self._cancel: set[str] = set()

    # ---------------------------------------------------------- intake
    def create_job(self, source: Path, *, website_hint: str = "") -> JobContext:
        source = Path(source).resolve()
        ctx = JobContext.create(source, self.settings.paths.work)
        if website_hint:
            ctx.data["website_hint"] = website_hint
        ctx.save()
        self.db.create_job(
            ctx.job_id,
            str(source),
            source_hash=ctx.data["source_hash"],
            slug=ctx.slug,
            work_dir=str(ctx.work_dir),
            meta={"website_hint": website_hint},
        )
        self.db.add_event("info", f"Job created for {source.name}", ctx.job_id)
        log.info("Created job %s for %s (work: %s)", ctx.job_id, source.name, ctx.work_dir.name)
        return ctx

    def find_existing(self, source: Path) -> dict | None:
        """Return an existing job for the same file content (dedupe re-drops)."""
        from contentforge.utils import file_sha1

        h = file_sha1(source, max_bytes=64 << 20)
        return self.db.find_job_by_hash(h)

    def load_job(self, job_id: str) -> JobContext | None:
        job = self.db.get_job(job_id)
        if not job or not job.get("work_dir"):
            return None
        ctx = JobContext.load(Path(job["work_dir"]))
        if ctx is None:
            log.warning("state.json missing for job %s; rebuilding context", job_id)
            src = Path(job["source_path"])
            ctx = JobContext(
                job_id=job_id, source=src, work_dir=Path(job["work_dir"]), slug=job["slug"]
            )
            ctx.work_dir.mkdir(parents=True, exist_ok=True)
            ctx.save()
        return ctx

    def cancel(self, job_id: str) -> None:
        self._cancel.add(job_id)

    # ------------------------------------------------------------- run
    def run(self, ctx: JobContext, *, force_from: str | None = None) -> JobResult:
        t0 = time.time()
        result = JobResult(job_id=ctx.job_id, slug=ctx.slug, status="processing")
        self.db.update_job(ctx.job_id, status="processing", started_at=_now_iso(), error=None)
        self.db.execute("UPDATE jobs SET attempts = attempts + 1 WHERE id = ?", (ctx.job_id,))
        if force_from:
            self._reset_from(ctx, force_from)

        steps = [cls(self.settings, self.db, self.ff) for cls in self.step_classes]
        pending = {st.name for st in steps if st.name not in ctx.completed_steps}
        try:
            for step in steps:
                if ctx.job_id in self._cancel:
                    raise PipelineError(step.name, "cancelled by operator")
                if step.name in ctx.completed_steps and self._artifacts_present(
                    ctx, step.name, pending
                ):
                    log.info("step %-14s skip (already done)", step.name)
                    continue
                if not step.enabled:
                    log.info("step %-14s disabled in config", step.name)
                    if step.name not in ctx.skipped_steps:
                        ctx.skipped_steps.append(step.name)
                    self.db.step_skipped(ctx.job_id, step.name)
                    ctx.save()
                    continue
                self._run_step(ctx, step, result)
            status = "archived" if "archive" in ctx.completed_steps else "completed"
            result.status = status
            result.output_dir = Path(ctx.data["output_dir"]) if ctx.data.get("output_dir") else None
            self.db.update_job(
                ctx.job_id,
                status=status,
                finished_at=_now_iso(),
                current_step=None,
                meta={
                    **(self.db.get_job(ctx.job_id) or {}).get("meta", {}),
                    "final": ctx.data.get("final"),
                    "sync": ctx.data.get("sync"),
                },
            )
            self.db.add_event("info", f"Job finished -> {result.output_dir}", ctx.job_id)
            log.info(
                "Job %s finished in %.1fs -> %s", ctx.job_id, time.time() - t0, result.output_dir
            )
        except PipelineError as exc:
            result.status = "failed"
            result.error = str(exc)
            self.db.update_job(ctx.job_id, status="failed", error=str(exc), finished_at=_now_iso())
            self.db.add_event("error", str(exc), ctx.job_id)
            log.error("Job %s failed: %s", ctx.job_id, exc)
        finally:
            self._cancel.discard(ctx.job_id)
            result.total_seconds = time.time() - t0
            ctx.save()
        return result

    def _run_step(self, ctx: JobContext, step: Step, result: JobResult) -> None:
        name = step.name
        self.db.step_started(ctx.job_id, name)
        self._notify(ctx.job_id, name, "running")
        log.info("step %-14s start", name)
        t0 = time.time()
        attempts = 1 + max(0, self.settings.pipeline.retries)
        try:
            outputs = retry(
                lambda: step.run(ctx),
                attempts=attempts,
                backoff=self.settings.pipeline.retry_backoff_seconds,
                exceptions=(Exception,),
                label=f"step {name}",
            )
        except RetryError as exc:
            err = exc.last_exception
            tb = "".join(traceback.format_exception(type(err), err, err.__traceback__))
            log.debug("Traceback for %s:\n%s", name, tb)
            self.db.step_failed(ctx.job_id, name, f"{type(err).__name__}: {err}")
            self._notify(ctx.job_id, name, "failed")
            raise PipelineError(name, f"{type(err).__name__}: {err}") from err
        dt = time.time() - t0
        result.step_times[name] = dt
        if name not in ctx.completed_steps:
            ctx.completed_steps.append(name)
        ctx.save()
        self.db.step_finished(ctx.job_id, name, outputs=outputs, duration=dt)
        self._notify(ctx.job_id, name, "done")
        log.info("step %-14s done in %.1fs %s", name, dt, _short(outputs))

    # ---------------------------------------------------------- resume
    # artefacts a completed step must still provide; large intermediates are
    # deleted early (low-disk design), so they only count when a step that
    # consumes them still has to run.
    _REQUIRED: dict[str, list[str]] = {
        "extract_audio": ["source_audio"],
        "transcribe": ["transcript_json"],
        "script": ["script_json"],
        "render_cut": ["cut_video"],
        "sync_audio": ["mixed_audio", "synced_video"],
        "subtitles": ["ass"],
        "render_final": ["final_video"],
        "thumbnail": ["thumbnail"],
        "social": ["social_json"],
    }
    _CONSUMERS: dict[str, set[str]] = {
        "cut_video": {"sync_audio", "render_final", "thumbnail"},
        "synced_video": {"render_final", "thumbnail"},
    }

    def _artifacts_present(
        self, ctx: JobContext, step_name: str, pending: set[str] | None = None
    ) -> bool:
        for key in self._REQUIRED.get(step_name, []):
            consumers = self._CONSUMERS.get(key)
            if consumers is not None and pending is not None and not (consumers & pending):
                continue  # nobody left to use it - fine that it was cleaned up
            if not ctx.has_artifact(key):
                log.info("step %-14s artefact %r missing - re-running", step_name, key)
                if pending is not None:
                    pending.add(step_name)
                return False
        return True

    def _reset_from(self, ctx: JobContext, step_name: str) -> None:
        names = [cls.name for cls in self.step_classes]
        if step_name not in names:
            raise ValueError(f"Unknown step '{step_name}'. Valid: {', '.join(names)}")
        idx = names.index(step_name)
        drop = set(names[idx:])
        ctx.completed_steps = [s for s in ctx.completed_steps if s not in drop]
        ctx.skipped_steps = [s for s in ctx.skipped_steps if s not in drop]
        ctx.save()
        log.info("Reset job %s to re-run from '%s'", ctx.job_id, step_name)

    def resume_incomplete(self) -> list[JobResult]:
        """Resume every job left in ``processing``/``queued`` (e.g. after a crash)."""
        results = []
        for status in ("processing", "queued"):
            for job in self.db.list_jobs(status=status, limit=1000):
                src = Path(job["source_path"])
                if not src.exists():
                    self.db.update_job(
                        job["id"], status="failed", error="source file missing on resume"
                    )
                    continue
                ctx = self.load_job(job["id"])
                if ctx is None:
                    continue
                log.info(
                    "Resuming job %s (%s) from step %s",
                    job["id"],
                    job["slug"],
                    job.get("current_step"),
                )
                results.append(self.run(ctx))
        return results

    def _notify(self, job_id: str, step: str, status: str) -> None:
        if self.on_progress:
            try:
                self.on_progress(job_id, step, status)
            except Exception:  # pragma: no cover
                pass

    @property
    def step_names(self) -> list[str]:
        return [cls.name for cls in self.step_classes]


def _now_iso() -> str:
    from contentforge.db import utcnow

    return utcnow()


def _short(d: dict) -> str:
    items = []
    for k, v in (d or {}).items():
        s = str(v)
        items.append(f"{k}={s if len(s) < 40 else s[:37] + '...'}")
    return "(" + ", ".join(items[:4]) + ")" if items else ""

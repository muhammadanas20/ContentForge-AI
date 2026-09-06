"""Watcher, scheduler, app façade and CLI tests."""

import threading
import time
from pathlib import Path

import pytest

from contentforge.config.schema import SchedulerConfig, WatcherConfig
from contentforge.input import InputWatcher
from contentforge.scheduling import ForgeScheduler


def test_watcher_detects_and_dedupes(tmp_path):
    inp = tmp_path / "in"
    inp.mkdir()
    seen: list[Path] = []
    lock = threading.Lock()

    def handler(p: Path):
        with lock:
            seen.append(p)

    (inp / "existing.mp4").write_bytes(b"a" * 100)
    (inp / "ignore.txt").write_text("x")
    cfg = WatcherConfig(stable_seconds=0.3, poll_interval=0.1, process_existing_on_start=True)
    w = InputWatcher(cfg, inp, handler, workers=2, use_polling=True)
    w.start()
    try:
        # simulate a recorder writing progressively
        f = inp / "new.mkv"
        with f.open("wb") as fh:
            for _ in range(3):
                fh.write(b"b" * 50)
                fh.flush()
                time.sleep(0.15)
        (inp / ".hidden.mp4").write_bytes(b"x")
        (inp / "partial.mp4.part").write_bytes(b"x")
        assert w.wait_idle(timeout=10)
        time.sleep(1.0)  # let late modify events settle
        assert w.wait_idle(timeout=5)
    finally:
        w.stop()
    names = sorted(p.name for p in seen)
    assert names == ["existing.mp4", "new.mkv"]
    assert not w.enqueue(inp / "existing.mp4")  # already done -> deduped
    assert w.queue_size == 0


def test_scheduler_registers_and_runs(tmp_path):
    cfg = SchedulerConfig(mode="scheduled", process_cron="* * * * *")
    sched = ForgeScheduler(cfg, "UTC")
    ran = threading.Event()
    sched.register(
        process_queue=lambda: ran.set(),
        cleanup=lambda: None,
        weekly_analytics=lambda: None,
        monthly_report=lambda: None,
        health=lambda: None,
        health_interval_minutes=1,
    )
    assert set(sched.jobs) == {
        "process_queue",
        "daily_cleanup",
        "weekly_analytics",
        "monthly_report",
        "health",
    }
    sched.start()
    try:
        runs = sched.next_runs()
        assert runs["process_queue"] is not None
    finally:
        sched.stop()
    immediate = ForgeScheduler(SchedulerConfig(mode="immediate"), "UTC")
    immediate.register(process_queue=lambda: None, cleanup=lambda: None)
    assert "process_queue" not in immediate.jobs and "daily_cleanup" in immediate.jobs
    disabled = ForgeScheduler(SchedulerConfig(enabled=False), "UTC")
    disabled.register(cleanup=lambda: None)
    assert disabled.jobs == {}


def test_scheduler_safe_wrapper_swallows_errors():
    from contentforge.scheduling.scheduler import _safe

    _safe(lambda: 1 / 0, "boom")()  # must not raise


@pytest.mark.ffmpeg
def test_app_process_file_dedupe_and_retry(settings, ffmpeg, sample_video, monkeypatch):
    import sys

    import contentforge.pipeline.steps as sm

    sys.path.insert(0, str(Path(__file__).parent))
    from test_pipeline import FakeTranscriber, FakeTTS

    monkeypatch.setattr(sm, "Transcriber", FakeTranscriber)
    monkeypatch.setattr(sm, "get_tts_engine", lambda cfg: FakeTTS(ffmpeg))
    s = settings
    s.video.width, s.video.height, s.video.fps, s.video.preset, s.video.crf = (
        540,
        960,
        24,
        "ultrafast",
        30,
    )
    s.pipeline.retries = 0
    s.pipeline.steps.archive = False
    s.pipeline.cleanup_work_on_success = False
    from contentforge.app import ContentForgeApp

    app = ContentForgeApp(s, console_logging=False)
    src = s.paths.input / "tool.mp4"
    src.write_bytes(sample_video.read_bytes())
    r1 = app.process_file(src)
    assert r1.status == "completed", r1.error
    # identical content dropped again under another name -> deduped, no new job
    dup = s.paths.input / "tool copy.mp4"
    dup.write_bytes(sample_video.read_bytes())
    r2 = app.process_file(dup)
    assert r2.job_id == r1.job_id and len(app.db.list_jobs()) == 1
    r3 = app.process_file(dup, force=True)
    assert r3.job_id != r1.job_id and r3.status == "completed"
    r4 = app.retry_job(r1.job_id, from_step="social")
    assert r4.status == "completed" and set(r4.step_times) == {"social", "package", "analytics"}
    with pytest.raises(ValueError):
        app.retry_job("nope")
    health = app.health_check()
    assert health["jobs"]["completed"] == 2
    assert Path(app.weekly_report()["md"]).exists()
    assert Path(app.monthly_report()["md"]).exists()
    app.close()


def test_cli_doctor_steps_config(settings, capsys, monkeypatch):
    from contentforge import cli

    monkeypatch.setattr(cli, "load_settings", lambda *a, **k: settings)
    assert cli.main(["steps"]) == 0
    out = capsys.readouterr().out
    assert "transcribe" in out and "render_final" in out
    assert cli.main(["config", "video"]) == 0
    assert '"width": 1080' in capsys.readouterr().out
    code = cli.main(["doctor"])
    assert code in (0, 1)
    assert "ffmpeg" in capsys.readouterr().out


def test_cli_jobs_and_cleanup(settings, capsys, monkeypatch):
    from contentforge import cli
    from contentforge.db import Database

    monkeypatch.setattr(cli, "load_settings", lambda *a, **k: settings)
    db = Database(settings.paths.db)
    db.create_job("abc123", "/x/a.mp4", slug="a")
    db.close()
    assert cli.main(["jobs", "--json"]) == 0
    assert "abc123" in capsys.readouterr().out
    assert cli.main(["jobs", "abc123"]) == 0
    assert cli.main(["-q", "cleanup", "--dry-run", "-v"]) == 0
    assert "work dirs" in capsys.readouterr().out
    assert (
        cli.main(
            [
                "-q",
                "analytics",
                "add",
                "abc123",
                "--views",
                "10",
                "--likes",
                "2",
                "--completion",
                "55",
            ]
        )
        == 0
    )
    assert cli.main(["-q", "analytics", "report"]) == 0
    assert "Analytics report" in capsys.readouterr().out
    assert cli.main(["-q", "process", str(settings.paths.input / "missing.mp4")]) == 1

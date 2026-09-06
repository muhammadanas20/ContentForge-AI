"""Cleanup, archive, packager and analytics tests."""

import os
import time
from pathlib import Path

from contentforge.analytics import AnalyticsService
from contentforge.archive import Archiver
from contentforge.cleanup import Cleaner
from contentforge.db import Database
from contentforge.output import UploadPackager


def _age(path: Path, days: float) -> None:
    t = time.time() - days * 86400
    for p in [path, *path.rglob("*")]:
        os.utime(p, (t, t))


def test_cleaner_respects_safety_rules(settings):
    db = Database(settings.paths.db)
    work = settings.paths.work
    done = work / "job-done"
    done.mkdir()
    (done / "a.bin").write_bytes(b"x" * 100)
    active = work / "job-active"
    active.mkdir()
    (active / "a.bin").write_bytes(b"x")
    fresh = work / "job-fresh"
    fresh.mkdir()
    (fresh / "a.bin").write_bytes(b"x")
    failed_old = work / "job-failed"
    failed_old.mkdir()
    (failed_old / "a.bin").write_bytes(b"x")
    failed_new = work / "job-failed2"
    failed_new.mkdir()
    (failed_new / "a.bin").write_bytes(b"x")
    db.create_job("done", "/x/done.mp4", work_dir=str(done))
    db.update_job("done", status="completed")
    db.create_job("active", "/x/active.mp4", work_dir=str(active))
    db.update_job("active", status="processing")
    db.create_job("fresh", "/x/fresh.mp4", work_dir=str(fresh))
    db.update_job("fresh", status="completed")
    db.create_job("failed", "/x/f.mp4", work_dir=str(failed_old))
    db.update_job("failed", status="failed")
    db.create_job("failed2", "/x/f2.mp4", work_dir=str(failed_new))
    db.update_job("failed2", status="failed")
    _age(done, 1)
    _age(active, 5)
    _age(failed_old, 60)
    _age(failed_new, 2)

    arch_old = settings.paths.archive / "2025-01" / "old"
    arch_old.mkdir(parents=True)
    (arch_old / "v.mp4").write_bytes(b"x" * 10)
    arch_new = settings.paths.archive / "2026-09" / "new"
    arch_new.mkdir(parents=True)
    (arch_new / "v.mp4").write_bytes(b"x")
    _age(arch_old, 45)
    _age(arch_new, 2)
    (settings.paths.logs / "contentforge-2020-01-01.log").write_text("old")

    settings.cleanup.min_free_disk_gb = 0
    cleaner = Cleaner(settings.cleanup, settings.paths, db)
    dry = cleaner.run(dry_run=True)
    assert done.exists() and str(done) in dry.removed_work_dirs

    rep = cleaner.run()
    assert not done.exists()  # completed + old enough -> removed
    assert active.exists()  # processing -> kept
    assert fresh.exists()  # modified recently -> kept
    assert not failed_old.exists()  # failed but ancient -> reclaimed
    assert failed_new.exists()  # failed, recent -> kept (unfinished work)
    assert not arch_old.exists() and arch_new.exists()
    assert not (settings.paths.archive / "2025-01").exists()
    assert rep.removed_logs == 1
    assert rep.freed_bytes >= 110
    assert any("job active" in s for s in rep.skipped)
    assert "work dirs: 2" in rep.summary()
    assert set(cleaner.storage_overview()) >= {"work", "archive", "output"}
    db.close()


def test_cleaner_low_disk_removes_oldest_first(settings, monkeypatch):
    db = Database(settings.paths.db)
    a = settings.paths.archive / "2025-01" / "a"
    a.mkdir(parents=True)
    (a / "f").write_bytes(b"1")
    b = settings.paths.archive / "2025-02" / "b"
    b.mkdir(parents=True)
    (b / "f").write_bytes(b"1")
    _age(a, 400)
    _age(b, 300)
    settings.cleanup.archive_retention_days = 0  # disable age-based deletion
    settings.cleanup.min_free_disk_gb = 10**6  # impossible target -> reclaim everything eligible
    calls = {"n": 0}
    import contentforge.cleanup.cleaner as mod

    real = mod.disk_free_gb

    def fake_free(p):
        calls["n"] += 1
        # pretend the disk is full until the first archive is removed
        return real(p) if not a.exists() else 0.0

    monkeypatch.setattr(mod, "disk_free_gb", fake_free)
    settings.cleanup.min_free_disk_gb = 1
    Cleaner(settings.cleanup, settings.paths, db).run()
    assert not a.exists() and b.exists()
    db.close()


def test_archiver_and_packager(settings, tmp_path):
    src = settings.paths.input / "rec.mp4"
    src.write_bytes(b"video")
    arch = Archiver(settings.paths.archive)
    dest = arch.archive_job(
        job_id="j", slug="rec", source=src, delete_source=True, metadata={"k": 1}
    )
    assert not src.exists() and (dest / "rec.mp4").exists() and (dest / "archive.json").exists()
    assert arch.list_archives() == [dest]

    final = tmp_path / "final.mp4"
    final.write_bytes(b"mp4")
    thumb = tmp_path / "t.jpg"
    thumb.write_bytes(b"jpg")
    pk = UploadPackager(settings.paths.output)
    out = pk.build(
        slug="rec",
        final_video=final,
        files={"thumbnail": thumb, "missing": tmp_path / "nope"},
        caption_text="hello #x",
        manifest_extra={"title": "T"},
    )
    assert (out / "rec.mp4").exists() and (out / "cover.jpg").exists()
    assert (out / "caption.txt").read_text() == "hello #x\n"
    assert (out / "manifest.json").exists() and (out / "README.md").exists()


def test_analytics_report(settings, tmp_path):
    db = Database(settings.paths.db)
    for i in range(4):
        db.create_job(f"j{i}", f"/x/{i}.mp4", slug=f"v{i}")
        db.update_job(f"j{i}", title=f"Video {i}", status="completed")
    svc = AnalyticsService(settings.analytics, db)
    svc.record("j0", "instagram", views=5000, likes=400, comments=40, shares=80, completion_rate=65)
    svc.record("j1", "instagram", views=800, likes=10, comments=0, shares=1, completion_rate=0.2)
    svc.record("j2", "youtube_shorts", views=200, likes=5, completion_rate=0.5)
    svc.record("j3", "instagram", views=100, likes=1, completion_rate=0.1, follower_growth=0)
    svc.record("j1", "instagram", views=900, likes=12, completion_rate=0.22)  # newer row supersedes
    perfs = svc.performances()
    assert len(perfs) == 4
    best = max(perfs, key=lambda p: p.score)
    assert best.job_id == "j0" and best.completion_rate == 0.65
    assert next(p for p in perfs if p.job_id == "j1").views == 900
    report = svc.build_report("test")
    assert report.top_videos[0] == "Video 0"
    assert report.suggestions and any("Best performer" in s for s in report.suggestions)
    files = svc.write_report(report, "r")
    assert files["md"].exists() and "| Video 0" in files["md"].read_text()

    csv_path = tmp_path / "m.csv"
    csv_path.write_text("job_id,platform,views,likes\nj2,instagram,50,2\n")
    assert svc.import_csv(csv_path) == 1
    assert (
        AnalyticsService(settings.analytics, Database(tmp_path / "empty.db"))
        .suggestions([])[0]
        .startswith("No metrics")
    )
    db.close()

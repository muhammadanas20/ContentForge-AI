"""Database layer tests (SQLite)."""

from contentforge.db import Database


def test_job_lifecycle(tmp_path):
    db = Database(tmp_path / "t.sqlite3")
    job = db.create_job("j1", "/in/a.mp4", source_hash="abc", slug="a", meta={"k": 1})
    assert job["status"] == "queued" and job["meta"] == {"k": 1}

    db.step_started("j1", "transcribe")
    steps = db.get_steps("j1")
    assert steps["transcribe"]["status"] == "running" and steps["transcribe"]["attempts"] == 1
    db.step_failed("j1", "transcribe", "oops")
    db.step_started("j1", "transcribe")
    assert db.get_steps("j1")["transcribe"]["attempts"] == 2
    db.step_finished("j1", "transcribe", outputs={"srt": "x.srt"}, duration=1.5)
    assert db.get_steps("j1")["transcribe"]["outputs"] == {"srt": "x.srt"}
    db.step_skipped("j1", "narration")
    assert db.get_steps("j1")["narration"]["status"] == "skipped"

    db.update_job("j1", status="completed", meta={"done": True})
    assert db.get_job("j1")["status"] == "completed"
    assert db.get_job("j1")["meta"] == {"done": True}
    assert db.find_job_by_hash("abc")["id"] == "j1"
    assert db.find_job_by_source("/in/a.mp4")["id"] == "j1"
    assert db.count_by_status() == {"completed": 1}
    assert [j["id"] for j in db.list_jobs(status="completed")] == ["j1"]

    db.delete_job("j1")
    assert db.get_job("j1") is None
    db.close()


def test_metrics_hashtags_events(tmp_path):
    db = Database(tmp_path / "t.sqlite3")
    db.create_job("j1", "/in/a.mp4")
    db.add_metrics("j1", "instagram", views=100, likes=5, recorded_at="2026-01-01T00:00:00")
    db.add_metrics("j1", "instagram", views=200, likes=9, recorded_at="2026-01-02T00:00:00")
    latest = db.latest_metrics_per_job()
    assert len(latest) == 1 and latest[0]["views"] == 200
    assert len(db.list_metrics("j1")) == 2

    db.add_hashtag_set("j1", ["#a", "#b"])
    db.add_hashtag_set("j1", ["#c"])
    assert db.recent_hashtag_sets(1) == [["#c"]]

    db.add_event("error", "bad thing", "j1")
    db.add_event("info", "fine")
    assert len(db.recent_events(level="error")) == 1
    assert len(db.recent_events()) == 2
    db.close()

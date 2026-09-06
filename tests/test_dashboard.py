"""Smoke-test every dashboard page with Streamlit's AppTest (no browser needed)."""

from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

APP = Path(__file__).resolve().parents[1] / "contentforge" / "dashboard" / "app.py"


@pytest.fixture()
def seeded(settings, monkeypatch):
    from contentforge.db import Database

    db = Database(settings.paths.db)
    db.create_job("j1", "/x/a.mp4", slug="a")
    db.update_job(
        "j1", status="completed", title="Video A", output_dir=str(settings.paths.output / "a")
    )
    (settings.paths.output / "a").mkdir(parents=True)
    (settings.paths.output / "a" / "caption.txt").write_text("cap")
    db.create_job("j2", "/x/b.mp4", slug="b")
    db.update_job("j2", status="failed", error="boom", current_step="render_cut")
    db.step_started("j1", "probe")
    db.step_finished("j1", "probe")
    db.add_metrics("j1", "instagram", views=100, likes=10, completion_rate=0.5)
    db.add_event("error", "boom", "j2")
    db.close()
    (settings.paths.logs / "contentforge-2026-09-06.log").write_text(
        "2026-09-06 | INFO | x | hello\n"
    )
    monkeypatch.setenv("CONTENTFORGE_DATA_DIR", str(settings.paths.data_dir))
    import contentforge.dashboard  # noqa: F401

    return settings


@pytest.mark.parametrize(
    "page", ["Overview", "Completed", "Errors", "Logs", "Analytics", "Storage", "System health"]
)
def test_dashboard_pages_render(seeded, page):
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, at.exception
    at.sidebar.radio[0].set_value(page).run()
    assert not at.exception, at.exception
    assert at.title[0].value == page

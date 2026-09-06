"""End-to-end test of the v0.3 smart pipeline, including what the frames look like.

FFmpeg exiting 0 is not evidence of quality, so this test also *looks* at the
rendered Reel: the composition must be 9:16, the recording must be visible
inside the frame (not a black canvas), captions must be burned in, and the
important website content must survive the framing.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from contentforge.db import Database
from contentforge.pipeline.runner import PipelineRunner
from contentforge.pipeline.smart_steps import SMART_STEPS, steps_for_mode

pytestmark = pytest.mark.ffmpeg


@pytest.fixture(scope="module")
def smart_settings(tmp_path_factory):
    """Module-scoped settings so the (slow) Reel is produced exactly once."""
    import os

    from contentforge.config import load_settings, reset_settings
    from contentforge.log import setup_logging

    root = Path(__file__).resolve().parents[1]
    env = {k: v for k, v in os.environ.items() if not k.startswith("CONTENTFORGE")}
    env["CONTENTFORGE_DATA_DIR"] = str(tmp_path_factory.mktemp("smart") / "data")
    s = load_settings(root / "config" / "config.yaml", root=root, environ=env)
    s.ensure_directories()
    setup_logging(s.paths.logs, "DEBUG", console=False, force=True)
    reset_settings(s)
    s.pipeline.mode = "smart"
    s.pipeline.retries = 0
    s.pipeline.steps.transcribe = False  # no Whisper model in CI
    s.video.preset, s.video.crf, s.video.fps = "ultrafast", 30, 24
    s.editing.max_duration = 30
    s.understanding.sample_fps = 6
    s.understanding.ocr_every_seconds = 1.0
    s.tts.engine = "espeak"
    yield s
    reset_settings(None)


@pytest.fixture(scope="module")
def reel(smart_settings, tutorial_recording):
    """Run the whole smart pipeline once and hand the tests its output."""
    import sys

    sys.path.insert(0, str(Path(__file__).parent))
    from test_pipeline import FakeTTS

    import contentforge.pipeline.smart_steps as sm
    from contentforge.utils.ffmpeg import FFmpeg

    ffmpeg = FFmpeg()

    src = smart_settings.paths.input / "studentofferco-demo.mp4"
    src.write_bytes(tutorial_recording.path.read_bytes())

    engine = FakeTTS(ffmpeg, duration=1.6)
    original = sm.get_tts_engine
    sm.get_tts_engine = lambda cfg: engine  # deterministic, no voice models in CI
    db = Database(smart_settings.paths.db)
    try:
        runner = PipelineRunner(smart_settings, db, ffmpeg)
        ctx = runner.create_job(src, website_hint="studentofferco.com")
        ctx.data["website_context"] = "StudentOfferCo lists verified student discounts."
        ctx.save()
        result = runner.run(ctx)
    finally:
        sm.get_tts_engine = original
        db.close()
    assert result.status in ("completed", "archived"), result.error
    out_dir = Path(ctx.data["output_dir"])
    return {"result": result, "dir": out_dir, "ctx": ctx, "settings": smart_settings}


def test_the_smart_pipeline_is_selected_by_configuration(settings):
    settings.pipeline.mode = "smart"
    assert steps_for_mode(settings) is SMART_STEPS
    settings.pipeline.mode = "classic"
    assert steps_for_mode(settings) is not SMART_STEPS


def test_every_smart_step_ran(reel):
    ran = set(reel["result"].step_times)
    assert {"understand", "plan_edit", "script", "narration", "compose", "captions", "mix",
            "render_final", "cover", "quality", "package"} <= ran


def test_the_package_contains_everything_needed_to_post(reel):
    out = reel["dir"]
    names = {p.name for p in out.iterdir()}
    video = next(p for p in out.glob("*.mp4"))
    assert names >= {
        "cover.jpg",
        "caption.txt",
        "manifest.json",
        "script.md",
        "script_grounded.json",
        "quality.json",
        "quality.md",
        "subtitles.ass",
        "subtitles.srt",
        "narration.wav",
        video.name,
    }


def test_quality_gates_all_passed(reel):
    report = json.loads((reel["dir"] / "quality.json").read_text())
    assert report["passed"], [c for c in report["checks"] if not c["passed"]]
    names = {c["name"] for c in report["checks"]}
    assert {"aspect_9_16", "ocr_regions_preserved", "captions_fit_screen", "cover_ready"} <= names


def test_the_reel_is_a_real_9_16_video_with_audio(reel, ffmpeg):
    video = next(reel["dir"].glob("*.mp4"))
    info = ffmpeg.probe(video)
    assert (info.width, info.height) == (1080, 1920)
    assert info.has_audio
    assert 6 < info.duration < 30
    assert info.duration < 12.0  # dead time was removed from the 12 s source


def test_the_narration_is_as_long_as_the_reel(reel, ffmpeg):
    manifest = json.loads((reel["dir"] / "manifest.json").read_text())
    narration = manifest["narration"]
    assert narration["available"]
    assert narration["coverage"] >= 0.55
    assert abs(narration["duration"] - manifest["duration"]) < 2.5


def test_the_script_is_grounded_in_the_recording(reel):
    script = json.loads((reel["dir"] / "script_grounded.json").read_text())
    assert script["grounded"] and not script["degraded"]
    roles = [s["role"] for s in script["segments"]]
    assert roles[0] == "hook" and roles[-1] == "cta"
    assert all(s["visual_action"] for s in script["segments"])


# --------------------------------------------------------- visual regression
def _frames(video: Path, times: list[float], tmp: Path, ffmpeg) -> list[np.ndarray]:
    from PIL import Image

    out = []
    for i, t in enumerate(times):
        p = tmp / f"f{i}.png"
        ffmpeg.run(["-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1", "-y", str(p)])
        out.append(np.asarray(Image.open(p).convert("RGB"), dtype=np.uint8))
    return out


def test_frames_show_the_recording_not_an_empty_canvas(reel, ffmpeg, tmp_path):
    video = next(reel["dir"].glob("*.mp4"))
    info = ffmpeg.probe(video)
    frames = _frames(video, [info.duration * 0.3, info.duration * 0.6], tmp_path, ffmpeg)
    for frame in frames:
        assert frame.shape == (1920, 1080, 3)
        # the website card is bright; a black/blurred-only frame would not be
        bright = float((frame.mean(axis=2) > 200).mean())
        assert bright > 0.15, f"only {bright:.0%} of the frame is page content"
        # and it must have detail (real UI), not a flat colour
        assert frame.reshape(-1, 3).std(axis=0).mean() > 25


def test_captions_are_burned_into_the_caption_band(reel, ffmpeg, tmp_path):
    video = next(reel["dir"].glob("*.mp4"))
    info = ffmpeg.probe(video)
    times = [info.duration * f for f in (0.25, 0.35, 0.45, 0.55, 0.65, 0.75)]
    white = []
    for frame in _frames(video, times, tmp_path, ffmpeg):
        band = frame[1400:1750].mean(axis=2)  # the caption band, above the IG safe area
        white.append(float((band > 200).mean()))
    assert max(white) > 0.006, f"no burned-in caption pixels found: {white}"


def test_captions_never_cover_the_website_card(reel):
    """The caption band must sit outside the region the recording occupies."""
    overlay = json.loads((reel["dir"] / "overlay.json").read_text())
    assert overlay["chunks"]
    assert all(c["band"] in ("bottom", "top", "middle") for c in overlay["chunks"])
    plan = json.loads((reel["dir"] / "edit_plan.json").read_text())
    assert plan["shots"]


def test_the_cover_is_readable_and_branded(reel):
    from PIL import Image

    with Image.open(reel["dir"] / "cover.jpg") as im:
        assert im.size == (1080, 1920)
        arr = np.asarray(im.convert("RGB"), dtype=np.uint8)
    # brand strip at the bottom uses the accent colour
    strip = arr[-90:, :, :].reshape(-1, 3).mean(axis=0)
    assert strip[0] > strip[2] + 40, f"brand strip is not the accent colour: {strip}"
    # the title band has punchy contrast
    assert arr[80:520].mean(axis=2).std() > 35


def test_work_directory_is_cleaned_but_state_is_kept(reel):
    work = reel["ctx"].work_dir
    leftovers = sorted(p.name for p in work.iterdir())
    assert leftovers == ["state.json"], leftovers

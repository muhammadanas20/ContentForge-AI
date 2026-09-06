"""Cover generation and the pre-packaging quality gates."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from contentforge.media.captions import CaptionChunk, CaptionWord, OverlayPlan
from contentforge.media.cover import CoverGenerator, CoverStyle, score_frames
from contentforge.models.schemas import GroundedScript, ScriptSegment
from contentforge.pipeline.quality import QualityGate, QualityThresholds
from contentforge.processing.editor import EditConfig, plan_edit

pytestmark = pytest.mark.ffmpeg


@pytest.fixture(scope="module")
def plan(understanding):
    return plan_edit(understanding, config=EditConfig(max_duration=40))


@pytest.fixture(scope="module")
def script():
    return GroundedScript(
        title="Studentofferco.com for students",
        segments=[
            ScriptSegment(0.0, 2.0, "Save this one: studentofferco.com.", "hook", "opening frame"),
            ScriptSegment(2.0, 6.0, "This is studentofferco.com.", "setup", "the page"),
            ScriptSegment(6.0, 8.0, "Follow StudentTools.pk for more.", "cta", "closing frame"),
        ],
        website="studentofferco.com",
    )


# -------------------------------------------------------------------- cover
def test_frame_scoring_prefers_the_result_moment(understanding, tutorial_recording):
    candidates = score_frames(understanding)
    assert candidates
    best = candidates[0]
    assert best.t >= tutorial_recording.reveal - 0.7, f"cover frame at {best.t:.1f}s"
    assert best.score > candidates[-1].score
    # a frame from the dead tail should never win
    assert best.reasons.get("reveal", 0) > 0


def test_cover_is_a_branded_1080x1920_image(tutorial_recording, understanding, plan, script, tmp_path):
    out = tmp_path / "cover.jpg"
    result = CoverGenerator(CoverStyle(brand="StudentTools.pk")).generate(
        tutorial_recording.path,
        out,
        understanding=understanding,
        plan=plan,
        script=script,
        subtitle="studentofferco.com",
        work_dir=tmp_path / "work",
    )
    assert out.exists()
    with Image.open(out) as im:
        assert im.size == (1080, 1920)
    assert result.concept in CoverGenerator.CONCEPTS
    assert len(result.concepts) >= 2, "only one concept was considered"
    assert result.title and len(result.title.split()) <= 7
    # working files are cleaned up
    assert not list((tmp_path / "work").glob("cover_*.png"))


def test_cover_survives_a_recording_without_understanding(tutorial_recording, tmp_path):
    out = tmp_path / "plain.jpg"
    CoverGenerator().generate(tutorial_recording.path, out, work_dir=tmp_path / "w2", title="Quick look")
    assert out.exists()
    with Image.open(out) as im:
        assert im.size == (1080, 1920)


# ------------------------------------------------------------------ quality
class _Narration:
    def __init__(self, path, duration, coverage):
        self.path = path
        self.duration = duration
        self.coverage = coverage


def _overlay(texts: list[str]) -> OverlayPlan:
    plan = OverlayPlan(duration=10.0)
    t = 0.0
    for text in texts:
        words = [CaptionWord(w, t + i * 0.3, t + (i + 1) * 0.3) for i, w in enumerate(text.split())]
        plan.chunks.append(CaptionChunk(t, t + 1.0, words))
        t += 1.2
    return plan


@pytest.fixture(scope="module")
def rendered(tutorial_recording, tmp_path_factory):
    """A real 1080x1920 file with audio to run the gates against."""
    from contentforge.utils.ffmpeg import FFmpeg

    out = tmp_path_factory.mktemp("q") / "final.mp4"
    FFmpeg().run(
        [
            "-i",
            str(tutorial_recording.path),
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=200:sample_rate=48000",
            "-vf",
            "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
            "-t",
            "10",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(out),
        ]
    )
    return out


def test_a_good_reel_passes_every_gate(rendered, plan, understanding, script, tmp_path, ffmpeg):
    narration = _Narration(rendered, 10.0, 0.85)
    cover = tmp_path / "cover.jpg"
    Image.new("RGB", (1080, 1920), "black").save(cover)
    report = QualityGate(QualityThresholds(), ffmpeg).run(
        rendered,
        plan=plan,
        understanding=understanding,
        script=script,
        narration=narration,
        overlay=_overlay(["ONE CLICK", "AND IT OPENS"]),
        cover=cover,
        audio_levels={"peak_db": -1.2, "mean_db": -18.0},
        work_dir=tmp_path,
        source_size=(960, 540),
    )
    assert report.passed, [c.to_dict() for c in report.errors]
    names = {c.name for c in report.checks}
    assert {"aspect_9_16", "ocr_regions_preserved", "narration_covers_timeline", "captions_fit_screen"} <= names


def test_a_squashed_video_fails_the_aspect_gate(tutorial_recording, ffmpeg, tmp_path):
    report = QualityGate(QualityThresholds(), ffmpeg).run(tutorial_recording.path)
    assert not report.passed
    assert any(c.name == "aspect_9_16" for c in report.errors)


def test_missing_narration_fails(rendered, ffmpeg, tmp_path):
    report = QualityGate(QualityThresholds(), ffmpeg).run(
        rendered, narration=None, overlay=_overlay(["HELLO"]), work_dir=tmp_path
    )
    assert any(c.name == "narration_exists" for c in report.errors)


def test_short_narration_on_a_long_video_fails(rendered, ffmpeg, tmp_path):
    narration = _Narration(rendered, 3.0, 0.20)
    report = QualityGate(QualityThresholds(), ffmpeg).run(
        rendered, narration=narration, overlay=_overlay(["HELLO"]), work_dir=tmp_path
    )
    failed = {c.name for c in report.errors}
    assert "narration_covers_timeline" in failed
    assert "narration_duration_matches" in failed


def test_overflowing_captions_fail(rendered, ffmpeg, tmp_path):
    long_word = "SUPERCALIFRAGILISTICEXPIALIDOCIOUSSTUDENTOFFERS"
    report = QualityGate(QualityThresholds(), ffmpeg).run(
        rendered, overlay=_overlay([long_word]), work_dir=tmp_path
    )
    assert any(c.name == "captions_fit_screen" for c in report.errors)


def test_leftover_work_files_are_reported_as_a_warning(rendered, ffmpeg, tmp_path):
    (tmp_path / "shot_000.mp4").write_bytes(b"x")
    report = QualityGate(QualityThresholds(), ffmpeg).run(rendered, work_dir=tmp_path)
    assert any(c.name == "work_dir_clean" for c in report.warnings)
    assert not any(c.name == "work_dir_clean" for c in report.errors)


def test_report_serialises_for_the_manifest(rendered, ffmpeg, tmp_path):
    report = QualityGate(QualityThresholds(), ffmpeg).run(rendered, work_dir=tmp_path)
    data = report.to_dict()
    assert "checks" in data and data["checks"]
    assert set(data["checks"][0]) == {"name", "passed", "detail", "severity", "value"}
    assert "| check |" in report.to_markdown()
    Path(tmp_path / "quality.md").write_text(report.to_markdown())

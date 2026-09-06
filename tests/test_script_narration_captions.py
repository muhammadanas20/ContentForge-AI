"""Grounded script, timeline-aligned narration and Reel captions."""

from __future__ import annotations

import wave
from pathlib import Path

import pytest

from contentforge.ai.narration import NarrationBuilder
from contentforge.ai.script_writer import GroundedScriptPlanner
from contentforge.ai.tts.base import TTSEngine, TTSResult
from contentforge.config.schema import ScriptConfig
from contentforge.media.captions import (
    OverlayStyle,
    ReelOverlayRenderer,
    build_overlay_plan,
    chunk_segment,
    fit_font_size,
)
from contentforge.models.schemas import EditPlan, GroundedScript, Region
from contentforge.processing.editor import EditConfig, plan_edit

pytestmark = pytest.mark.ffmpeg


class WordRateTTS(TTSEngine):
    """A deterministic engine: speech lasts exactly ``wps`` words per second."""

    name = "wordrate"

    def __init__(self, ffmpeg, wps: float = 2.5):
        self.ff = ffmpeg
        self.wps = wps

    @property
    def voice(self) -> str:
        return "test"

    def is_available(self) -> bool:
        return True

    def synthesize(self, text: str, out_path: Path) -> TTSResult:
        duration = max(0.4, len(text.split()) / self.wps)
        self.ff.run(
            [
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=220:sample_rate=24000:duration={duration:.3f}",
                "-c:a",
                "pcm_s16le",
                str(out_path),
            ]
        )
        return TTSResult(Path(out_path), duration, self.name, "test", 24000, [(0.0, duration, text)])


@pytest.fixture(scope="module")
def plan(understanding) -> EditPlan:
    return plan_edit(understanding, config=EditConfig(max_duration=40))


@pytest.fixture(scope="module")
def script(plan, understanding) -> GroundedScript:
    planner = GroundedScriptPlanner(ScriptConfig(), brand="StudentTools.pk")
    return planner.plan(
        plan,
        understanding,
        website="studentofferco.com",
        website_context="StudentOfferCo lists verified student discounts for university students.",
    )


# ------------------------------------------------------------------- script
def test_every_segment_maps_to_a_real_visual_segment(script, plan):
    assert script.segments
    for seg in script.segments:
        assert seg.text.strip()
        assert seg.visual_action, f"{seg.role} segment has no visual"
        assert seg.end > seg.start
        assert 0.0 <= seg.start <= plan.duration + 0.01
        assert seg.end <= plan.duration + 0.01
        shot = plan.shot_at((seg.start + seg.end) / 2)
        assert shot is not None
        assert seg.source_start >= plan.shots[0].src_start - 0.01


def test_segments_follow_the_reel_structure(script):
    roles = [s.role for s in script.segments]
    assert roles[0] == "hook"
    assert roles[-1] == "cta"
    assert "demo" in roles


def test_the_script_never_invents_features(script):
    """Only the website name, visible text and generic actions may appear."""
    banned = {"discount code", "coupon", "guarantee", "cashback", "unlimited", "premium"}
    text = " ".join(s.text.lower() for s in script.segments)
    assert not any(b in text for b in banned)
    assert script.grounded and not script.degraded


def test_lines_fit_the_time_they_have(script):
    for seg in script.segments:
        if seg.role == "cta":
            continue
        words = len(seg.text.split())
        assert words <= seg.duration * 3.4 + 1, f"{seg.text!r} in {seg.duration:.1f}s"


def test_degraded_fallback_is_still_grounded():
    planner = GroundedScriptPlanner(ScriptConfig(), brand="StudentTools.pk")
    script = planner.plan(EditPlan(), None, website="studentofferco.com")
    assert script.degraded and script.grounded
    assert script.segments and script.segments[-1].role == "cta"
    assert "studentofferco.com" in script.segments[0].text.lower()


def test_legacy_script_dict_keeps_the_social_step_working(script):
    legacy = script.to_legacy_dict()
    assert legacy["hook"] and legacy["cta"] and legacy["title"]
    assert isinstance(legacy["body"], list) and legacy["body"]


# ---------------------------------------------------------------- narration
def test_narration_matches_the_edit_timeline(script, ffmpeg, tmp_path):
    builder = NarrationBuilder(WordRateTTS(ffmpeg), ffmpeg, sample_rate=24000)
    out = tmp_path / "narration.wav"
    result = builder.build(script, out, work_dir=tmp_path / "parts")
    with wave.open(str(out), "rb") as w:
        seconds = w.getnframes() / w.getframerate()
    timeline = max(s.end for s in script.segments)
    # the narration spans the Reel: never a 5 s voice-over on a 20 s video
    assert timeline - 1.0 <= seconds <= timeline + 2.0
    assert result.coverage >= 0.55
    assert [s.role for s in result.segments] == [s.role for s in script.segments]
    for a, b in zip(result.segments, result.segments[1:]):
        assert a.end <= b.start + 1e-6, "narration segments overlap"


def test_slow_speech_is_time_fitted_instead_of_drifting(script, ffmpeg, tmp_path):
    slow = NarrationBuilder(WordRateTTS(ffmpeg, wps=1.4), ffmpeg, sample_rate=24000)
    result = slow.build(script, tmp_path / "slow.wav", work_dir=tmp_path / "slowparts")
    assert any(s.tempo > 1.02 for s in result.segments), "nothing was time-fitted"
    assert all(s.tempo <= 1.23 for s in result.segments), "speech was sped up too hard"


def test_narration_failure_is_not_fatal(script, ffmpeg, tmp_path):
    class BrokenTTS(WordRateTTS):
        def synthesize(self, text, out_path):
            raise RuntimeError("no voice available")

    builder = NarrationBuilder(BrokenTTS(ffmpeg), ffmpeg)
    with pytest.raises(RuntimeError):
        builder.build(script, tmp_path / "x.wav")


# ----------------------------------------------------------------- captions
def test_captions_are_short_and_cover_the_line():
    chunks = chunk_segment("Scroll down and everything is listed there for students.", 0.0, 4.0)
    assert chunks
    assert all(len(c.words) <= 4 for c in chunks)
    assert chunks[0].start == pytest.approx(0.0, abs=0.01)
    assert chunks[-1].end == pytest.approx(4.0, abs=0.2)
    joined = " ".join(c.text for c in chunks)
    assert joined.startswith("Scroll down") and joined.endswith("students.")


def test_captions_move_away_from_important_content():
    st = OverlayStyle()
    bottom_focus = [(0.0, 5.0, Region(0.05, 0.72, 0.9, 0.26))]

    class Seg:
        start, end, text, role = 0.0, 3.0, "Look at this result", "demo"

    plan = build_overlay_plan([Seg()], duration=5.0, style=st, focus_regions=bottom_focus)
    assert plan.chunks
    assert any(c.band == "top" for c in plan.chunks)


def test_long_words_are_shrunk_to_stay_on_screen():
    st = OverlayStyle()
    normal = fit_font_size("ONE CLICK", st)
    long_word = fit_font_size("STUDENTOFFERCO.COM/STUDENT-OFFERS", st)
    assert normal == st.font_size
    assert long_word < st.font_size


def test_ass_output_is_well_formed(tmp_path):
    class Seg:
        start, end, text, role = 2.5, 4.5, "One click and it opens", "demo"

    st = OverlayStyle()
    plan = build_overlay_plan(
        [Seg()], duration=6.0, style=st, clicks=[(1.0, 540.0, 900.0)], hook_text="Save this", cta_text="Follow us"
    )
    assert plan.chunks, "captions were suppressed by the hook/CTA cards"
    ass = ReelOverlayRenderer(st).build_ass(plan)
    assert "[Script Info]" in ass and "PlayResX: 1080" in ass
    assert ass.count("Dialogue:") >= len(plan.chunks) + 3  # + cards, watermark, progress
    assert "\\pos(" in ass
    assert "Style: Caption" in ass and "Style: Card" in ass
    # the watermark and progress bar are always there
    assert "StudentTools.pk" in ass

"""Tests for Creative QA scoring system."""

from __future__ import annotations

from contentforge.models.schemas import (
    EditPlan,
    GroundedScript,
    ScriptSegment,
    VideoUnderstanding,
)
from contentforge.pipeline.creative_qa import CreativeQA, EditingScore


def test_editing_score_overall_weighting():
    score = EditingScore(
        hook_strength=0.9,
        first_3s_clarity=0.8,
        visual_change_rate=0.7,
        information_density=0.8,
        pacing=0.75,
        payoff_strength=0.85,
        caption_readability=0.9,
        audio_clarity=0.8,
        cta_quality=0.7,
        visual_coherence=0.8,
    )
    overall = score.overall
    assert 0.75 <= overall <= 0.85

    d = score.to_dict()
    assert "overall" in d
    assert d["overall"] == round(overall, 4)


def test_creative_qa_evaluate_with_minimal_script():
    qa = CreativeQA()
    script = GroundedScript(
        title="PDF Tool",
        segments=[
            ScriptSegment(start=0.0, end=3.0, text="Still wasting time on PDFs? Watch this.", role="hook"),
            ScriptSegment(start=3.0, end=8.0, text="Smallpdf lets you convert them instantly.", role="demo"),
            ScriptSegment(start=8.0, end=10.0, text="Follow StudentTools.pk for more.", role="cta"),
        ],
    )
    score = qa.evaluate(script=script, duration=10.0, caption_count=3, narration_coverage=0.9)
    assert isinstance(score, EditingScore)
    assert score.cta_quality > 0.6
    assert score.audio_clarity > 0.8
    assert score.overall > 0.0

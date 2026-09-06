"""Tests for the Creative Director and CreativePlan."""

from __future__ import annotations

from contentforge.creative.director import CreativeDirector, CreativePlan
from contentforge.models.schemas import ActionEvent, FrameObservation, Region, VideoUnderstanding


def test_creative_plan_defaults_and_serialization():
    plan = CreativePlan(
        concept="Quick PDF converter demo",
        hook_style="problem",
        music_mood="tech",
        layout_preference="canvas",
    )
    d = plan.to_dict()
    assert d["concept"] == "Quick PDF converter demo"
    assert d["hook_style"] == "problem"

    restored = CreativePlan.from_dict(d)
    assert restored.concept == plan.concept
    assert restored.layout_preference == "canvas"

    md = plan.to_markdown()
    assert "Creative Plan" in md
    assert "Quick PDF converter demo" in md


def test_rule_based_creative_plan_generation():
    u = VideoUnderstanding(
        width=1920,
        height=1080,
        duration=30.0,
        website="smallpdf.com",
        actions=[
            ActionEvent(start=2.0, end=3.0, kind="click", label="Choose File"),
            ActionEvent(start=10.0, end=12.0, kind="reveal", label="Download"),
        ],
    )
    director = CreativeDirector(brand="StudentTools.pk")
    plan = director.plan(u, website="smallpdf.com")

    assert isinstance(plan, CreativePlan)
    assert plan.source == "rule-based"
    assert plan.hook_style in ("problem", "curiosity", "question", "shock", "secret", "result_first")
    assert plan.music_mood in ("tech", "upbeat", "calm", "energetic", "educational")
    assert len(plan.hook_candidates) >= 1

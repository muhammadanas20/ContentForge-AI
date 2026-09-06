"""Caption chunking + ASS rendering tests."""

from pathlib import Path

import pytest

from contentforge.ai.transcriber import Transcript
from contentforge.config.schema import BrandingConfig, ProgressBarConfig, SubtitleConfig
from contentforge.subtitles import AssRenderer, OverlaySpec, build_captions, write_all


def test_build_captions_limits(fake_transcript):
    caps = build_captions(
        fake_transcript, max_words=3, max_chars=20, highlight_keywords=["free", "students"]
    )
    assert caps
    for c in caps:
        assert len(c.words) <= 3
        assert len(c.text) <= 20 or len(c.words) == 1
        assert c.end > c.start
    # sequential, non-overlapping
    for a, b in zip(caps, caps[1:]):
        assert a.end <= b.start + 1e-6
    highlighted = [w.text for c in caps for w in c.words if w.highlight]
    assert "FREE" in highlighted and "STUDENTS" in highlighted
    assert all(w.text == w.text.upper() for c in caps for w in c.words)


def test_build_captions_breaks_on_sentence_and_pause(fake_transcript):
    caps = build_captions(fake_transcript, max_words=10, max_chars=200, max_duration=100)
    # sentence end after "instantly." should force a break
    texts = [c.text for c in caps]
    assert any(t.endswith("INSTANTLY.") for t in texts)


def test_captions_from_text_without_word_timings():
    tr = Transcript(language="en", duration=4.0, segments=[])
    tr2 = Transcript.from_text("one two three four five six", 3.0)
    caps = build_captions(tr2, max_words=2)
    assert len(caps) == 3 and abs(caps[-1].end - 3.0) < 1e-6
    assert build_captions(tr) == []


@pytest.mark.parametrize("style", ["bold-pop", "clean", "karaoke", "minimal"])
def test_ass_render_styles(tmp_path, fake_transcript, style):
    cfg = SubtitleConfig(style=style, highlight_keywords=["free"])
    caps = build_captions(fake_transcript, highlight_keywords=cfg.highlight_keywords)
    r = AssRenderer(cfg, 1080, 1920)
    spec = OverlaySpec(
        duration=10,
        progress_bar=ProgressBarConfig(),
        branding=BrandingConfig(),
        intro_text="Free PDF tool for students",
        outro_text="Follow for more",
    )
    out = r.render(caps, tmp_path / "o.ass", spec)
    text = out.read_text()
    assert "PlayResX: 1080" in text and "[Events]" in text
    assert text.count("Dialogue:") >= len(caps) + 5  # captions + bar(2) + watermark + cards
    assert "StudentTools.pk" in text
    assert "\\t(0,10000,\\fscx100)" in text  # progress bar animates over full duration
    if style == "karaoke":
        assert "\\kf" in text
    if style == "bold-pop":
        assert "\\fscx82" in text


def test_ass_disabled_subtitles_still_draws_overlays(tmp_path, fake_transcript):
    cfg = SubtitleConfig(enabled=False)
    caps = build_captions(fake_transcript)
    out = AssRenderer(cfg).render(
        caps, tmp_path / "o.ass", OverlaySpec(duration=5, progress_bar=ProgressBarConfig())
    )
    text = out.read_text()
    assert "Style: Caption" in text
    assert text.count("Dialogue:") == 2


def test_writers(tmp_path, fake_transcript):
    caps = build_captions(fake_transcript)
    out = write_all(caps, tmp_path / "subs.x", ["srt", "json", "txt", "bogus"])
    assert set(out) == {"srt", "json", "txt"}
    srt = out["srt"].read_text()
    assert srt.startswith("1\n00:00:00,000 -->")
    assert Path(out["json"]).read_text().startswith("[")

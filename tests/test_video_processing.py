"""FFmpeg render tests for the video editor and audio mixer."""

from pathlib import Path

import pytest

from contentforge.ai.transcriber import Transcript
from contentforge.config.schema import (
    AudioConfig,
    BrandingConfig,
    ProgressBarConfig,
    SubtitleConfig,
    VideoConfig,
)
from contentforge.processing import (
    AudioMixer,
    Timeline,
    VideoEditor,
    ZoomPulse,
    analyse_video,
    build_keep_ranges,
    plan_crop,
    plan_zoom_pulses,
    zoom_expression,
)
from contentforge.subtitles import AssRenderer, OverlaySpec, build_captions

pytestmark = pytest.mark.ffmpeg


def small_video_cfg() -> VideoConfig:
    return VideoConfig(width=540, height=960, fps=24, crf=30, preset="ultrafast")


def test_plan_crop_modes():
    from contentforge.utils.ffmpeg import MediaInfo

    info = MediaInfo(path=Path("x"), width=1920, height=1080)
    c = plan_crop(info, 1080, 1920, "center")
    assert c.h == 1080 and c.w == 608 and c.x == (1920 - 608) // 2
    assert plan_crop(info, 1080, 1920, "left").x == 0
    assert plan_crop(info, 1080, 1920, "right").x == 1920 - 608
    smart = plan_crop(info, 1080, 1920, "smart", center_x=0.9)
    assert smart.x == 1920 - 608  # clamped
    tall = MediaInfo(path=Path("x"), width=1080, height=2400)
    c2 = plan_crop(tall, 1080, 1920, "center")
    assert c2.w == 1080 and c2.h == 1920


def test_zoom_planning_and_expression():
    pulses = plan_zoom_pulses(30, interval=6, duration=2.5, max_zoom=1.1)
    assert pulses and all(p.start >= 1 for p in pulses)
    assert all(b.start >= a.start + a.duration + 1 for a, b in zip(pulses, pulses[1:]))
    expr = zoom_expression(pulses)
    assert expr.startswith("1+") and "between(time" in expr and "cos(" in expr
    assert zoom_expression([]) == "1"
    assert plan_zoom_pulses(2, interval=6, duration=2.5, max_zoom=1.1) == []
    anchored = plan_zoom_pulses(30, interval=6, duration=2, max_zoom=1.1, anchors=[0.5, 5, 6, 12])
    assert [p.start for p in anchored] == [5, 12]


def test_frame_analysis(sample_video):
    fa = analyse_video(sample_video, sample_fps=2)
    assert fa.width == 1280 and fa.height == 720
    assert len(fa.times) >= 10
    assert 0 <= fa.dominant_center() <= 1
    # testsrc2 is constantly moving, so no low motion spans at a tiny threshold
    assert fa.low_motion_intervals(threshold=0.01, min_duration=1.0) == []


def test_full_render_pipeline(tmp_path, ffmpeg, sample_video):
    vcfg = small_video_cfg()
    editor = VideoEditor(vcfg, ffmpeg)
    info = ffmpeg.probe(sample_video)

    wav = ffmpeg.extract_audio(sample_video, tmp_path / "a.wav")
    silences = ffmpeg.detect_silence(wav, threshold_db=-30, min_duration=0.5)
    keep = build_keep_ranges(info.duration, silences, padding=0.1, min_silence=0.5)
    assert len(keep) == 2
    tl = Timeline(keep)
    assert tl.output_duration < info.duration - 1.0

    crop = plan_crop(info, vcfg.width, vcfg.height, "center")
    pulses = [ZoomPulse(start=1.0, duration=1.5, max_zoom=1.1)]
    cut = editor.render_cut(
        sample_video, tmp_path / "cut.mp4", keep=keep, crop=crop, zoom_pulses=pulses
    )
    cinfo = ffmpeg.probe(cut)
    assert cinfo.width == 540 and cinfo.height == 960
    assert abs(cinfo.duration - tl.output_duration) < 0.3
    assert not cinfo.has_audio

    # audio: cut original and mix (as "narration") with loudnorm
    mixer = AudioMixer(AudioConfig(), ffmpeg)
    cut_audio = mixer.cut_audio(sample_video, tmp_path / "cut.wav", keep)
    assert abs(ffmpeg.probe(cut_audio).duration - tl.output_duration) < 0.3
    mixed = mixer.mix(tmp_path / "mix.wav", duration=tl.output_duration, narration=cut_audio)
    assert abs(ffmpeg.probe(mixed).duration - tl.output_duration) < 0.2

    # overlays
    tr = Transcript.from_text(
        "free pdf tool for students converts files instantly", tl.output_duration
    )
    caps = build_captions(tr, highlight_keywords=["free"])
    ass = AssRenderer(SubtitleConfig(), vcfg.width, vcfg.height).render(
        caps,
        tmp_path / "o.ass",
        OverlaySpec(
            duration=tl.output_duration,
            progress_bar=ProgressBarConfig(),
            branding=BrandingConfig(),
            intro_text="Free PDF tool",
            outro_text="Follow for more",
        ),
    )
    final = editor.render_final(
        cut, mixed, tmp_path / "final.mp4", ass_path=ass, duration=tl.output_duration
    )
    finfo = ffmpeg.probe(final)
    assert finfo.has_audio and finfo.has_video
    assert finfo.width == 540 and finfo.height == 960
    assert abs(finfo.duration - tl.output_duration) < 0.3

    # progress bar should be partially filled mid-way: check orange pixels at top row
    from PIL import Image

    frame = ffmpeg.extract_frame(final, tmp_path / "mid.png", at=tl.output_duration / 2)
    im = Image.open(frame).convert("RGB")
    row = [im.getpixel((x, 6)) for x in range(0, im.width, 4)]
    orange = [p for p in row if p[0] > 180 and 80 < p[1] < 170 and p[2] < 90]
    assert 0.3 * len(row) < len(orange) < 0.7 * len(row)


def test_retime_and_freeze(tmp_path, ffmpeg, sample_video):
    editor = VideoEditor(small_video_cfg(), ffmpeg)
    slow = editor.retime_video(sample_video, tmp_path / "slow.mp4", 7.0)
    assert abs(ffmpeg.probe(slow).duration - 7.0) < 0.3
    ext = editor.freeze_extend(sample_video, tmp_path / "ext.mp4", 8.0)
    assert abs(ffmpeg.probe(ext).duration - 8.0) < 0.3


def test_mix_without_sources_produces_silence(tmp_path, ffmpeg):
    mixer = AudioMixer(AudioConfig(), ffmpeg)
    out = mixer.mix(tmp_path / "s.wav", duration=1.5)
    assert abs(ffmpeg.probe(out).duration - 1.5) < 0.1

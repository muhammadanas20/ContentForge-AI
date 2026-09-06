"""Tests for utils: fs, retry, timefmt, ffmpeg wrapper."""

from pathlib import Path

import pytest

from contentforge.utils import (
    RetryError,
    atomic_write_json,
    format_ass_time,
    format_srt_time,
    hex_to_ass_color,
    hex_to_rgb,
    human_size,
    read_json,
    retry,
    safe_rmtree,
    seconds_to_clock,
    slugify,
    wait_until_stable,
)


def test_slugify():
    assert slugify("Hello, World! PDF -> Word") == "hello-world-pdf-word"
    assert slugify("   ") == "untitled"
    assert len(slugify("a" * 200, max_length=20)) == 20


def test_atomic_json_roundtrip(tmp_path):
    p = tmp_path / "x" / "y.json"
    atomic_write_json(p, {"a": [1, 2], "p": Path("/tmp")})
    assert read_json(p) == {"a": [1, 2], "p": "/tmp"}
    assert not p.with_name("y.json.tmp").exists()
    assert read_json(tmp_path / "missing.json", default=7) == 7


def test_wait_until_stable(tmp_path):
    f = tmp_path / "f.bin"
    f.write_bytes(b"abc")
    assert wait_until_stable(f, stable_seconds=0.2, poll=0.05, timeout=3)
    assert not wait_until_stable(tmp_path / "missing", stable_seconds=0.1, poll=0.05, timeout=0.3)


def test_safe_rmtree_guard(tmp_path):
    inside = tmp_path / "a" / "b"
    inside.mkdir(parents=True)
    (inside / "f").write_text("x")
    assert safe_rmtree(inside, tmp_path)
    assert not inside.exists()
    assert not safe_rmtree(tmp_path, tmp_path)  # refuse to delete guard itself
    assert not safe_rmtree("/tmp", tmp_path)  # refuse paths outside guard


def test_retry_success_after_failures():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ValueError("boom")
        return "ok"

    assert retry(flaky, attempts=3, backoff=0, sleep=lambda _: None) == "ok"
    assert calls["n"] == 3


def test_retry_exhausted():
    def always():
        raise KeyError("nope")

    with pytest.raises(RetryError) as ei:
        retry(always, attempts=2, backoff=0, sleep=lambda _: None)
    assert isinstance(ei.value.last_exception, KeyError)
    assert ei.value.attempts == 2


def test_time_formats():
    assert format_srt_time(0) == "00:00:00,000"
    assert format_srt_time(3661.5) == "01:01:01,500"
    assert format_srt_time(1.9999) == "00:00:02,000"
    assert format_ass_time(61.25) == "0:01:01.25"
    assert format_ass_time(0.999) == "0:00:01.00"
    assert seconds_to_clock(75) == "01:15"
    assert seconds_to_clock(3700) == "1:01:40"


def test_colours():
    assert hex_to_rgb("#FF7A00") == (255, 122, 0)
    assert hex_to_rgb("fff") == (255, 255, 255)
    assert hex_to_ass_color("#FF7A00") == "&H00007AFF"
    assert hex_to_ass_color("#00000080") == "&H7F000000"
    with pytest.raises(ValueError):
        hex_to_rgb("#12345")


def test_human_size():
    assert human_size(512) == "512.0 B"
    assert human_size(1536) == "1.5 KB"
    assert human_size(3 * 1024**3) == "3.0 GB"


@pytest.mark.ffmpeg
def test_ffmpeg_probe_and_ops(ffmpeg, sample_video, tmp_path):
    info = ffmpeg.probe(sample_video)
    assert info.has_video and info.has_audio
    assert info.width == 1280 and info.height == 720
    assert 5.5 <= info.duration <= 6.5
    assert not info.is_vertical

    wav = ffmpeg.extract_audio(sample_video, tmp_path / "a.wav")
    assert wav.exists() and wav.stat().st_size > 1000

    silences = ffmpeg.detect_silence(wav, threshold_db=-30, min_duration=0.5)
    assert len(silences) >= 1
    s, e = silences[0]
    assert 1.8 <= s <= 2.3 and 3.8 <= e <= 4.3

    frame = ffmpeg.extract_frame(sample_video, tmp_path / "f.png", at=1.0)
    assert frame.exists()

    sil = ffmpeg.make_silence(tmp_path / "s.wav", 0.5)
    assert 0.4 <= ffmpeg.probe(sil).duration <= 0.6

    assert ffmpeg.version()


@pytest.mark.ffmpeg
def test_ffmpeg_error_raised(ffmpeg, tmp_path):
    from contentforge.utils import FFmpegError

    with pytest.raises(FFmpegError):
        ffmpeg.run(["-i", str(tmp_path / "nonexistent.mp4"), str(tmp_path / "o.mp4")])

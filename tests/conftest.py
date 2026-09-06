"""Shared pytest fixtures."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from contentforge.config import load_settings, reset_settings
from contentforge.log import setup_logging
from contentforge.utils.ffmpeg import FFmpeg, ffmpeg_available

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def settings(tmp_path: Path):
    """Settings anchored at the repo config but with all data paths in tmp."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("CONTENTFORGE")}
    env["CONTENTFORGE_DATA_DIR"] = str(tmp_path / "data")
    s = load_settings(REPO_ROOT / "config" / "config.yaml", root=REPO_ROOT, environ=env)
    s.ensure_directories()
    setup_logging(s.paths.logs, "DEBUG", console=False, force=True)
    reset_settings(s)
    yield s
    reset_settings(None)


@pytest.fixture()
def fast_settings(settings):
    """Settings tuned for fast test renders (small frame, ultrafast, no retries)."""
    settings.video.width, settings.video.height, settings.video.fps = 540, 960, 24
    settings.video.preset, settings.video.crf = "ultrafast", 30
    settings.pipeline.mode = "classic"  # v0.2 path; the smart pipeline has its own tests
    settings.pipeline.retries = 0
    settings.pipeline.retry_backoff_seconds = 0
    settings.audio.silence.threshold_db = -30
    settings.audio.silence.min_duration = 0.5
    return settings


@pytest.fixture()
def ffmpeg():
    if not ffmpeg_available():
        pytest.skip("ffmpeg not available")
    return FFmpeg()


@pytest.fixture()
def sample_video(tmp_path: Path, ffmpeg: FFmpeg) -> Path:
    """A 6-second 1280x720 synthetic 'screen recording' with speech-like audio + silence gap."""
    out = tmp_path / "sample_recording.mp4"
    # Audio: tone 0-2s, silence 2-4s, tone 4-6s -> gives silencedetect something to find
    ffmpeg.run(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=1280x720:rate=30:duration=6",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100:duration=6",
            "-af",
            "volume=enable='between(t,2,4)':volume=0",
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


@pytest.fixture()
def fake_transcript():
    from contentforge.ai.transcriber import Transcript, TranscriptSegment, TranscriptWord

    words_text = (
        "This free website helps students convert PDF files instantly. "
        "You upload the file, choose a format, and download the result in seconds."
    ).split()
    words = []
    t = 0.0
    for w in words_text:
        words.append(TranscriptWord(word=w, start=t, end=t + 0.3))
        t += 0.35
    seg1 = TranscriptSegment(
        id=0, start=0.0, end=words[8].end, text=" ".join(words_text[:9]), words=words[:9]
    )
    seg2 = TranscriptSegment(
        id=1,
        start=words[9].start,
        end=words[-1].end,
        text=" ".join(words_text[9:]),
        words=words[9:],
    )
    return Transcript(language="en", duration=t, segments=[seg1, seg2])


@pytest.fixture(scope="session")
def tutorial_recording(tmp_path_factory):
    """A short synthetic 'student offers' screen recording with known ground truth.

    Rendered once per test session (~3 s) and shared by every v0.3 test so the
    understanding/framing/editing assertions all run against the same material.
    """
    if not ffmpeg_available():
        pytest.skip("ffmpeg not available")
    import sys

    sys.path.insert(0, str(Path(__file__).parent))
    from screen_recording import make_tutorial_recording

    out = tmp_path_factory.mktemp("tutorial") / "tutorial.mp4"
    return make_tutorial_recording(out, width=960, height=540, fps=20, duration=12.0)


@pytest.fixture(scope="session")
def understanding(tutorial_recording):
    """``understand_video`` on the shared recording (analysed once per session)."""
    from contentforge.processing.video_understanding import understand_video

    return understand_video(
        tutorial_recording.path,
        sample_fps=6,
        ocr_every_seconds=1.0,
        website="studentofferco.com",
    )

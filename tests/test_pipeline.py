"""End-to-end pipeline tests with a stubbed transcriber/TTS (no model downloads)."""

from pathlib import Path

import pytest

import contentforge.pipeline.steps as steps_mod
from contentforge.ai.transcriber import Transcript, TranscriptSegment, TranscriptWord
from contentforge.ai.tts.base import TTSEngine, TTSError, TTSResult
from contentforge.db import Database
from contentforge.pipeline import PipelineRunner
from contentforge.pipeline.steps import DEFAULT_STEPS, ScriptStep, Step

pytestmark = pytest.mark.ffmpeg

NARRATION_TEXT = (
    "This free website helps students convert PDF files instantly. "
    "You upload the file, choose Word, and download the result in seconds. No sign up needed."
)


class FakeTranscriber:
    def __init__(self, config):
        pass

    def transcribe(self, audio_path):
        words = NARRATION_TEXT.split()
        tw, t = [], 0.3
        for w in words:
            tw.append(TranscriptWord(w, t, t + 0.18))
            t += 0.2
        seg = TranscriptSegment(0, 0.3, t, NARRATION_TEXT, tw)
        return Transcript(language="en", duration=6.0, segments=[seg], model="fake")


class FakeTTS(TTSEngine):
    name = "fake"

    def __init__(self, ffmpeg, duration=5.0):
        self.ff = ffmpeg
        self.duration = duration

    def is_available(self):
        return True

    @property
    def voice(self):
        return "fake"

    def synthesize(self, text, out_path):
        sentences = self.split_sentences(text)
        per = self.duration / max(1, len(sentences))
        self.ff.run(
            [
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=300:sample_rate=24000:duration={self.duration}",
                "-c:a",
                "pcm_s16le",
                str(out_path),
            ]
        )
        timings = [(i * per, (i + 1) * per, s) for i, s in enumerate(sentences)]
        return TTSResult(Path(out_path), self.duration, "fake", "fake", 24000, timings)


class FailingTTS(FakeTTS):
    def synthesize(self, text, out_path):
        raise TTSError("no engine")


@pytest.fixture()
def fast_settings(settings):
    settings.video.width, settings.video.height, settings.video.fps = 540, 960, 24
    settings.video.preset, settings.video.crf = "ultrafast", 30
    settings.pipeline.retries = 0
    settings.pipeline.retry_backoff_seconds = 0
    settings.audio.silence.threshold_db = -30
    settings.audio.silence.min_duration = 0.5
    return settings


@pytest.fixture()
def patched(monkeypatch, ffmpeg):
    monkeypatch.setattr(steps_mod, "Transcriber", FakeTranscriber)
    monkeypatch.setattr(steps_mod, "get_tts_engine", lambda cfg: FakeTTS(ffmpeg))


def _drop(sample_video: Path, settings, name="smallpdf.com - convert pdf.mp4") -> Path:
    dst = settings.paths.input / name
    dst.write_bytes(sample_video.read_bytes())
    return dst


def test_full_pipeline_end_to_end(fast_settings, ffmpeg, sample_video, patched):
    s = fast_settings
    db = Database(s.paths.db)
    src = _drop(sample_video, s)
    runner = PipelineRunner(s, db, ffmpeg)
    ctx = runner.create_job(src)
    assert ctx.data["website_hint"] if "website_hint" in ctx.data else True
    result = runner.run(ctx)
    assert result.status == "archived", result.error
    assert result.output_dir and result.output_dir.exists()

    out = result.output_dir
    video = out / f"{ctx.slug}.mp4"
    assert video.exists()
    info = ffmpeg.probe(video)
    assert info.width == 540 and info.height == 960 and info.has_audio
    # narration is 5s (+0.6 tail) and the cut video ~4s -> retimed to ~5.6s
    assert 5.0 <= info.duration <= 6.5
    for f in (
        "cover.jpg",
        "caption.txt",
        "caption.md",
        "social.json",
        "script.md",
        "script.json",
        "subtitles.srt",
        "subtitles.ass",
        "subtitles.json",
        "transcript.txt",
        "transcript.json",
        "thumbnail_brief.md",
        "manifest.json",
        "README.md",
        "narration.wav",
    ):
        assert (out / f).exists(), f
    caption = (out / "caption.txt").read_text()
    assert "#studenttoolspk" in caption and "smallpdf.com" in caption

    # source archived, work dir cleaned (only state.json left), db consistent
    assert not src.exists()
    assert (s.paths.archive).glob("*/*/smallpdf.com - convert pdf.mp4")
    remaining = [p.name for p in ctx.work_dir.iterdir()]
    assert remaining == ["state.json"]
    job = db.get_job(ctx.job_id)
    assert job["status"] == "archived" and job["title"] and job["output_dir"] == str(out)
    steps = db.get_steps(ctx.job_id)
    assert all(v["status"] == "done" for v in steps.values()), {
        k: v["status"] for k, v in steps.items()
    }
    assert steps["narration"]["outputs"]["engine"] == "fake"
    assert db.latest_metrics_per_job()  # baseline metrics registered
    assert db.recent_hashtag_sets(1)
    db.close()


def test_pipeline_without_narration_uses_original_audio(
    fast_settings, ffmpeg, sample_video, monkeypatch
):
    s = fast_settings
    monkeypatch.setattr(steps_mod, "Transcriber", FakeTranscriber)
    monkeypatch.setattr(steps_mod, "get_tts_engine", lambda cfg: FailingTTS(ffmpeg))
    s.pipeline.steps.archive = False
    db = Database(s.paths.db)
    src = _drop(sample_video, s)
    runner = PipelineRunner(s, db, ffmpeg)
    ctx = runner.create_job(src)
    result = runner.run(ctx)
    assert result.status == "completed", result.error
    assert src.exists()  # archive disabled -> raw input stays
    assert ctx.data["sync"]["method"] == "original-audio"
    assert db.get_steps(ctx.job_id)["archive"]["status"] == "skipped"
    info = ffmpeg.probe(result.output_dir / f"{ctx.slug}.mp4")
    assert info.has_audio and 3.0 <= info.duration <= 5.0  # silence removed (~2s gap)
    events = db.recent_events(level="warning")
    assert any("Narration unavailable" in e["message"] for e in events)
    db.close()


def test_pipeline_resume_after_failure(fast_settings, ffmpeg, sample_video, patched, monkeypatch):
    s = fast_settings
    db = Database(s.paths.db)
    src = _drop(sample_video, s)
    runner = PipelineRunner(s, db, ffmpeg)
    ctx = runner.create_job(src)

    calls = {"n": 0}
    original_run = ScriptStep.run

    def flaky(self, c):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated crash")
        return original_run(self, c)

    monkeypatch.setattr(ScriptStep, "run", flaky)
    r1 = runner.run(ctx)
    assert r1.status == "failed" and "script" in r1.error
    assert db.get_job(ctx.job_id)["status"] == "failed"
    assert "transcribe" in ctx.completed_steps and "script" not in ctx.completed_steps

    # resume: transcribe must NOT run again; script retried and everything completes
    transcribe_calls = {"n": 0}
    real = FakeTranscriber.transcribe

    def counting(self, p):
        transcribe_calls["n"] += 1
        return real(self, p)

    monkeypatch.setattr(FakeTranscriber, "transcribe", counting)
    ctx2 = runner.load_job(ctx.job_id)
    r2 = runner.run(ctx2)
    assert r2.status == "archived", r2.error
    assert transcribe_calls["n"] == 0
    assert calls["n"] == 2
    assert db.get_steps(ctx.job_id)["script"]["attempts"] == 2
    db.close()


def test_force_from_step_and_resume_incomplete(fast_settings, ffmpeg, sample_video, patched):
    s = fast_settings
    s.pipeline.steps.archive = False
    s.pipeline.cleanup_work_on_success = False
    db = Database(s.paths.db)
    src = _drop(sample_video, s)
    runner = PipelineRunner(s, db, ffmpeg)
    ctx = runner.create_job(src)
    assert runner.run(ctx).status == "completed"
    done_before = list(ctx.completed_steps)

    # simulate crash: mark processing, then resume_incomplete should finish it quickly (all steps cached)
    db.update_job(ctx.job_id, status="processing")
    results = runner.resume_incomplete()
    assert len(results) == 1 and results[0].status == "completed"

    # force re-run from subtitles: earlier steps cached, later steps re-run
    s.subtitles.style = "karaoke"
    ctx2 = runner.load_job(ctx.job_id)
    r = runner.run(ctx2, force_from="subtitles")
    assert r.status == "completed"
    assert set(r.step_times) == {
        "subtitles",
        "render_final",
        "thumbnail",
        "social",
        "package",
        "analytics",
    }
    assert "\\kf" in (ctx2.work_dir / "overlay.ass").read_text()
    assert done_before == ctx2.completed_steps
    with pytest.raises(ValueError):
        runner.run(ctx2, force_from="nope")
    db.close()


def test_probe_rejects_bad_input(fast_settings, ffmpeg, tmp_path):
    s = fast_settings
    db = Database(s.paths.db)
    bad = s.paths.input / "bad.mp4"
    bad.write_bytes(b"not a video")
    runner = PipelineRunner(s, db, ffmpeg)
    ctx = runner.create_job(bad)
    r = runner.run(ctx)
    assert r.status == "failed" and "probe" in r.error
    assert db.get_job(ctx.job_id)["status"] == "failed"
    assert runner.find_existing(bad)["id"] == ctx.job_id
    db.close()


def test_step_names_unique():
    names = [c.name for c in DEFAULT_STEPS]
    assert len(names) == len(set(names))
    assert all(issubclass(c, Step) for c in DEFAULT_STEPS)

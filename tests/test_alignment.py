"""Word-level narration alignment: algorithm, fallbacks and pipeline integration."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import contentforge.pipeline.steps as steps_mod
from contentforge.ai.alignment import Alignment, align_script, normalise_tokens
from contentforge.ai.transcriber import Transcript, TranscriptSegment, TranscriptWord
from contentforge.db import Database
from contentforge.pipeline import PipelineRunner
from contentforge.subtitles import build_captions
from tests.test_pipeline import FakeTranscriber, FakeTTS

SCRIPT = (
    "Stop wasting time. This free website StudentTools.pk converts PDF files to Word "
    "in 5 seconds. No sign up needed!"
)


def _asr(words: list[str], t0: float = 0.5, engine: str = "fw") -> Transcript:
    tw, t = [], t0
    for w in words:
        d = 0.12 + 0.05 * len(w)
        tw.append(TranscriptWord(w, t, t + d))
        t += d + 0.05
    return Transcript(
        "en", t + 0.3, [TranscriptSegment(0, t0, t, " ".join(words), tw)], engine=engine
    )


def test_normalise_tokens_expands_tts_rewrites():
    assert normalise_tokens("StudentTools.pk") == [["student", "tools", "dot", "p", "k"]]
    assert normalise_tokens("smallpdf.com, 5 A&B 100%") == [
        ["smallpdf", "dot", "com"],
        ["five"],
        ["a", "and", "b"],
        ["100", "percent"],
    ]
    assert normalise_tokens("Hello!!") == [["hello"]]


def test_align_script_handles_asr_errors_and_keeps_script_text():
    # ASR: dropped "free", "converts"->"convert", URL spelled out, "5"->"five"
    asr = _asr(
        "stop wasting time this website student tools dot p k convert pdf files to word "
        "in five seconds no sign up needed".split()
    )
    al = align_script(SCRIPT, asr, duration=asr.duration, min_word_seconds=0.15)
    assert al is not None
    assert [w.word for w in al.words] == SCRIPT.split()  # on-screen text = script, not ASR
    assert al.match_ratio > 0.85 and al.asr_words == 22
    unmatched = [w.word for w in al.words if not w.matched]
    assert unmatched == ["free", "converts"]
    # monotonic, non-overlapping, every word visible >= min_word_seconds
    for a, b in zip(al.words, al.words[1:]):
        assert b.start >= a.end - 1e-9
    assert all(w.end - w.start >= 0.15 - 1e-9 for w in al.words)
    # matched words carry the ASR timing (StudentTools.pk spans 5 ASR tokens)
    st = next(w for w in al.words if w.word == "StudentTools.pk")
    assert st.end - st.start > 1.0
    # round trip + transcript with sentence segments
    back = Alignment.from_dict(al.to_dict())
    assert len(back.words) == len(al.words) and back.match_ratio == pytest.approx(
        al.match_ratio, abs=1e-3
    )
    sentences = [
        (0, 1.7, "Stop wasting time."),
        (1.7, 7.4, SCRIPT.split(". ")[1] + "."),
        (7.4, 9.0, "No sign up needed!"),
    ]
    tr = back.to_transcript(sentences, 9.0)
    assert [len(s.words) for s in tr.segments] == [3, 12, 4]
    assert tr.segments[0].start == pytest.approx(0.5, abs=0.01)
    caps = build_captions(tr, max_words=4, max_chars=22, highlight_keywords=["free"])
    assert caps and caps[0].text.startswith("STOP")


def test_align_script_rejects_garbage_and_edge_cases():
    junk = _asr("completely different words that share nothing with the script at all".split())
    assert align_script(SCRIPT, junk, duration=junk.duration) is None
    assert align_script("", junk) is None
    empty = Transcript("en", 3.0, [], engine="fw")
    assert align_script(SCRIPT, empty) is None
    # leading + trailing unmatched words get plausible extrapolated timings inside [0, duration]
    asr = _asr("this free website student tools dot p k converts pdf files to word".split(), t0=2.0)
    assert align_script(SCRIPT, asr, duration=9.0) is None  # 9/19 matched < 0.6 default
    al = align_script(SCRIPT, asr, duration=9.0, min_match_ratio=0.4)
    assert al is not None and al.words[0].start >= 0.0 and al.words[-1].end <= 9.02
    assert al.words[0].word == "Stop" and not al.words[0].matched
    assert al.words[-1].word == "needed!" and not al.words[-1].matched


# --------------------------------------------------------------------------- pipeline


class AligningTranscriber(FakeTranscriber):
    """Returns the NARRATION_TEXT transcript for the source, and for narration.wav a
    realistic ASR of the *actual script* with a few typical recognition errors."""

    calls: list[str] = []
    seen_scripts: list[str] = []

    def transcribe(self, audio_path):
        AligningTranscriber.calls.append(Path(audio_path).name)
        if Path(audio_path).name != "narration.wav":
            return super().transcribe(audio_path)
        script_json = Path(audio_path).with_name("script.json")
        import json

        text = " ".join(
            p
            for p in [json.loads(script_json.read_text())["hook"]]
            + json.loads(script_json.read_text())["body"]
            + [json.loads(script_json.read_text())["cta"]]
        )
        AligningTranscriber.seen_scripts.append(text)
        toks = [t for sub in normalise_tokens(text) for t in sub]
        # drop every 9th word, like a real ASR would miss a few
        toks = [t for i, t in enumerate(toks) if i % 9 != 4]
        words, t = [], 0.15
        for w in toks:
            d = 0.10 + 0.045 * len(w)
            words.append(TranscriptWord(w, t, t + d))
            t += d + 0.04
        return Transcript(
            "en", t, [TranscriptSegment(0, 0.15, t, " ".join(toks), words)], model="fake"
        )


@pytest.mark.ffmpeg
def test_pipeline_word_level_captions_and_fallback(
    fast_settings, ffmpeg, sample_video, monkeypatch
):
    AligningTranscriber.calls = []
    monkeypatch.setattr(steps_mod, "Transcriber", AligningTranscriber)
    monkeypatch.setattr(steps_mod, "get_tts_engine", lambda cfg: FakeTTS(ffmpeg, duration=8.0))
    s = fast_settings
    db = Database(s.paths.db)
    src = s.paths.input / "smallpdf.com - convert pdf.mp4"
    src.write_bytes(sample_video.read_bytes())
    runner = PipelineRunner(s, db, ffmpeg)
    ctx = runner.create_job(src)
    r = runner.run(ctx)
    assert r.status == "archived", r.error
    assert AligningTranscriber.calls.count("narration.wav") == 1
    narr = ctx.data["narration"]
    assert narr["alignment"] and narr["alignment"]["match_ratio"] >= 0.6
    steps = db.get_steps(ctx.job_id)
    assert steps["narration"]["outputs"]["word_alignment"].endswith("%")
    # captions in the package carry the aligned (non-uniform) word timings
    import json

    caps = json.loads((r.output_dir / "subtitles.json").read_text())
    words = [w for c in caps for w in c["words"]]
    durations = {round(w["end"] - w["start"], 2) for w in words}
    assert len(durations) > 3  # not evenly spread anymore
    script_words = [
        re.sub(r"[^\w]", "", w).upper() for w in AligningTranscriber.seen_scripts[-1].split()
    ]
    cap_words = [re.sub(r"[^\w]", "", w["text"]).upper() for w in words]
    assert cap_words == [w for w in script_words if w]  # exactly the script, in order
    # timings are inside the final video and monotonic
    final = float(ctx.data["final_duration"])
    assert all(0 <= w["start"] <= w["end"] <= final + 0.05 for w in words)
    assert all(b["start"] >= a["start"] for a, b in zip(words, words[1:]))
    # ASS carries per-word timing in karaoke style too
    ass = (r.output_dir / "subtitles.ass").read_text()
    assert "Dialogue:" in ass
    db.close()

    # ---- fallback: alignment disabled -> sentence-level evenly spread timings, still works
    s.subtitles.word_level.enabled = False
    AligningTranscriber.calls = []
    src = s.paths.input / "fallback - demo.mp4"
    src.write_bytes(sample_video.read_bytes())
    db = Database(s.paths.db)
    runner = PipelineRunner(s, db, ffmpeg)
    ctx2 = runner.create_job(src)
    r2 = runner.run(ctx2)
    assert r2.status == "archived", r2.error
    assert "narration.wav" not in AligningTranscriber.calls
    assert ctx2.data["narration"]["alignment"] is None
    assert db.get_steps(ctx2.job_id)["narration"]["outputs"]["word_alignment"] == "disabled"
    db.close()

    # ---- fallback: ASR crashes on the narration -> warning event, sentence timings, job OK
    s.subtitles.word_level.enabled = True

    class Crashing(AligningTranscriber):
        def transcribe(self, audio_path):
            if Path(audio_path).name == "narration.wav":
                raise RuntimeError("model not downloaded")
            return super().transcribe(audio_path)

    monkeypatch.setattr(steps_mod, "Transcriber", Crashing)
    src = s.paths.input / "crash - demo.mp4"
    src.write_bytes(sample_video.read_bytes())
    db = Database(s.paths.db)
    runner = PipelineRunner(s, db, ffmpeg)
    ctx3 = runner.create_job(src)
    r3 = runner.run(ctx3)
    assert r3.status == "archived", r3.error
    assert ctx3.data["narration"]["alignment"] is None
    assert (
        db.get_steps(ctx3.job_id)["narration"]["outputs"]["word_alignment"] == "fallback-sentence"
    )
    db.close()

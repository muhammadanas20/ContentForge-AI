"""Tests for script writing, LLM helpers, social/hashtag generation and transcript model."""

import random

import requests

from contentforge.ai.llm import LLMClient, LLMConfig, parse_json_object
from contentforge.ai.script_writer import (
    Script,
    ScriptWriter,
    clean_transcript,
    extract_keywords,
    split_sentences,
)
from contentforge.ai.social import HashtagGenerator, SocialWriter
from contentforge.ai.transcriber import Transcript
from contentforge.config.schema import ScriptConfig, SocialConfig

TRANSCRIPT = (
    "Um so, this is a free website called smallpdf and, uh, it lets students convert PDF files "
    "to Word documents. You just upload the file, like, pick the format you want, and it converts "
    "it in a few seconds. You know, there's no sign up needed and it also compresses large PDF files "
    "so you can email them to your professor. Basically, it saves a lot of time before assignments."
)


def test_clean_and_split():
    cleaned = clean_transcript(TRANSCRIPT)
    assert "Um" not in cleaned and "uh" not in cleaned.split()
    sents = split_sentences(cleaned)
    assert len(sents) >= 4
    kws = extract_keywords(cleaned)
    assert "pdf" in kws and "files" in kws or "convert" in kws


def test_rule_based_script_is_grounded_and_structured():
    cfg = ScriptConfig(target_words_max=70)
    w = ScriptWriter(cfg, llm=LLMClient(LLMConfig(provider="none")), rng=random.Random(1))
    s = w.write(TRANSCRIPT, website_hint="smallpdf.com")
    assert s.hook and s.body and s.cta
    assert s.word_count <= 70 + 5
    assert s.source == "rule-based"
    assert s.estimated_seconds > 0
    assert "smallpdf.com" in s.hook or s.hook  # hook mentions site for some styles
    d = s.to_dict()
    assert Script.from_dict(d).narration == s.narration
    assert "# " in s.to_markdown()


def test_llm_script_used_when_grounded(monkeypatch):
    cfg = ScriptConfig(target_words_max=120)

    class FakeLLM(LLMClient):
        def __init__(self):
            super().__init__(LLMConfig(provider="ollama", model="x"))

        @property
        def enabled(self):
            return True

        def complete(self, system, user, json_mode=False):
            return (
                '{"title": "Convert PDF to Word Free", "hook": "Stop paying for PDF tools.", '
                '"body": ["Open smallpdf and upload your PDF file.", "Pick Word as the format.", '
                '"It converts in seconds with no sign up."], "cta": "Follow for more.", "keywords": ["pdf"]}'
            )

    s = ScriptWriter(cfg, llm=FakeLLM()).write(TRANSCRIPT)
    assert s.source == "llm:ollama" and s.title == "Convert PDF to Word Free"


def test_llm_script_rejected_when_hallucinating():
    class FakeLLM(LLMClient):
        def __init__(self):
            super().__init__(LLMConfig(provider="ollama", model="x"))

        @property
        def enabled(self):
            return True

        def complete(self, system, user, json_mode=False):
            return (
                '{"title": "X", "hook": "H", "body": ["Blockchain quantum telemetry synchronises '
                'holographic spreadsheets across satellites automatically overnight."], "cta": "C"}'
            )

    s = ScriptWriter(ScriptConfig(), llm=FakeLLM()).write(TRANSCRIPT)
    assert s.source == "rule-based"


def test_llm_client_disabled_and_failures():
    c = LLMClient(LLMConfig(provider="none"))
    assert not c.enabled and c.complete("a", "b") is None
    c2 = LLMClient(LLMConfig(provider="openai", api_key=""))
    assert not c2.enabled
    # network failure path must return None, not raise
    c3 = LLMClient(
        LLMConfig(provider="ollama", base_url="http://127.0.0.1:9", model="x", timeout=0.5)
    )
    assert c3.enabled and c3.complete("a", "b") is None


def test_llm_openai_request_shape(monkeypatch):
    captured = {}

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": '{"ok": true}'}}]}

    class Sess(requests.Session):
        def post(self, url, **kw):
            captured["url"] = url
            captured["kw"] = kw
            return Resp()

    c = LLMClient(
        LLMConfig(provider="openai", api_key="k", model="m", base_url="https://x/v1"),
        session=Sess(),
    )
    assert c.complete_json("s", "u") == {"ok": True}
    assert captured["url"] == "https://x/v1/chat/completions"
    assert captured["kw"]["headers"]["Authorization"] == "Bearer k"
    assert captured["kw"]["json"]["response_format"] == {"type": "json_object"}


def test_parse_json_object():
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('Sure! {"a": [1,2]} done') == {"a": [1, 2]}
    assert parse_json_object("nope") is None
    assert parse_json_object("[1,2]") is None


def test_hashtags_balanced_and_rotating():
    cfg = SocialConfig()
    gen = HashtagGenerator(cfg, random.Random(3))
    first = gen.generate(["pdf", "convert"])
    assert len(first) == cfg.hashtags.count == len(set(first))
    assert first[0] == "#studenttoolspk" and "#pdf" in first
    second = gen.generate(["pdf"], recent_sets=[first])
    non_topic_first = set(first[5:])
    non_topic_second = set(second[5:])
    # most generic tags should differ from the previous set
    assert len(non_topic_first & non_topic_second) < len(non_topic_first) * 0.6


def test_hashtags_banned_and_count():
    cfg = SocialConfig()
    cfg.hashtags.count = 8
    cfg.hashtags.banned = ["#viral", "fyp"]
    tags = HashtagGenerator(cfg, random.Random(0)).generate([])
    assert len(tags) == 8 and "#viral" not in tags and "#fyp" not in tags


def test_social_writer_package():
    cfg = SocialConfig(cta_variants=["Follow us."], comment_prompts=["Which tool next?"])
    script = Script(
        hook="Still paying for PDF tools?",
        body=["Upload the file.", "Pick Word.", "Download."],
        cta="Follow.",
        title="Convert PDF to Word Free",
        keywords=["pdf", "word"],
        website="smallpdf.com",
    )
    pkg = SocialWriter(cfg, llm=LLMClient(LLMConfig(provider="none")), rng=random.Random(1)).write(
        script
    )
    assert pkg.cta == "Follow us." and pkg.comment_prompt == "Which tool next?"
    assert "smallpdf.com" in pkg.caption and "1. Upload the file" in pkg.caption
    assert pkg.full_caption.endswith(" ".join(pkg.hashtags))
    assert len(pkg.full_caption) <= cfg.caption_max_length
    assert "#shorts" in pkg.youtube_title
    assert "Instagram caption" in pkg.to_markdown()
    assert pkg.to_dict()["full_caption"] == pkg.full_caption


def test_transcript_model_roundtrip(fake_transcript, tmp_path):
    files = fake_transcript.save(tmp_path / "t")
    assert files["srt"].read_text().startswith("1\n00:00:00,000")
    loaded = Transcript.load(files["json"])
    assert loaded.text == fake_transcript.text
    assert loaded.word_count == fake_transcript.word_count
    spans = loaded.speech_intervals(gap=0.1)
    assert spans and spans[0][0] == 0.0
    assert len(loaded.speech_intervals(gap=1.0)) == 1

"""Turn a raw transcript into a short-form, student-friendly narration script.

Design goals:

* **Grounded** - the script may only describe what the transcript actually
  says. The rule-based writer literally reuses transcript sentences; the LLM
  prompt forbids inventing features and the result is validated against the
  transcript vocabulary when ``strict_grounding`` is on.
* **Structured** - every script has a *hook*, a fast-paced *body*, and a *CTA*.
* **Timed** - word count is bounded so the narration fits the target duration.
* **Offline-first** - works with no LLM; the LLM merely improves phrasing.
"""

from __future__ import annotations

import random
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from contentforge.ai.llm import LLMClient
from contentforge.config.schema import ScriptConfig
from contentforge.log import get_logger

log = get_logger("script")

STOPWORDS = frozenset(
    """a an the and or but so if then than that this these those there here is are was were be
    been being am do does did doing have has had having i you he she it we they me him her us them
    my your his its our their of to in on at by for with from as into onto up down out over under
    again further once about above below between through during before after off just very can
    will would should could may might must shall not no nor only own same too s t don now also
    what which who whom whose when where why how all any both each few more most other some such
    ok okay yeah um uh like get got go going gonna wanna let lets really actually basically thing
    things stuff one two three first second next""".split()
)

FILLERS = re.compile(
    r"\b(um+|uh+|erm+|ah+|hmm+|you know|i mean|sort of|kind of|like,|basically,|actually,|so,? yeah|okay so|alright so)\b[,]?\s*",
    re.I,
)


@dataclass
class Script:
    hook: str
    body: list[str]
    cta: str
    title: str
    keywords: list[str] = field(default_factory=list)
    source: str = "rule-based"
    hook_style: str = "problem"
    website: str = ""
    estimated_seconds: float = 0.0

    @property
    def narration(self) -> str:
        """Full narration text, in speaking order."""
        return " ".join(_clean_spaces(p) for p in [self.hook, *self.body, self.cta] if p.strip())

    @property
    def word_count(self) -> int:
        return len(self.narration.split())

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["narration"] = self.narration
        d["word_count"] = self.word_count
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Script:
        fields = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**fields)

    def to_markdown(self) -> str:
        lines = [f"# {self.title}", "", f"**Hook ({self.hook_style}):** {self.hook}", ""]
        lines += [f"- {b}" for b in self.body]
        lines += [
            "",
            f"**CTA:** {self.cta}",
            "",
            f"_Keywords: {', '.join(self.keywords)}_",
            f"_~{self.estimated_seconds:.0f}s, {self.word_count} words, source: {self.source}_",
        ]
        return "\n".join(lines) + "\n"


class ScriptWriter:
    """Generates :class:`Script` objects from transcripts."""

    SYSTEM_PROMPT = (
        "You write narration scripts for 20-60 second Instagram Reels made by {brand}, a page that "
        "shows Pakistani university and college students useful free websites and tools. "
        "Rules: (1) ONLY describe features that are explicitly present in the transcript - never "
        "invent features, prices, numbers or claims. (2) Simple, energetic, student-friendly English. "
        "(3) Short sentences. Fast pacing. (4) Start with a strong hook (question, surprising fact, or "
        "a relatable student problem). (5) Create curiosity, then show the steps. (6) End with a clear "
        "call to action. (7) Total length {min_words}-{max_words} words. "
        'Respond ONLY with JSON: {{"title": str, "hook": str, "body": [str, ...], "cta": str, '
        '"keywords": [str, ...]}}'
    )

    def __init__(
        self,
        config: ScriptConfig,
        brand: str = "StudentTools.pk",
        llm: LLMClient | None = None,
        rng: random.Random | None = None,
    ):
        self.config = config
        self.brand = brand
        self.llm = llm or LLMClient()
        self.rng = rng or random.Random()

    # ------------------------------------------------------------- public
    def write(self, transcript_text: str, *, website_hint: str = "") -> Script:
        cleaned = clean_transcript(transcript_text)
        keywords = extract_keywords(cleaned, top_n=8)
        script: Script | None = None
        if self.llm.enabled:
            script = self._write_with_llm(cleaned, keywords, website_hint)
            if script and self.config.strict_grounding and not self._is_grounded(script, cleaned):
                log.warning("LLM script failed grounding check - using rule-based script instead")
                script = None
        if script is None:
            script = self._write_rule_based(cleaned, keywords, website_hint)
        script = self._fit_length(script)
        script.estimated_seconds = round(
            script.word_count / max(0.5, self.config.speaking_rate_wps), 1
        )
        script.website = website_hint or self.brand
        log.info(
            "Script ready: %d words (~%.0fs), hook=%s, source=%s",
            script.word_count,
            script.estimated_seconds,
            script.hook_style,
            script.source,
        )
        return script

    # -------------------------------------------------------- rule-based
    def _write_rule_based(self, text: str, keywords: list[str], website_hint: str) -> Script:
        sentences = split_sentences(text)
        if not sentences:
            sentences = ["Here is a quick look at a useful tool for students."]
        topic = _guess_topic(sentences, keywords)
        style = self.rng.choice(self.config.hook_styles or ["problem"])
        hook = _build_hook(style, topic, website_hint or None)
        body = [_punctuate(s) for s in sentences]
        cta = self.rng.choice([self.config.cta_default]) if self.config.cta_default else ""
        title = _title_case(topic)
        return Script(
            hook=hook,
            body=body,
            cta=cta,
            title=title,
            keywords=keywords,
            source="rule-based",
            hook_style=style,
        )

    # --------------------------------------------------------------- llm
    def _write_with_llm(self, text: str, keywords: list[str], website_hint: str) -> Script | None:
        system = self.SYSTEM_PROMPT.format(
            brand=self.brand,
            min_words=self.config.target_words_min,
            max_words=self.config.target_words_max,
        )
        user = f"Website: {website_hint or 'unknown'}\nTranscript:\n{text}"
        data = self.llm.complete_json(system, user)
        if not data:
            return None
        try:
            body = data.get("body") or []
            if isinstance(body, str):
                body = split_sentences(body)
            script = Script(
                hook=str(data.get("hook", "")).strip(),
                body=[str(b).strip() for b in body if str(b).strip()],
                cta=str(data.get("cta", self.config.cta_default)).strip()
                or self.config.cta_default,
                title=str(data.get("title", "")).strip()
                or _title_case(_guess_topic(split_sentences(text), keywords)),
                keywords=[str(k).lower() for k in (data.get("keywords") or keywords)][:10],
                source=f"llm:{self.llm.config.provider}",
                hook_style="llm",
            )
        except (AttributeError, TypeError, ValueError) as exc:
            log.warning("Malformed LLM script: %s", exc)
            return None
        if not script.hook or not script.body:
            return None
        return script

    def _is_grounded(self, script: Script, transcript: str) -> bool:
        """Reject scripts that introduce many content words absent from the transcript."""
        transcript_vocab = set(_content_words(transcript)) | set(_content_words(self.brand))
        body_words = _content_words(" ".join(script.body))
        if not body_words:
            return False
        novel = [w for w in body_words if w not in transcript_vocab and not _is_generic(w)]
        ratio = len(novel) / len(body_words)
        log.debug("Grounding check: %.0f%% novel content words (%s)", ratio * 100, novel[:8])
        return ratio <= 0.35

    # ------------------------------------------------------------ length
    def _fit_length(self, script: Script) -> Script:
        """Trim body sentences until the narration fits ``target_words_max``."""
        max_words = self.config.target_words_max
        while script.word_count > max_words and len(script.body) > 1:
            # Drop the least keyword-dense sentence from the *middle/end*, keep the first step.
            scores = [(_keyword_density(s, script.keywords), i) for i, s in enumerate(script.body)]
            scores = [sc for sc in scores if sc[1] != 0] or scores
            _, idx = min(scores)
            script.body.pop(idx)
        if script.word_count > max_words and script.body:
            words = script.body[-1].split()
            keep = max(4, len(words) - (script.word_count - max_words))
            script.body[-1] = " ".join(words[:keep]).rstrip(",;") + "."
        return script


# ---------------------------------------------------------------- helpers
def clean_transcript(text: str) -> str:
    """Remove filler words, repeated words and odd spacing."""
    t = FILLERS.sub("", text)
    t = re.sub(r"\b(\w+)( \1\b)+", r"\1", t, flags=re.I)  # "the the" -> "the"
    t = re.sub(r"\s+([,.!?])", r"\1", t)
    return _clean_spaces(t)


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    out = []
    for p in parts:
        p = p.strip()
        if len(p.split()) >= 3:
            out.append(p)
    # Very long run-on sentences (common in ASR output) -> split on commas/and.
    result: list[str] = []
    for s in out:
        if len(s.split()) > 28:
            chunks = re.split(r",\s+|\s+and then\s+|\s+so\s+", s)
            buf = ""
            for c in chunks:
                buf = (buf + " " + c).strip()
                if len(buf.split()) >= 8:
                    result.append(_punctuate(buf))
                    buf = ""
            if buf:
                result.append(_punctuate(buf))
        else:
            result.append(s)
    return result


_WEAK_SUFFIXES = ("ly", "ing", "ed")
_WEAK_VERBS = frozenset(
    """upload uploads download downloads choose pick click open paste create creates make makes
    convert converts generate generates need needs want wants use uses helps help lets let
    works work shows show see tell""".split()
)


def extract_keywords(text: str, top_n: int = 8) -> list[str]:
    """Keyword extraction favouring topical nouns.

    Score = frequency, boosted for capitalised/acronym tokens (PDF, APA) and
    penalised for adverbs (``-ly``), participles and generic action verbs so that
    "pdf", "citations", "flashcards" outrank "instantly" and "upload".
    """
    counts: dict[str, float] = {}
    caps = {m.lower() for m in re.findall(r"\b[A-Z][A-Z0-9]{1,}\b", text)}
    for w in _content_words(text):
        score = 1.0
        if w in caps:
            score += 1.5
        if w.endswith("ly") or w in _WEAK_VERBS or w.endswith("ing"):
            score -= 0.6
        elif w.endswith("ed"):
            score -= 0.3
        if _is_generic(w):
            score -= 0.4
        counts[w] = counts.get(w, 0.0) + score
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], -len(kv[0]), kv[0]))
    return [w for w, sc in ranked[:top_n] if sc > 0] or [w for w, _ in ranked[:top_n]]


def _content_words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-zA-Z][a-zA-Z0-9'-]{2,}", text.lower()) if w not in STOPWORDS]


_GENERIC = frozenset(
    """students student free tool tools website site app online step steps click open upload download
    file files use using need want today watch video follow share save comment link check try simple
    easy quick fast instantly seconds minutes hack trick tip tips best top way ways help helps helpful
    show shows let's learn learning study studying assignment assignments university college class
    exam exams work works results result page pages start starting done ready sign signup needed
    required account login free website tool seconds minute minutes hours""".split()
)


def _is_generic(word: str) -> bool:
    return word in _GENERIC or word.rstrip("s") in _GENERIC


def _guess_topic(sentences: list[str], keywords: list[str]) -> str:
    """Pick a natural 2-4 word phrase from the transcript that is dense in keywords.

    Instead of gluing keywords together ("instantly download students") we scan
    the actual sentences for the window of consecutive words with the highest
    keyword score, so the hook reads like a phrase a human would say
    ("convert PDF files").
    """
    if not sentences:
        return "student tool"
    kw_rank = {k: len(keywords) - i for i, k in enumerate(keywords[:8])}
    best_score, best_phrase = 0.0, ""
    for sent in sentences:
        tokens = [re.sub(r"[^\w'-]", "", t) for t in sent.split()]
        tokens = [t for t in tokens if t]
        for size in (3, 2, 4):
            for i in range(0, max(0, len(tokens) - size + 1)):
                window = tokens[i : i + size]
                lowered = [w.lower() for w in window]
                if lowered[0] in STOPWORDS or lowered[-1] in STOPWORDS:
                    continue
                if any(_is_generic(w) and w not in kw_rank for w in lowered) and size > 2:
                    continue
                score = sum(kw_rank.get(w, 0) for w in lowered) / (size**0.5)
                if any(w in _WEAK_VERBS or w.endswith("ly") for w in lowered):
                    score *= 0.5
                if score > best_score:
                    best_score, best_phrase = score, " ".join(window)
    if best_phrase:
        return best_phrase.strip(".,;:!?")
    return keywords[0] if keywords else " ".join(sentences[0].split()[:4])


def _build_hook(style: str, topic: str, website: str | None) -> str:
    site = website or "this website"
    topic = topic.strip() or "this tool"
    hooks = {
        "question": f"Still wasting hours on {topic}? Watch this.",
        "shock": f"Most students have never heard of {site}. It handles {topic} for free.",
        "problem": f"If {topic} is slowing down your assignments, {site} fixes it in seconds.",
        "secret": f"Here's a {topic} trick almost no student knows about.",
        "curiosity": f"Wait until you see what {site} does with {topic}.",
    }
    return hooks.get(style, hooks["problem"])


@dataclass
class HookCandidate:
    style: str
    text: str
    score: float = 0.0
    scores: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "style": self.style,
            "text": self.text,
            "score": round(self.score, 3),
            "scores": {k: round(v, 3) for k, v in self.scores.items()},
        }


def score_hook(text: str, topic: str = "", website: str | None = None) -> dict[str, float]:
    """Score a hook on retention potential (0.0-1.0)."""
    words = text.split()
    length = len(words)
    # 1. Length/conciseness (ideal 6-12 words)
    length_score = 1.0 if 6 <= length <= 12 else (0.75 if length <= 16 else 0.4)
    # 2. Curiosity / high-retention triggers
    curiosity_triggers = {
        "never", "wasting", "trick", "secret", "wait", "fixes",
        "almost no", "free", "hack", "hours", "save this", "must know",
    }
    has_trigger = any(t in text.lower() for t in curiosity_triggers)
    curiosity_score = 0.95 if has_trigger else 0.55
    # 3. Specificity (mentions topic or site)
    spec_score = 0.9 if ((topic and topic.lower() in text.lower()) or (website and website.lower() in text.lower())) else 0.6
    # 4. First 3 words punchiness
    first_3 = " ".join(words[:3]).lower()
    punchy_starts = ("still wasting", "most students", "if you", "here's a", "wait until", "stop doing", "never do", "save this")
    punch_score = 0.95 if any(first_3.startswith(p) for p in punchy_starts) else 0.65

    overall = length_score * 0.25 + curiosity_score * 0.35 + spec_score * 0.2 + punch_score * 0.2
    return {
        "overall": round(overall, 3),
        "conciseness": round(length_score, 3),
        "curiosity": round(curiosity_score, 3),
        "specificity": round(spec_score, 3),
        "punchiness": round(punch_score, 3),
    }


def generate_hook_candidates(
    topic: str,
    website: str | None,
    budget: int = 10,
) -> list[HookCandidate]:
    """Generate multiple hook variations across styles and score each on retention dimensions."""
    styles = ["problem", "shock", "curiosity", "question", "secret"]
    candidates = []
    for s in styles:
        text = _build_hook(s, topic, website)
        score_dict = score_hook(text, topic, website)
        candidates.append(
            HookCandidate(
                style=s,
                text=text,
                score=score_dict.get("overall", 0.5),
                scores=score_dict,
            )
        )
    candidates.sort(key=lambda c: -c.score)
    return candidates


def _keyword_density(sentence: str, keywords: list[str]) -> float:
    words = sentence.lower().split()
    if not words:
        return 0.0
    return sum(1 for w in words if any(k in w for k in keywords)) / len(words)


def _punctuate(s: str) -> str:
    s = s.strip()
    if not s:
        return s
    s = s[0].upper() + s[1:]
    return s if s[-1] in ".!?" else s + "."


def _title_case(s: str) -> str:
    small = {"a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "with"}
    words = s.split()
    return " ".join(w.capitalize() if i == 0 or w not in small else w for i, w in enumerate(words))


def _clean_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


# =========================================================================== v0.3
# Grounded script planner: every narration beat is bound to a real visual
# segment of the edit, and may only mention things the recording (or the
# supplied website context) actually shows.
# ---------------------------------------------------------------------------
from contentforge.models.schemas import (  # noqa: E402
    ActionEvent,
    EditPlan,
    GroundedScript,
    Region,
    ScriptSegment,
    VideoUnderstanding,
)

_ROLE_ORDER = ("hook", "setup", "demo", "payoff", "cta")


@dataclass
class Beat:
    """A stretch of the edit that one narration line will cover."""

    role: str
    start: float
    end: float
    src_start: float
    src_end: float
    actions: list[ActionEvent] = field(default_factory=list)
    focus: Region | None = None
    texts: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


class GroundedScriptPlanner:
    """Writes a :class:`GroundedScript` from the edit plan + video understanding.

    Grounding rules (enforced, not just prompted):

    * only OCR strings that were actually seen, action labels, the website name
      and the operator-supplied website context may become facts;
    * when nothing was recognised (heuristic OCR backend, no text), the planner
      falls back to describing *what happens* ("a click opens the offer") which
      is still true, instead of inventing features;
    * an optional LLM pass may rephrase, but a segment that introduces unknown
      content words is discarded and the rule-based line is kept.
    """

    SYSTEM_PROMPT = (
        "You rewrite narration lines for a {duration:.0f} second Instagram Reel by {brand} that shows "
        "students a website. You are given, for each line, the exact visual action on screen and the "
        "text that is visible. Rules: (1) NEVER repeat phrases or sentences across lines—every single line "
        "must be fresh, distinct, and engaging. (2) Keep each line under {max_words} words so it fits the slot. "
        "(3) Write natural, punchy, energetic creator-style narration that hooks students and explains the value. "
        "(4) Keep the same number of lines and the same order. "
        'Respond ONLY with JSON: {{"lines": [str, ...]}}'
    )

    def __init__(
        self,
        config: ScriptConfig,
        brand: str = "StudentTools.pk",
        llm: LLMClient | None = None,
        rng: random.Random | None = None,
    ):
        self.config = config
        self.brand = brand
        self.llm = llm or LLMClient()
        self.rng = rng or random.Random(20250903)

    # ------------------------------------------------------------------ api
    def plan(
        self,
        edit: EditPlan,
        understanding: VideoUnderstanding | None = None,
        *,
        website: str = "",
        website_context: str = "",
        transcript_text: str = "",
        creative_plan: Any = None,
    ) -> GroundedScript:
        site = (website or "").strip()
        duration = edit.duration or (understanding.duration if understanding else 0.0)
        if duration <= 0:
            return self._minimal(site, 0.0, transcript_text, reason="empty edit plan")
        if understanding is None or not understanding.available or not edit.shots:
            return self._minimal(site, duration, transcript_text, reason="no video understanding")

        facts = self._collect_facts(understanding, website_context, transcript_text)
        beats = self._beats(edit, understanding)
        segments: list[ScriptSegment] = []
        used_lines: set[str] = set()
        for beat in beats:
            text, visual, evidence = self._line_for(
                beat, understanding, site, facts, creative_plan=creative_plan, used=used_lines
            )
            if not text:
                continue
            segments.append(
                ScriptSegment(
                    start=round(beat.start, 3),
                    end=round(beat.end, 3),
                    text=text,
                    role=beat.role,
                    visual_action=visual,
                    focus_region=beat.focus,
                    evidence=evidence,
                    source_start=round(beat.src_start, 3),
                    source_end=round(beat.src_end, 3),
                )
            )
        script = GroundedScript(
            title=self._title(site, facts),
            segments=segments,
            keywords=self._keywords(facts),
            website=site or self.brand,
            source="grounded-rule-based",
            grounded=True,
        )
        script.notes.append(
            f"OCR backend: {understanding.ocr_backend}"
            + (" (text recognised)" if understanding.has_ocr_text else " (regions only)")
        )
        if website_context:
            script.notes.append("website context supplied by the operator was used for grounding")
        if self.llm.enabled:
            polished = self._polish(script, facts, duration, creative_plan=creative_plan)
            if polished is not None:
                script = polished
        _budget_segments(script, self.config.speaking_rate_wps)
        log.info(
            "Grounded script: %d segments, %d words over %.1fs (%s)",
            len(script.segments),
            script.word_count,
            script.duration,
            script.source,
        )
        return script

    # --------------------------------------------------------------- beats
    def _beats(self, edit: EditPlan, u: VideoUnderstanding) -> list[Beat]:
        """Group shots into narration beats, one per visual idea."""
        beats: list[Beat] = []
        for shot in edit.shots:
            actions = [
                a
                for a in u.actions_between(shot.src_start, shot.src_end)
                if a.kind not in ("idle", "move")
            ]
            focus = next((a.region for a in actions if a.region is not None), None)
            beat = Beat(
                role=shot.role,
                start=shot.out_start,
                end=shot.out_end,
                src_start=shot.src_start,
                src_end=shot.src_end,
                actions=actions,
                focus=focus or u.content_region,
            )
            if beats and _mergeable(beats[-1], beat):
                prev = beats[-1]
                prev.end = beat.end
                prev.src_end = beat.src_end
                prev.actions.extend(beat.actions)
                prev.focus = prev.focus or beat.focus
            else:
                beats.append(beat)
        # a beat shorter than ~2.5 s with matching role folds forward
        merged: list[Beat] = []
        for beat in beats:
            if (
                merged
                and beat.duration < 2.5
                and merged[-1].role == beat.role
                and (merged[-1].duration + beat.duration <= 8.5)
            ):
                merged[-1].end = beat.end
                merged[-1].src_end = beat.src_end
                merged[-1].actions.extend(beat.actions)
            else:
                merged.append(beat)
        return merged

    # --------------------------------------------------------------- facts
    def _collect_facts(
        self, u: VideoUnderstanding, website_context: str, transcript_text: str
    ) -> dict[str, Any]:
        texts = [t for t in u.dominant_texts(24) if len(t) >= 3]
        headline = ""
        for f in u.frames[: max(1, len(u.frames) // 3)]:
            for b in f.text_boxes:
                if b.has_text and len(b.text.split()) >= 3 and b.region.h > 0.02:
                    headline = b.text.strip()
                    break
            if headline:
                break
        labels = [a.label.strip() for a in u.actions if a.label.strip()]
        return {
            "texts": texts,
            "headline": headline,
            "labels": labels,
            "context": clean_transcript(website_context or ""),
            "transcript": clean_transcript(transcript_text or ""),
            "has_text": u.has_ocr_text,
            "kinds": sorted({a.kind for a in u.actions}),
        }

    def _vocabulary(self, facts: dict[str, Any], site: str) -> set[str]:
        blob = " ".join(
            [
                " ".join(facts.get("texts") or []),
                facts.get("headline", ""),
                " ".join(facts.get("labels") or []),
                facts.get("context", ""),
                facts.get("transcript", ""),
                site,
                self.brand,
            ]
        )
        return set(_content_words(blob))

    # ---------------------------------------------------------------- lines
    def _line_for(
        self,
        beat: Beat,
        u: VideoUnderstanding,
        site: str,
        facts: dict[str, Any],
        creative_plan: Any = None,
        used: set[str] | None = None,
    ) -> tuple[str, str, list[str]]:
        # words that can realistically be spoken while this shot is on screen
        budget = max(4, int(beat.duration * max(1.0, self.config.speaking_rate_wps) * 1.15))
        kind = _dominant_kind(beat.actions)
        label = next(
            (a.label.strip() for a in beat.actions if a.kind in ("click", "type", "reveal") and a.label.strip()),
            "",
        )
        near_text = ""
        if facts.get("has_text"):
            boxes = u.text_boxes_at((beat.src_start + beat.src_end) / 2, window=1.5)
            boxes = [b for b in boxes if b.has_text and len(b.text.split()) >= 2]
            boxes.sort(key=lambda b: -(b.region.area * max(0.2, b.confidence)))
            near_text = boxes[0].text.strip() if boxes else ""
        site_name = site or self.brand
        evidence: list[str] = [t for t in (label, near_text) if t]
        target = _shorten(label or near_text, 5)

        if beat.role == "hook":
            # Prefer top hook candidate from creative director if available
            hooks = list(getattr(creative_plan, "hook_candidates", []) or [])
            if hooks:
                return _fit_line(hooks[0], budget), "opening frame of the recording", evidence
            return self._hook(site_name, facts, budget), "opening frame of the recording", evidence

        if beat.role == "setup":
            variants = []
            if facts.get("headline"):
                variants.append(f"This is {site_name}. {_shorten(facts['headline'], 9)}.")
                evidence.append(facts["headline"])
            if facts.get("context"):
                variants.append(f"This is {site_name}. {_first_sentence(facts['context'], 12)}")
                evidence.append(facts["context"][:80])
            variants += [
                f"This is {site_name} - here is what it does.",
                f"Check out {site_name} on screen.",
                f"This is {site_name}.",
            ]
            return _pick(variants, budget, used=used), "the page we start from", evidence

        if beat.role == "cta":
            cta_candidate = getattr(creative_plan, "cta_text", "") if creative_plan else ""
            cta = cta_candidate or self.config.cta_default or f"Follow {self.brand} for more."
            return cta, "closing frame", []

        if beat.role == "payoff":
            variants = []
            if target:
                variants.append(f"And there it is - {target}.")
                variants.append(f"Here is the final output for {target}.")
            variants += [
                "And just like that, the complete result appears on screen.",
                "Here is your finished result, ready to use.",
                "Everything is generated instantly right in front of you.",
                "Take a look at how clean and fast the result turns out.",
                "There is the exact outcome you were looking for.",
            ]
            return _pick(variants, budget, used=used), "the result appears on screen", evidence

        # ---- demonstration beats: describe the action that is actually visible
        if kind == "click":
            variants = ([f"Click {target} and it opens right away.", f"Select {target} to get started."] if target else []) + [
                "One click and it opens right away.",
                "Click to jump straight to the tool.",
                "Select the option you want to use.",
            ]
            visual = f"click on {target}" if target else "click on the page"
        elif kind == "type":
            variants = ([f"Type your details into {target}."] if target else []) + [
                "Fill in the short form here.",
                "Type it in here to proceed.",
                "Enter your input in the box.",
            ]
            visual = f"typing into {target}" if target else "typing into the form"
        elif kind == "scroll":
            variants = [
                "Scroll down to explore all the available tools.",
                "Browse through the list to find what you need.",
                "Everything you need is organized right on the page.",
                "Notice the variety of options laid out here.",
                "Keep scrolling to see the full selection.",
            ]
            visual = "scrolling the page"
        elif kind in ("reveal", "navigate"):
            variants = (
                ["The next screen loads instantly.", "The tool opens up right away.", "Here is the next step."]
                if kind == "navigate"
                else ["Watch what shows up next.", "Here comes the main feature.", "Watch this closely."]
            )
            visual = "the page updates"
        elif near_text:
            variants = [f"Look at {_shorten(near_text, 8)}.", f"Here is {_shorten(near_text, 4)}."]
            visual = "content visible on screen"
        else:
            variants = [
                "Keep watching - this is the most useful part.",
                "Notice how smoothly the interface handles this.",
                "Keep watching as it moves forward.",
            ]
            visual = "demonstration continues"
        return _pick(variants, budget, used=used), visual, evidence

    def _hook(self, site: str, facts: dict[str, Any], budget: int = 8) -> str:
        headline = facts.get("headline") or ""
        topic = headline or _guess_topic(facts.get("sentences") or [], facts.get("keywords") or [])
        candidates = generate_hook_candidates(topic, site, budget=budget)
        if candidates:
            for c in candidates:
                if len(c.text.split()) <= budget:
                    return c.text
            return candidates[0].text
        options: list[str] = []
        if headline:
            options.append(f"{_shorten(headline, 7)}? {site} does it for free.")
        pool = [
            f"Most students have never opened {site}.",
            f"Here is what {site} really does.",
            f"Save this one: {site}.",
        ]
        idx = abs(hash(site)) % len(pool) if site else 0
        options += [pool[idx]] + pool + ["You need to see this."]
        return _pick(options, budget)

    # -------------------------------------------------------------- titles
    def _title(self, site: str, facts: dict[str, Any]) -> str:
        if facts.get("headline"):
            return _title_case(_shorten(facts["headline"], 8))
        if site:
            return _title_case(f"{site} for students")
        return _title_case(f"{self.brand} tool demo")

    def _keywords(self, facts: dict[str, Any]) -> list[str]:
        blob = " ".join((facts.get("texts") or [])[:12] + (facts.get("labels") or []))
        blob = f"{blob} {facts.get('context', '')}"
        return extract_keywords(blob, top_n=8) if blob.strip() else []

    # ----------------------------------------------------------------- llm
    def _polish(
        self,
        script: GroundedScript,
        facts: dict[str, Any],
        duration: float,
        creative_plan: Any = None,
    ) -> GroundedScript | None:
        lines = [s.text for s in script.segments]
        fact_lines = []
        for s in script.segments:
            fact_lines.append(
                f"- [{s.role} {s.start:.1f}-{s.end:.1f}s] visual: {s.visual_action or 'n/a'}; "
                f"visible text: {'; '.join(s.evidence) or 'none recognised'}; draft line: {s.text}"
            )
        concept = getattr(creative_plan, "concept", "") if creative_plan else ""
        hook_style = getattr(creative_plan, "hook_style", "") if creative_plan else ""
        system = self.SYSTEM_PROMPT.format(
            brand=self.brand, duration=duration, max_words=max(8, int(self.config.speaking_rate_wps * 4))
        )
        if concept:
            system += f"\nCreative Concept: {concept} (Hook Style: {hook_style})"
        user = (
            f"Website: {script.website}\n"
            f"Facts that may be mentioned: {', '.join((facts.get('texts') or [])[:20]) or 'none'}\n"
            + (f"Operator-supplied context: {facts['context']}\n" if facts.get("context") else "")
            + "Draft lines to polish into an engaging, cohesive narration without repetition:\n"
            + "\n".join(fact_lines)
        )
        data = self.llm.complete_json(system, user)
        if not data or not isinstance(data.get("lines"), list):
            return None
        new_lines = [str(x).strip() for x in data["lines"]]
        if not new_lines:
            return None
        if len(new_lines) != len(lines):
            log.info("LLM returned %d lines for %d segments - fitting lines to segments", len(new_lines), len(lines))
        has_grounding_source = bool(facts.get("has_text") or facts.get("transcript") or facts.get("context"))
        vocab = self._vocabulary(facts, script.website)
        kept = 0
        for seg, new in zip(script.segments, new_lines):
            if not new:
                continue
            if self.config.strict_grounding and has_grounding_source and not _grounded_line(new, vocab):
                continue
            seg.text = new
            kept += 1
        if kept > 0:
            script.source = f"grounded-llm:{self.llm.config.provider}"
            script.notes.append(f"LLM polished {kept}/{len(lines)} lines")
            return script
        return None

    # ------------------------------------------------------------ degraded
    def _minimal(self, site: str, duration: float, transcript_text: str, *, reason: str) -> GroundedScript:
        """A short, clearly grounded script when there is nothing to look at."""
        site_name = site or self.brand
        duration = max(6.0, duration or 12.0)
        pieces: list[tuple[str, str]] = [("hook", f"A quick look at {site_name}.")]
        spoken = clean_transcript(transcript_text or "")
        sentences = split_sentences(spoken)[:3]
        for s in sentences:
            pieces.append(("demo", _punctuate(s)))
        if not sentences:
            pieces.append(("demo", f"Here is {site_name} on screen, step by step."))
        pieces.append(("cta", self.config.cta_default or f"Follow {self.brand} for more."))
        slot = duration / len(pieces)
        segments = [
            ScriptSegment(
                start=round(i * slot, 3),
                end=round((i + 1) * slot, 3),
                text=text,
                role=role,
                visual_action="recording (no visual analysis available)",
                focus_region=None,
                evidence=["transcript"] if role == "demo" and sentences else [],
                source_start=round(i * slot, 3),
                source_end=round((i + 1) * slot, 3),
            )
            for i, (role, text) in enumerate(pieces)
        ]
        script = GroundedScript(
            title=_title_case(f"{site_name} quick look"),
            segments=segments,
            keywords=extract_keywords(spoken, top_n=6) if spoken else [],
            website=site_name,
            source="grounded-minimal",
            grounded=True,
            degraded=True,
            notes=[f"minimal script: {reason}", "no features are claimed beyond what was recorded"],
        )
        log.warning("Grounded script degraded (%s) - %d minimal segments", reason, len(segments))
        return script


# ---------------------------------------------------------------- helpers
_KIND_PRIORITY = ("click", "reveal", "navigate", "type", "scroll")


def _dominant_kind(actions: list[ActionEvent]) -> str:
    """The action a viewer would say the shot is about."""
    kinds = {a.kind for a in actions}
    for kind in _KIND_PRIORITY:
        if kind in kinds:
            return kind
    return ""


def _pick(variants: list[str], budget: int, used: set[str] | None = None) -> str:
    """Longest phrasing that fits budget and has not been used recently."""
    valid = [v for v in variants if v and len(v.split()) <= budget]
    if used is not None and valid:
        unused = [v for v in valid if v not in used]
        if unused:
            chosen = unused[0]
            used.add(chosen)
            return chosen
    if valid:
        chosen = valid[0]
        if used is not None:
            used.add(chosen)
        return chosen
    shortest = min((v for v in variants if v), key=lambda v: len(v.split()), default="")
    res = _fit_line(shortest, budget)
    if used is not None:
        used.add(res)
    return res


def _mergeable(prev: Beat, nxt: Beat) -> bool:
    """Two consecutive shots share a narration line when they show the same idea."""
    if prev.role != nxt.role:
        return False
    if prev.duration + nxt.duration > 7.5:
        return False
    prev_kinds = {a.kind for a in prev.actions}
    next_kinds = {a.kind for a in nxt.actions}
    if prev_kinds and next_kinds and prev_kinds != next_kinds:
        return False
    return prev.duration < 3.0 or not next_kinds


def _budget_segments(script: GroundedScript, speaking_rate_wps: float) -> None:
    """Shorten lines that cannot be spoken inside their own visual slot.

    Speech is fitted again at synthesis time (atempo), so a slot may carry a
    little more than its nominal word budget; what matters here is that no line
    is so long that the narration drifts away from the picture.
    """
    for seg in script.segments:
        if seg.role == "cta":
            continue  # the call to action is brand copy: spoken slightly faster, never cut
        budget = max(4, int(seg.duration * max(1.0, speaking_rate_wps) * 1.12))
        seg.text = _fit_line(seg.text, budget)


def _fit_line(text: str, budget: int) -> str:
    """Shorten a line to ``budget`` words, cutting at a clause when possible."""
    words = text.split()
    if len(words) <= budget:
        return text
    for sep in (" - ", ", ", " and ", " so ", " that "):
        head = text.split(sep)[0]
        if head and len(head.split()) <= budget:
            return _punctuate(head.strip(" .,:;-"))
    return _punctuate(" ".join(words[:budget]).rstrip(",;:-"))


def _grounded_line(line: str, vocabulary: set[str]) -> bool:
    words = _content_words(line)
    if not words:
        return False
    novel = [w for w in words if w not in vocabulary and not _is_generic(w)]
    return len(novel) / len(words) <= 0.35


def _shorten(text: str, max_words: int) -> str:
    words = [w for w in text.split() if w.strip()]
    return " ".join(words[:max_words]).strip(" .,:;-")


def _first_sentence(text: str, max_words: int) -> str:
    parts = split_sentences(text)
    first = parts[0] if parts else text
    return _punctuate(_shorten(first, max_words))


def plan_grounded_script(
    edit: EditPlan,
    understanding: VideoUnderstanding | None,
    config: ScriptConfig,
    *,
    brand: str = "StudentTools.pk",
    website: str = "",
    website_context: str = "",
    transcript_text: str = "",
    creative_plan: Any = None,
    llm: LLMClient | None = None,
) -> GroundedScript:
    """Convenience wrapper used by the pipeline step."""
    planner = GroundedScriptPlanner(config, brand=brand, llm=llm)
    return planner.plan(
        edit,
        understanding,
        website=website,
        website_context=website_context,
        transcript_text=transcript_text,
        creative_plan=creative_plan,
    )

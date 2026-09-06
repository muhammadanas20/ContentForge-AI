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

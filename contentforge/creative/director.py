"""AI Creative Director — the *brain* of the v0.4 premium pipeline.

Before any rendering happens, the Creative Director reviews the video
understanding and produces a unified :class:`CreativePlan` that shapes every
downstream decision:

* narrative order (chronological vs result-first)
* hook style and phrasing direction
* pacing / editing intensity
* music mood and energy
* caption style
* cover style and concept
* CTA approach

With Gemini available, these decisions are made by a vision-capable AI that
has actually *seen* the key frames of the recording. Without Gemini, the
director falls back to deterministic rules based on action analysis.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from contentforge.ai.llm import LLMClient, LLMConfig
from contentforge.log import get_logger
from contentforge.models.schemas import VideoUnderstanding

log = get_logger("creative")

# ------------------------------------------------------------------ schemas

HOOK_STYLES = (
    "curiosity",
    "surprising_discovery",
    "problem_solution",
    "result_first",
    "challenge",
    "time_saving",
    "hidden_tool",
    "comparison",
)

MUSIC_MOODS = (
    "energetic",
    "tech",
    "educational",
    "motivational",
    "calm",
    "cinematic",
    "playful",
    "minimal",
)

CAPTION_STYLES = (
    "clean",
    "bold",
    "kinetic",
    "minimal",
    "educational",
)

COVER_STYLES = (
    "bold_text_screenshot",
    "result_first",
    "curiosity",
    "before_after",
    "ui_focused",
    "minimal_premium",
    "branded_educational",
)

NARRATIVE_ORDERS = (
    "chronological",
    "result_first",
    "problem_solution",
    "hook_demo_payoff",
)


@dataclass
class CreativePlan:
    """The unified creative direction for a Reel.

    Every downstream module (script, edit, captions, music, cover) reads the
    plan to make consistent decisions instead of each guessing independently.
    """

    # Concept
    concept: str = ""          # One-line creative concept
    narrative_order: str = "chronological"  # one of NARRATIVE_ORDERS

    # Hook
    hook_style: str = "curiosity"
    hook_candidates: list[str] = field(default_factory=list)
    hook_scores: dict[str, float] = field(default_factory=dict)

    # Visual
    editing_intensity: float = 0.6   # 0=minimal cuts, 1=fast-paced
    zoom_intensity: float = 0.4      # 0=no zooms, 1=frequent punch-ins
    layout_preference: str = "auto"  # auto | fill | canvas
    motion_style: str = "smooth"     # smooth | dynamic | minimal

    # Audio
    music_mood: str = "tech"
    music_energy: float = 0.7       # 0=ambient, 1=high energy
    sfx_level: str = "subtle"       # none | subtle | moderate

    # Captions
    caption_style: str = "bold"

    # Cover
    cover_style: str = "bold_text_screenshot"
    cover_headline: str = ""

    # CTA
    cta_text: str = ""
    cta_style: str = "follow"  # follow | save | share | link

    # Meta
    target_duration: float = 0.0  # seconds, 0 = auto
    content_category: str = "ai_tool"  # ai_tool | productivity | tutorial | website_discovery
    source: str = "rule-based"  # rule-based | gemini
    confidence: float = 0.5
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CreativePlan:
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})

    def to_markdown(self) -> str:
        lines = [
            "# Creative Plan",
            "",
            f"**Concept**: {self.concept}",
            f"**Narrative order**: {self.narrative_order}",
            f"**Hook style**: {self.hook_style}",
            f"**Music**: {self.music_mood} (energy {self.music_energy:.1f})",
            f"**Captions**: {self.caption_style}",
            f"**Cover**: {self.cover_style}",
            f"**CTA**: {self.cta_text} ({self.cta_style})",
            f"**Category**: {self.content_category}",
            f"**Source**: {self.source} (confidence {self.confidence:.2f})",
            "",
        ]
        if self.hook_candidates:
            lines.append("## Hook candidates")
            for i, h in enumerate(self.hook_candidates, 1):
                score = self.hook_scores.get(h, 0)
                lines.append(f"{i}. {h} (score: {score:.2f})")
            lines.append("")
        if self.notes:
            lines.append("## Notes")
            lines += [f"- {n}" for n in self.notes]
        return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ director

GEMINI_CREATIVE_PROMPT = """\
You are a senior short-form video creative director for {brand}.

You are given a description of a screen recording that demonstrates a website/tool.
Your job is to produce a creative plan for an Instagram Reel that will maximise
viewer retention and clarity.

Available information:
- Website: {website}
- Recording duration: {duration:.1f}s
- Actions detected: {action_summary}
- Key on-screen text: {ocr_summary}
{context_note}

Produce a JSON creative plan with these fields:
- concept: one sentence describing the Reel concept
- narrative_order: "chronological" | "result_first" | "problem_solution" | "hook_demo_payoff"
- hook_style: "curiosity" | "surprising_discovery" | "problem_solution" | "result_first" | "time_saving" | "hidden_tool"
- hook_candidates: list of 3-5 hook text options (short, punchy, student-friendly)
- music_mood: "energetic" | "tech" | "educational" | "motivational" | "calm" | "cinematic" | "minimal"
- music_energy: float 0-1
- caption_style: "clean" | "bold" | "minimal" | "educational"
- cover_style: "bold_text_screenshot" | "result_first" | "curiosity" | "ui_focused" | "minimal_premium"
- cover_headline: short headline for the cover (max 7 words)
- cta_text: call-to-action text
- content_category: "ai_tool" | "productivity" | "tutorial" | "website_discovery"
- editing_intensity: float 0-1 (how fast-paced the edit should be)
- zoom_intensity: float 0-1

Rules:
1. NEVER invent features. Only reference what the recording shows.
2. Hook must be specific to the content, not generic.
3. CTA should mention {brand}.
4. Prefer "result_first" narrative when a clear result/output is visible.
5. Match music mood to the content tone.
"""


class CreativeDirector:
    """Produces a unified creative plan before rendering starts."""

    def __init__(
        self,
        brand: str = "StudentTools.pk",
        llm: LLMClient | None = None,
    ):
        self.brand = brand
        self.llm = llm or LLMClient()

    def plan(
        self,
        understanding: VideoUnderstanding | None,
        *,
        website: str = "",
        website_context: str = "",
        key_frames: list[Path] | None = None,
    ) -> CreativePlan:
        """Produce a creative plan for the recording."""
        # Try Gemini-powered planning first
        if self.llm.enabled and understanding is not None:
            plan = self._ai_plan(understanding, website, website_context, key_frames)
            if plan is not None:
                return plan

        # Fall back to rule-based planning
        return self._rule_based_plan(understanding, website)

    def _ai_plan(
        self,
        u: VideoUnderstanding,
        website: str,
        website_context: str,
        key_frames: list[Path] | None,
    ) -> CreativePlan | None:
        """Use Gemini to produce an intelligent creative plan."""
        # Build the action summary
        action_counts: dict[str, int] = {}
        for a in u.actions:
            action_counts[a.kind] = action_counts.get(a.kind, 0) + 1
        action_summary = ", ".join(f"{v}x {k}" for k, v in action_counts.items()) or "none detected"

        # Get dominant on-screen text
        ocr_texts = u.dominant_texts(limit=15)
        ocr_summary = ", ".join(ocr_texts) if ocr_texts else "no text detected"

        context_note = ""
        if website_context:
            context_note = f"- Additional context: {website_context}"

        system = GEMINI_CREATIVE_PROMPT.format(
            brand=self.brand,
            website=website or u.website or "unknown",
            duration=u.duration,
            action_summary=action_summary,
            ocr_summary=ocr_summary,
            context_note=context_note,
        )

        user_msg = (
            f"Create a creative plan for this {u.duration:.0f}s screen recording "
            f"of {website or u.website or 'a website'}."
        )

        # Try vision mode with key frames if available
        data: dict[str, Any] | None = None
        if key_frames and self.llm.supports_vision:
            data = self.llm.complete_vision_json(system, user_msg, key_frames[:3])
        if data is None:
            data = self.llm.complete_json(system, user_msg)

        if data is None:
            return None

        try:
            plan = CreativePlan(
                concept=str(data.get("concept", "")),
                narrative_order=str(data.get("narrative_order", "chronological")),
                hook_style=str(data.get("hook_style", "curiosity")),
                hook_candidates=list(data.get("hook_candidates", [])),
                music_mood=str(data.get("music_mood", "tech")),
                music_energy=float(data.get("music_energy", 0.7)),
                caption_style=str(data.get("caption_style", "bold")),
                cover_style=str(data.get("cover_style", "bold_text_screenshot")),
                cover_headline=str(data.get("cover_headline", "")),
                cta_text=str(data.get("cta_text", "")),
                content_category=str(data.get("content_category", "ai_tool")),
                editing_intensity=float(data.get("editing_intensity", 0.6)),
                zoom_intensity=float(data.get("zoom_intensity", 0.4)),
                source="gemini",
                confidence=0.85,
            )
            # Score hook candidates
            if plan.hook_candidates:
                plan.hook_scores = {
                    h: self._score_hook(h, u) for h in plan.hook_candidates
                }
            plan.notes.append(f"Generated by {self.llm.config.model}")
            log.info(
                "Creative plan: %s (%s hook, %s music, %s cover)",
                plan.concept[:60],
                plan.hook_style,
                plan.music_mood,
                plan.cover_style,
            )
            return plan
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("Failed to parse Gemini creative plan: %s", exc)
            return None

    def _rule_based_plan(
        self,
        u: VideoUnderstanding | None,
        website: str,
    ) -> CreativePlan:
        """Deterministic creative plan when no AI is available."""
        plan = CreativePlan(source="rule-based", confidence=0.5)

        if u is None:
            plan.concept = f"Explore {website}" if website else "Website demo"
            plan.notes.append("No video understanding available - using defaults")
            return plan

        # Detect content category from actions
        has_clicks = any(a.kind == "click" for a in u.actions)
        has_typing = any(a.kind == "type" for a in u.actions)
        has_reveal = any(a.kind == "reveal" for a in u.actions)
        has_scroll = any(a.kind == "scroll" for a in u.actions)

        # Determine narrative order
        if has_reveal:
            plan.narrative_order = "result_first"
            plan.hook_style = "result_first"
            plan.notes.append("Result detected - using result-first narrative")
        elif has_typing and has_clicks:
            plan.narrative_order = "hook_demo_payoff"
            plan.hook_style = "curiosity"
        else:
            plan.narrative_order = "chronological"
            plan.hook_style = "hidden_tool"

        # Music mood based on action density
        action_rate = len(u.actions) / max(1.0, u.duration) * 10
        if action_rate > 3:
            plan.music_mood = "energetic"
            plan.music_energy = 0.8
        elif action_rate > 1.5:
            plan.music_mood = "tech"
            plan.music_energy = 0.65
        else:
            plan.music_mood = "educational"
            plan.music_energy = 0.45

        # Editing intensity
        plan.editing_intensity = min(1.0, action_rate / 4.0)

        # Concept
        site = website or u.website or "this website"
        if has_reveal:
            plan.concept = f"See what {site} can do in seconds"
        elif has_typing:
            plan.concept = f"How to use {site} step by step"
        else:
            plan.concept = f"Discover {site}"

        # Hook candidates (rule-based)
        plan.hook_candidates = [
            f"This website can save you hours of work.",
            f"Students are sleeping on {site}.",
            f"You need to know about {site}.",
            f"Stop doing things the hard way.",
        ]
        plan.hook_scores = {h: self._score_hook(h, u) for h in plan.hook_candidates}

        # CTA
        plan.cta_text = f"Follow {self.brand} for more free student tools."

        # Cover
        if has_reveal:
            plan.cover_style = "result_first"
        else:
            plan.cover_style = "bold_text_screenshot"

        plan.cover_headline = (
            plan.concept[:40] if len(plan.concept) <= 40
            else " ".join(plan.concept.split()[:7])
        )

        log.info(
            "Creative plan (rule-based): %s (%s hook, %s music)",
            plan.concept[:60],
            plan.hook_style,
            plan.music_mood,
        )
        return plan

    def _score_hook(self, hook: str, u: VideoUnderstanding | None) -> float:
        """Score a hook candidate (0-1). Higher = better."""
        score = 0.5  # baseline

        words = hook.split()
        word_count = len(words)

        # Brevity bonus (5-12 words ideal)
        if 5 <= word_count <= 12:
            score += 0.1
        elif word_count > 18:
            score -= 0.2

        # Specificity: does the hook reference specific content?
        if u and u.website:
            if u.website.lower() in hook.lower():
                score += 0.15  # mentions the actual website

        # Curiosity markers
        curiosity_words = {"discover", "secret", "hidden", "actually", "stop", "need",
                          "save", "hours", "free", "nobody", "sleeping"}
        if any(w.lower().rstrip(".,!?") in curiosity_words for w in words):
            score += 0.1

        # Question hook bonus
        if hook.strip().endswith("?"):
            score += 0.05

        # Penalise vague/generic hooks
        vague_words = {"amazing", "awesome", "incredible", "best", "must-see"}
        if any(w.lower().rstrip(".,!?") in vague_words for w in words):
            score -= 0.15

        return max(0.0, min(1.0, score))

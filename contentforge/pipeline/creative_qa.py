"""Creative quality assessment — scores the *artistic* quality of a Reel.

The existing :mod:`contentforge.pipeline.quality` handles **technical** gates
(valid MP4, correct aspect, audio levels).  This module adds **creative** scores
that measure whether the Reel is *good*, not just *valid*.

Every score is a float 0.0–1.0.  Scores are informational by default (they
produce warnings, not blocking errors) — the operator can tighten thresholds
once the system is trusted.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from contentforge.log import get_logger
from contentforge.models.schemas import EditPlan, GroundedScript, VideoUnderstanding

log = get_logger("creative_qa")


@dataclass
class EditingScore:
    """Per-dimension creative quality scores (0.0–1.0, higher = better)."""

    hook_strength: float = 0.0
    first_3s_clarity: float = 0.0
    visual_change_rate: float = 0.0
    information_density: float = 0.0
    pacing: float = 0.0
    payoff_strength: float = 0.0
    caption_readability: float = 0.0
    audio_clarity: float = 0.0
    cta_quality: float = 0.0
    visual_coherence: float = 0.0

    @property
    def overall(self) -> float:
        """Weighted overall score."""
        weights = {
            "hook_strength": 1.5,
            "first_3s_clarity": 1.2,
            "visual_change_rate": 0.8,
            "information_density": 0.9,
            "pacing": 1.0,
            "payoff_strength": 1.1,
            "caption_readability": 0.7,
            "audio_clarity": 0.8,
            "cta_quality": 0.6,
            "visual_coherence": 0.7,
        }
        total_weight = sum(weights.values())
        weighted_sum = sum(
            getattr(self, k) * w for k, w in weights.items()
        )
        return weighted_sum / max(1e-6, total_weight)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["overall"] = round(self.overall, 4)
        return {k: round(v, 4) if isinstance(v, float) else v for k, v in d.items()}


class CreativeQA:
    """Evaluate the creative quality of a rendered Reel."""

    def evaluate(
        self,
        *,
        plan: EditPlan | None = None,
        understanding: VideoUnderstanding | None = None,
        script: GroundedScript | None = None,
        narration_coverage: float = 0.0,
        caption_count: int = 0,
        duration: float = 0.0,
    ) -> EditingScore:
        """Run all creative quality checks and return a score."""
        score = EditingScore()

        if plan is not None:
            score.hook_strength = self._hook_strength(plan, script)
            score.first_3s_clarity = self._first_3s_clarity(plan, script)
            score.visual_change_rate = self._visual_change_rate(plan)
            score.pacing = self._pacing(plan)
            score.payoff_strength = self._payoff_strength(plan, understanding)
            score.visual_coherence = self._visual_coherence(plan)

        if script is not None:
            score.information_density = self._information_density(script, duration)
            score.cta_quality = self._cta_quality(script)

        if caption_count > 0:
            score.caption_readability = self._caption_readability(caption_count, duration)

        score.audio_clarity = self._audio_clarity(narration_coverage)

        log.info(
            "Creative QA: overall=%.2f (hook=%.2f pacing=%.2f payoff=%.2f)",
            score.overall,
            score.hook_strength,
            score.pacing,
            score.payoff_strength,
        )
        return score

    # -------------------------------------------------------- individual scores

    def _hook_strength(self, plan: EditPlan, script: GroundedScript | None) -> float:
        """Is the first segment a strong hook?"""
        if not plan.shots:
            return 0.0
        score = 0.3  # baseline

        # First shot role
        first = plan.shots[0]
        if first.role == "hook":
            score += 0.3

        # Duration of the hook (ideal: 1.5-3s)
        if 1.0 <= first.out_duration <= 3.5:
            score += 0.2
        elif first.out_duration > 5.0:
            score -= 0.2

        # Script has a hook segment
        if script:
            has_hook = any(s.role == "hook" for s in script.segments)
            if has_hook:
                score += 0.2

        return max(0.0, min(1.0, score))

    def _first_3s_clarity(self, plan: EditPlan, script: GroundedScript | None) -> float:
        """Is what/why communicated in the first 3 seconds?"""
        if not plan.shots:
            return 0.0
        score = 0.4  # baseline

        # How much of the first 3 seconds has content
        covered = sum(
            min(3.0, s.out_end) - s.out_start
            for s in plan.shots
            if s.out_start < 3.0
        )
        if covered >= 2.5:
            score += 0.2

        # Script starts quickly
        if script and script.segments:
            first_seg = script.segments[0]
            if first_seg.start < 0.5:
                score += 0.2
            if len(first_seg.text.split()) >= 3:
                score += 0.2

        return max(0.0, min(1.0, score))

    def _visual_change_rate(self, plan: EditPlan) -> float:
        """Does the edit feel dynamic? (Not too static, not too chaotic.)"""
        if not plan.shots or plan.duration <= 0:
            return 0.0

        cuts_per_minute = len(plan.shots) / max(0.01, plan.duration / 60.0)

        # Ideal: 8-20 cuts per minute for short-form
        if 8 <= cuts_per_minute <= 20:
            return 0.9
        if 5 <= cuts_per_minute < 8:
            return 0.7
        if 20 < cuts_per_minute <= 30:
            return 0.6
        if cuts_per_minute < 5:
            return 0.4
        return 0.3  # too chaotic

    def _pacing(self, plan: EditPlan) -> float:
        """Rhythm: does shot duration vary appropriately?"""
        if len(plan.shots) < 2:
            return 0.5

        durations = [s.out_duration for s in plan.shots]
        avg = sum(durations) / len(durations)
        variance = sum((d - avg) ** 2 for d in durations) / len(durations)
        std_dev = variance ** 0.5

        # Some variation is good (0.3-1.5s std dev), too much is chaotic
        if 0.3 <= std_dev <= 1.5:
            score = 0.9
        elif std_dev < 0.3:
            score = 0.6  # monotonous
        else:
            score = 0.5  # too erratic

        # Penalise any shot exceeding max reasonable duration
        long_shots = sum(1 for d in durations if d > 6.0)
        score -= long_shots * 0.1

        return max(0.0, min(1.0, score))

    def _payoff_strength(
        self, plan: EditPlan, understanding: VideoUnderstanding | None
    ) -> float:
        """Is the result/payoff moment clear and held?"""
        score = 0.4  # baseline

        # Check if payoff role exists
        has_payoff = any(s.role == "payoff" for s in plan.shots)
        if has_payoff:
            score += 0.3

        # Check for reveal emphasis
        if plan.emphasis:
            reveals = [e for e in plan.emphasis if e.kind in ("reveal", "result")]
            if reveals:
                score += 0.2

        # Check if result is held long enough
        if understanding:
            reveal_actions = [a for a in understanding.actions if a.kind == "reveal"]
            if reveal_actions:
                score += 0.1

        return max(0.0, min(1.0, score))

    def _information_density(self, script: GroundedScript, duration: float) -> float:
        """Words/second balanced with visual content."""
        if duration <= 0 or not script.segments:
            return 0.0

        wps = script.word_count / duration

        # Ideal: 2.0-3.5 words/second for narrated short-form
        if 2.0 <= wps <= 3.5:
            return 0.9
        if 1.5 <= wps < 2.0 or 3.5 < wps <= 4.0:
            return 0.7
        if wps < 1.5:
            return 0.4  # too sparse
        return 0.5  # too dense

    def _cta_quality(self, script: GroundedScript) -> float:
        """Is the CTA present, specific, and actionable?"""
        cta_segments = [s for s in script.segments if s.role == "cta"]
        if not cta_segments:
            return 0.2

        cta_text = " ".join(s.text for s in cta_segments).lower()

        score = 0.5  # has a CTA

        # Specific action words
        action_words = {"follow", "save", "share", "click", "link", "check out", "try", "visit"}
        if any(w in cta_text for w in action_words):
            score += 0.2

        # Mentions brand
        if "studenttools" in cta_text or "student tools" in cta_text:
            score += 0.2

        # Not too long
        word_count = len(cta_text.split())
        if word_count <= 15:
            score += 0.1

        return max(0.0, min(1.0, score))

    def _caption_readability(self, caption_count: int, duration: float) -> float:
        """Are captions properly distributed?"""
        if duration <= 0 or caption_count == 0:
            return 0.0

        captions_per_second = caption_count / duration

        # Ideal: 0.3-0.8 chunks/second (one chunk every 1.2-3.3 seconds)
        if 0.3 <= captions_per_second <= 0.8:
            return 0.9
        if 0.15 <= captions_per_second < 0.3 or 0.8 < captions_per_second <= 1.0:
            return 0.7
        return 0.4

    def _audio_clarity(self, narration_coverage: float) -> float:
        """Is narration clear and covering the timeline?"""
        # Coverage: 60-85% is ideal (some visual-only moments are fine)
        if 0.55 <= narration_coverage <= 0.90:
            return 0.9
        if 0.4 <= narration_coverage < 0.55 or 0.9 < narration_coverage <= 0.98:
            return 0.7
        if narration_coverage < 0.3:
            return 0.3
        return 0.6

    def _visual_coherence(self, plan: EditPlan) -> float:
        """Consistent framing, no excessive layout switching."""
        if not plan.shots:
            return 0.5

        layouts = [s.layout for s in plan.shots]
        unique_layouts = set(layouts)

        # One consistent layout = good
        if len(unique_layouts) == 1:
            return 0.9

        # Alternating is fine if there's a clear pattern
        switches = sum(1 for i in range(1, len(layouts)) if layouts[i] != layouts[i - 1])
        switch_rate = switches / max(1, len(layouts) - 1)

        if switch_rate <= 0.3:
            return 0.8
        if switch_rate <= 0.5:
            return 0.6
        return 0.4  # too much switching

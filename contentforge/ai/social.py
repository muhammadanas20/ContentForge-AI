"""Instagram caption, hashtag, CTA and SEO description generation.

Everything works offline with curated pools and deterministic rotation; an LLM
(if configured) is used only to polish the caption text and is grounded on the
script (which is itself grounded on the transcript).
"""

from __future__ import annotations

import random
from dataclasses import asdict, dataclass, field
from typing import Any

from contentforge.ai.llm import LLMClient
from contentforge.ai.script_writer import Script
from contentforge.config.schema import SocialConfig
from contentforge.log import get_logger

log = get_logger("social")

HASHTAG_POOLS: dict[str, list[str]] = {
    "brand": ["#studenttoolspk", "#studenttools", "#studenttoolsreels"],
    "students": [
        "#students",
        "#studentlife",
        "#universitylife",
        "#collegelife",
        "#studytips",
        "#studygram",
        "#studymotivation",
        "#studentsuccess",
        "#examtips",
        "#assignmenthelp",
        "#studysmart",
        "#studyhacks",
        "#studenthacks",
        "#learnontiktok",
        "#studywithme",
    ],
    "pakistan": [
        "#pakistanistudents",
        "#pakistan",
        "#karachi",
        "#lahore",
        "#islamabad",
        "#pakistaniyouth",
        "#pakistaneducation",
        "#punjabuniversity",
        "#nust",
        "#lums",
        "#fast",
        "#comsats",
        "#uet",
        "#pakistaniuniversities",
        "#pakstudents",
    ],
    "productivity": [
        "#productivity",
        "#productivityhacks",
        "#productivitytips",
        "#timemanagement",
        "#lifehacks",
        "#workflow",
        "#efficiency",
        "#studyproductivity",
        "#getthingsdone",
        "#organized",
    ],
    "tech": [
        "#tech",
        "#techtips",
        "#techtok",
        "#freetools",
        "#freewebsites",
        "#websites",
        "#usefulwebsites",
        "#onlinetools",
        "#techhacks",
        "#internethacks",
        "#software",
        "#apps",
    ],
    "education": [
        "#education",
        "#learning",
        "#edtech",
        "#onlinelearning",
        "#elearning",
        "#knowledge",
        "#learnsomethingnew",
        "#educationalcontent",
        "#teachersofinstagram",
        "#edutok",
    ],
    "viral": [
        "#reels",
        "#reelsinstagram",
        "#viral",
        "#trending",
        "#explore",
        "#explorepage",
        "#fyp",
        "#foryou",
        "#reelitfeelit",
        "#instagood",
        "#viralreels",
        "#shorts",
    ],
    "ai": [
        "#ai",
        "#aitools",
        "#artificialintelligence",
        "#chatgpt",
        "#aiforstudents",
        "#machinelearning",
    ],
}

TOPIC_TAGS: dict[str, list[str]] = {
    "pdf": ["#pdf", "#pdftools", "#pdfconverter"],
    "resume": ["#resume", "#cv", "#resumetips", "#careertips"],
    "cv": ["#cv", "#resume", "#jobsearch"],
    "presentation": ["#presentation", "#powerpoint", "#slides"],
    "notes": ["#notes", "#notetaking", "#studynotes"],
    "math": ["#math", "#mathematics", "#mathhelp"],
    "research": ["#research", "#researchpaper", "#thesis", "#academicwriting"],
    "citation": ["#citation", "#apa", "#references"],
    "grammar": ["#grammar", "#writing", "#essaywriting"],
    "essay": ["#essay", "#essaywriting", "#academicwriting"],
    "code": ["#coding", "#programming", "#developer"],
    "python": ["#python", "#coding", "#programming"],
    "design": ["#design", "#canva", "#graphicdesign"],
    "video": ["#videoediting", "#editing", "#contentcreation"],
    "image": ["#imageediting", "#photoediting"],
    "ai": ["#aitools", "#chatgpt", "#aiforstudents"],
    "job": ["#jobs", "#internship", "#careertips"],
    "scholarship": ["#scholarship", "#scholarships", "#studyabroad"],
    "plagiarism": ["#plagiarism", "#academicintegrity"],
    "summary": ["#summarizer", "#studysmart"],
    "translate": ["#translation", "#languagelearning"],
}


@dataclass
class SocialPackage:
    title: str
    caption: str
    cta: str
    comment_prompt: str
    hashtags: list[str]
    seo_description: str
    youtube_title: str
    keywords: list[str] = field(default_factory=list)
    source: str = "rule-based"

    @property
    def full_caption(self) -> str:
        return f"{self.caption}\n\n{self.cta}\n{self.comment_prompt}\n\n{' '.join(self.hashtags)}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["full_caption"] = self.full_caption
        return d

    def to_markdown(self) -> str:
        return (
            "\n".join(
                [
                    f"# {self.title}",
                    "",
                    "## Instagram caption (copy-paste)",
                    "",
                    "```",
                    self.full_caption,
                    "```",
                    "",
                    "## YouTube Shorts title",
                    "",
                    self.youtube_title,
                    "",
                    "## SEO description",
                    "",
                    self.seo_description,
                    "",
                    "## CTA",
                    "",
                    self.cta,
                    "",
                    "## Comment prompt",
                    "",
                    self.comment_prompt,
                    "",
                    "## Hashtags",
                    "",
                    " ".join(self.hashtags),
                    "",
                ]
            )
            + "\n"
        )


class HashtagGenerator:
    """Category-balanced hashtag sets with topic tags and repeat avoidance."""

    def __init__(self, config: SocialConfig, rng: random.Random | None = None):
        self.config = config
        self.rng = rng or random.Random()

    def generate(
        self, keywords: list[str], recent_sets: list[list[str]] | None = None
    ) -> list[str]:
        cfg = self.config.hashtags
        banned = {b.lower() if b.startswith("#") else f"#{b.lower()}" for b in cfg.banned}
        recent = {t for s in (recent_sets or []) for t in s}
        chosen: list[str] = []

        def add(tag: str) -> None:
            t = tag.lower()
            if t not in chosen and t not in banned:
                chosen.append(t)

        # 1) brand tags always first
        for t in HASHTAG_POOLS["brand"][:2]:
            add(t)
        # 2) topic tags from keywords
        for kw in keywords:
            for key, tags in TOPIC_TAGS.items():
                if key in kw.lower():
                    for t in tags:
                        add(t)
            if len(chosen) >= cfg.count // 2:
                break
        # 3) fill from categories round-robin, preferring tags not used recently
        cats = [c for c in cfg.categories if c in HASHTAG_POOLS and c != "brand"] or [
            "students",
            "viral",
        ]
        pools = {c: self._shuffled(HASHTAG_POOLS[c], recent) for c in cats}
        while len(chosen) < cfg.count and any(pools.values()):
            for c in cats:
                if pools[c]:
                    add(pools[c].pop(0))
                if len(chosen) >= cfg.count:
                    break
        return chosen[: cfg.count]

    def _shuffled(self, pool: list[str], recent: set[str]) -> list[str]:
        fresh = [t for t in pool if t not in recent]
        stale = [t for t in pool if t in recent]
        self.rng.shuffle(fresh)
        self.rng.shuffle(stale)
        return fresh + stale  # stale only used when fresh ones run out


class SocialWriter:
    SYSTEM = (
        "You write Instagram Reel captions for {brand}, a page sharing free tools for Pakistani students. "
        "Use ONLY facts in the provided script. No invented features. Friendly, punchy, 2-4 short lines, "
        'one emoji max per line, no hashtags. Return JSON: {{"caption": str, "youtube_title": str, '
        '"seo_description": str}}'
    )

    def __init__(
        self,
        config: SocialConfig,
        brand: str = "StudentTools.pk",
        llm: LLMClient | None = None,
        rng: random.Random | None = None,
    ):
        self.config = config
        self.brand = brand
        self.llm = llm or LLMClient()
        self.rng = rng or random.Random()
        self.hashtags = HashtagGenerator(config, self.rng)

    def write(
        self, script: Script, *, recent_hashtags: list[list[str]] | None = None, website: str = ""
    ) -> SocialPackage:
        site = website or script.website or self.brand
        cta = self.rng.choice(self.config.cta_variants) if self.config.cta_variants else script.cta
        prompt = self.rng.choice(self.config.comment_prompts) if self.config.comment_prompts else ""
        tags = self.hashtags.generate(script.keywords, recent_hashtags)

        caption = youtube_title = seo = ""
        source = "rule-based"
        if self.llm.enabled:
            data = self.llm.complete_json(
                self.SYSTEM.format(brand=self.brand),
                f"Website: {site}\nScript:\n{script.narration}",
            )
            if data and data.get("caption"):
                caption = str(data["caption"]).strip()
                youtube_title = str(data.get("youtube_title", "")).strip()
                seo = str(data.get("seo_description", "")).strip()
                source = f"llm:{self.llm.config.provider}"
        if not caption:
            caption = self._rule_caption(script, site)
        if not youtube_title:
            youtube_title = self._youtube_title(script)
        if not seo:
            seo = self._seo(script, site)

        pkg = SocialPackage(
            title=script.title,
            caption=caption[: self.config.caption_max_length - 400],
            cta=cta,
            comment_prompt=prompt,
            hashtags=tags,
            seo_description=seo,
            youtube_title=youtube_title,
            keywords=script.keywords,
            source=source,
        )
        if len(pkg.full_caption) > self.config.caption_max_length:
            pkg.hashtags = pkg.hashtags[: max(5, len(pkg.hashtags) - 5)]
        log.info(
            "Social package ready: %d hashtags, %d-char caption (%s)",
            len(pkg.hashtags),
            len(pkg.full_caption),
            source,
        )
        return pkg

    # ------------------------------------------------------------ rules
    def _rule_caption(self, script: Script, site: str) -> str:
        lines = [script.hook.strip()]
        steps = [b for b in script.body if b.strip()][:3]
        if steps:
            lines.append("")
            lines.extend(f"{i}. {s.rstrip('.')}" for i, s in enumerate(steps, 1))
        lines.append("")
        lines.append(
            f"Website: {site}" if site.lower() != self.brand.lower() else f"More at {self.brand}"
        )
        return "\n".join(lines)

    def _youtube_title(self, script: Script) -> str:
        base = script.title.strip().rstrip(".")
        title = f"{base} (Free Tool for Students) #shorts"
        return title if len(title) <= 100 else f"{base[:80]} #shorts"

    def _seo(self, script: Script, site: str) -> str:
        kws = ", ".join(script.keywords[:6])
        return (
            f"{script.title}. In this short video, {self.brand} shows how {site} helps students "
            f"with {kws or 'everyday study tasks'}. {script.body[0] if script.body else ''} "
            f"Free tool, no fluff. {script.cta}"
        ).strip()

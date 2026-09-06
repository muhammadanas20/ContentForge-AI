"""Performance analytics: store metrics, compute rates, and suggest improvements.

Metrics are entered manually (or via CSV import) from the Instagram / YouTube
insights screens - we deliberately do not scrape platforms, which would
violate their terms. Official APIs can be wired in later via
:meth:`AnalyticsService.record`.
"""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from statistics import mean, median
from typing import Any

from contentforge.config.schema import AnalyticsConfig
from contentforge.db import Database
from contentforge.log import get_logger
from contentforge.utils import atomic_write_json, atomic_write_text

log = get_logger("analytics")

METRIC_FIELDS = (
    "views",
    "likes",
    "comments",
    "shares",
    "saves",
    "watch_time_seconds",
    "avg_watch_seconds",
    "completion_rate",
    "follower_growth",
)


@dataclass
class VideoPerformance:
    job_id: str
    title: str
    platform: str
    views: int
    likes: int
    comments: int
    shares: int
    saves: int
    completion_rate: float
    avg_watch_seconds: float
    follower_growth: int
    like_rate: float
    comment_rate: float
    share_rate: float
    save_rate: float
    score: float
    recorded_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AnalyticsReport:
    generated_at: str
    period: str
    videos: list[VideoPerformance]
    totals: dict[str, float]
    averages: dict[str, float]
    top_videos: list[str]
    weak_videos: list[str]
    suggestions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    def to_markdown(self) -> str:
        lines = [
            f"# Analytics report ({self.period})",
            "",
            f"_Generated {self.generated_at}_",
            "",
            "## Totals",
            "",
        ]
        lines += [f"- **{k.replace('_', ' ').title()}:** {_fmt(v)}" for k, v in self.totals.items()]
        lines += ["", "## Averages", ""]
        lines += [
            f"- **{k.replace('_', ' ').title()}:** {_fmt(v)}" for k, v in self.averages.items()
        ]
        lines += [
            "",
            "## Videos",
            "",
            "| Title | Platform | Views | Like % | Comment % | Share % | Completion % | Score |",
            "|---|---|---:|---:|---:|---:|---:|---:|",
        ]
        for v in sorted(self.videos, key=lambda x: -x.score):
            lines.append(
                f"| {v.title[:40]} | {v.platform} | {v.views} | {v.like_rate * 100:.1f} | "
                f"{v.comment_rate * 100:.2f} | {v.share_rate * 100:.2f} | {v.completion_rate * 100:.0f} | {v.score:.0f} |"
            )
        lines += ["", "## Suggestions", ""] + [f"- {s}" for s in self.suggestions]
        return "\n".join(lines) + "\n"


class AnalyticsService:
    def __init__(self, config: AnalyticsConfig, db: Database):
        self.config = config
        self.db = db

    # ------------------------------------------------------------ record
    def record(self, job_id: str | None, platform: str, **metrics: Any) -> int:
        clean: dict[str, Any] = {}
        for k in METRIC_FIELDS:
            if k in metrics and metrics[k] not in (None, ""):
                clean[k] = (
                    float(metrics[k]) if "rate" in k or "seconds" in k else int(float(metrics[k]))
                )
        if "completion_rate" in clean and clean["completion_rate"] > 1:
            clean["completion_rate"] /= 100.0  # accept percentages
        if "notes" in metrics:
            clean["notes"] = str(metrics["notes"])
        if "recorded_at" in metrics and metrics["recorded_at"]:
            clean["recorded_at"] = str(metrics["recorded_at"])
        row_id = self.db.add_metrics(job_id, platform, **clean)
        log.info("Recorded metrics for %s/%s: %s", job_id, platform, clean)
        return row_id

    def import_csv(self, path: Path) -> int:
        """Import rows with columns: job_id, platform, views, likes, ... (header required)."""
        count = 0
        with Path(path).open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                job_id = row.pop("job_id", None) or None
                platform = row.pop("platform", "instagram") or "instagram"
                self.record(job_id, platform, **row)
                count += 1
        return count

    # ----------------------------------------------------------- analyse
    def performances(self) -> list[VideoPerformance]:
        titles = {
            j["id"]: (j.get("title") or j.get("slug") or j["id"])
            for j in self.db.list_jobs(limit=5000)
        }
        out = []
        for m in self.db.latest_metrics_per_job():
            views = int(m.get("views") or 0)
            denom = max(1, views)
            likes, comments = int(m.get("likes") or 0), int(m.get("comments") or 0)
            shares, saves = int(m.get("shares") or 0), int(m.get("saves") or 0)
            completion = float(m.get("completion_rate") or 0)
            perf = VideoPerformance(
                job_id=m.get("job_id") or "",
                title=titles.get(m.get("job_id"), m.get("job_id") or "?"),
                platform=m["platform"],
                views=views,
                likes=likes,
                comments=comments,
                shares=shares,
                saves=saves,
                completion_rate=completion,
                avg_watch_seconds=float(m.get("avg_watch_seconds") or 0),
                follower_growth=int(m.get("follower_growth") or 0),
                like_rate=likes / denom,
                comment_rate=comments / denom,
                share_rate=shares / denom,
                save_rate=saves / denom,
                score=0.0,
                recorded_at=m["recorded_at"],
            )
            perf.score = self.score(perf)
            out.append(perf)
        return out

    def score(self, p: VideoPerformance) -> float:
        """0-100 composite: completion (40 %), engagement rates (45 %), reach (15 %)."""
        b = self.config.benchmarks
        completion = min(1.0, p.completion_rate / max(0.01, b.completion_rate_good))
        like = min(1.0, p.like_rate / max(1e-6, b.like_rate_good))
        share = min(1.0, p.share_rate / max(1e-6, b.share_rate_good))
        comment = min(1.0, p.comment_rate / max(1e-6, b.comment_rate_good))
        reach = min(1.0, (p.views / 10000) ** 0.5) if p.views else 0
        return round(
            100 * (0.40 * completion + 0.20 * like + 0.15 * share + 0.10 * comment + 0.15 * reach),
            1,
        )

    def suggestions(self, perfs: list[VideoPerformance]) -> list[str]:
        b = self.config.benchmarks
        tips: list[str] = []
        if not perfs:
            return [
                "No metrics yet. Add them via the dashboard or `contentforge analytics add` after publishing."
            ]
        avg_completion = mean(p.completion_rate for p in perfs)
        avg_like = mean(p.like_rate for p in perfs)
        avg_share = mean(p.share_rate for p in perfs)
        avg_comment = mean(p.comment_rate for p in perfs)
        if avg_completion < b.completion_rate_poor:
            tips.append(
                "Completion rate is low: shorten videos to 20-35 s, start with the result in the first 2 s, "
                "and increase jump-cut aggressiveness (audio.silence.min_duration -> 0.4)."
            )
        elif avg_completion < b.completion_rate_good:
            tips.append(
                "Completion rate is average: tighten the hook (use 'question' or 'secret' hook styles) and "
                "cut any intro longer than 2 seconds."
            )
        else:
            tips.append("Completion rate is strong: keep the current pacing and length.")
        if avg_like < b.like_rate_good:
            tips.append(
                "Like rate is below target: make the payoff clearer and add on-screen keyword highlights "
                "(subtitles.highlight_keywords) on the benefit words."
            )
        if avg_share < b.share_rate_good:
            tips.append(
                "Shares are low: use the 'Share this with a classmate' CTA variant and pick tools that solve "
                "urgent, deadline-driven problems."
            )
        if avg_comment < b.comment_rate_good:
            tips.append(
                "Few comments: use a comment prompt that asks a question with a one-word answer "
                "(e.g. 'Comment TOOL for the link')."
            )
        best = max(perfs, key=lambda p: p.score)
        worst = min(perfs, key=lambda p: p.score)
        if len(perfs) >= 3 and best.job_id != worst.job_id:
            tips.append(
                f"Best performer: '{best.title}' (score {best.score:.0f}). Study its topic/hook and make 2-3 "
                f"similar videos. Weakest: '{worst.title}' (score {worst.score:.0f})."
            )
        growth = sum(p.follower_growth for p in perfs)
        if growth <= 0 and sum(p.views for p in perfs) > 1000:
            tips.append(
                "Views without follower growth: end every video with the 'Follow for more' CTA card and keep "
                "topics consistent so viewers know what they subscribe to."
            )
        return tips

    def build_report(self, period: str = "all-time") -> AnalyticsReport:
        perfs = self.performances()
        totals = {
            k: float(sum(getattr(p, k) for p in perfs))
            for k in ("views", "likes", "comments", "shares", "saves", "follower_growth")
        }
        averages = {
            "completion_rate": mean(p.completion_rate for p in perfs) if perfs else 0.0,
            "like_rate": mean(p.like_rate for p in perfs) if perfs else 0.0,
            "share_rate": mean(p.share_rate for p in perfs) if perfs else 0.0,
            "comment_rate": mean(p.comment_rate for p in perfs) if perfs else 0.0,
            "median_views": float(median(p.views for p in perfs)) if perfs else 0.0,
            "score": mean(p.score for p in perfs) if perfs else 0.0,
        }
        ranked = sorted(perfs, key=lambda p: -p.score)
        report = AnalyticsReport(
            generated_at=datetime.now().isoformat(timespec="seconds"),
            period=period,
            videos=perfs,
            totals=totals,
            averages=averages,
            top_videos=[p.title for p in ranked[:3]],
            weak_videos=[p.title for p in ranked[-3:]][::-1] if len(ranked) > 3 else [],
            suggestions=self.suggestions(perfs),
        )
        return report

    def write_report(self, report: AnalyticsReport, name: str | None = None) -> dict[str, Path]:
        out_dir = Path(self.config.report_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = name or f"report-{report.period}-{datetime.now():%Y%m%d}"
        return {
            "md": atomic_write_text(out_dir / f"{stem}.md", report.to_markdown()),
            "json": atomic_write_json(out_dir / f"{stem}.json", report.to_dict()),
        }


def _fmt(v: float) -> str:
    if isinstance(v, float) and v < 1:
        return f"{v * 100:.1f}%"
    return f"{v:,.0f}" if float(v).is_integer() else f"{v:,.2f}"

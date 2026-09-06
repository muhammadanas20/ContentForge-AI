# Analytics

ContentForge stores per-video, per-platform metrics and turns them into a score and concrete suggestions.
Metrics are **entered manually** (dashboard form or CLI) or **imported from CSV** exported from Instagram/YouTube
insights - the project does not scrape platforms.

## Recording metrics

```bash
contentforge analytics add <job_id> --platform instagram --views 4200 --likes 310 --comments 22 \
    --shares 48 --saves 90 --completion 61 --avg-watch 18.5 --followers 35 --notes "posted 7pm"
contentforge analytics import metrics.csv   # header: job_id,platform,views,likes,comments,shares,saves,completion_rate,follower_growth
```
`completion` accepts 0-1 or a percentage. Each call adds a new row; reports use the latest row per (video, platform),
so you can re-enter numbers as they grow.

## Score (0-100)

| Component | Weight | Saturates at |
|---|---|---|
| Completion rate | 40 % | `benchmarks.completion_rate_good` (0.55) |
| Like rate (likes/views) | 20 % | `like_rate_good` (0.06) |
| Share rate | 15 % | `share_rate_good` (0.01) |
| Comment rate | 10 % | `comment_rate_good` (0.005) |
| Reach | 15 % | √(views/10 000) |

## Suggestions

Generated from averages against the benchmarks, e.g.:

* low completion → shorten, front-load the result, tighten silence cuts;
* low like rate → clearer payoff, highlight benefit keywords;
* low shares → "share with a classmate" CTA, pick deadline-driven problems;
* few comments → one-word comment prompts;
* views without follower growth → consistent topics + follow CTA card;
* best vs weakest video comparison once ≥ 3 videos have data.

## Reports

```bash
contentforge analytics report --period weekly
```
Written to `data/output/reports/` as Markdown + JSON; the scheduler produces weekly and monthly ones automatically.

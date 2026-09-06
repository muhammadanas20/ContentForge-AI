# Scheduling

## Built-in scheduler (APScheduler)

`contentforge run` starts a background scheduler alongside the folder watcher:

| Job | Trigger (config key) | Default | What it does |
|---|---|---|---|
| `process_queue` | `scheduler.process_cron` | `0 */2 * * *` | Only in `mode: scheduled`; processes files collected by the watcher |
| `daily_cleanup` | `scheduler.daily_cleanup_cron` | `30 3 * * *` | `Cleaner.run()` |
| `weekly_analytics` | `scheduler.weekly_analytics_cron` | `0 9 * * 1` | Writes `output/reports/weekly-<date>.md/.json` |
| `monthly_report` | `scheduler.monthly_report_cron` | `0 9 1 * *` | Writes `output/reports/monthly-<month>.md/.json` |
| `health` | every 5 min | — | Disk check (runs cleanup when below `min_free_disk_gb`), queue size, job counts |

Crons use `project.timezone`. Missed runs (laptop asleep) are coalesced and executed once when the daemon is back
(`misfire_grace_time` 1 h).

### Immediate vs scheduled

* `immediate` (default): each recording is processed as soon as its size is stable.
* `scheduled`: recordings are queued in memory and processed at `process_cron`. Useful to keep the laptop responsive
  during the day and render overnight. Files still in `input/` are also picked up at daemon start.

## systemd (recommended for always-on)

```bash
scripts/install_service.sh            # user units: contentforge.service, contentforge-dashboard.service
systemctl --user restart contentforge
journalctl --user -u contentforge -f
```

## System cron alternative

If you prefer not to run a daemon, `scripts/cron.example` contains equivalent entries (process input every 2 h,
daily cleanup, weekly/monthly reports, nightly backup). Set `watcher.enabled: false` and `scheduler.enabled: false`
in that case.

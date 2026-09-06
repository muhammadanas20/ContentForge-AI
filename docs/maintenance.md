# Maintenance: updating, backup, restore, cleanup

## Updating

```bash
cd ContentForge-AI
git pull
source .venv/bin/activate
pip install -r requirements.txt --upgrade
pip install -e .
contentforge doctor
pytest -q                       # optional sanity check
systemctl --user restart contentforge contentforge-dashboard   # if installed as services
```
Config changes are additive: new keys get defaults from the schema; removed keys will raise a validation error
pointing at the offending line. Database schema changes are applied automatically (`CREATE TABLE IF NOT EXISTS`).

## Backup

`scripts/backup.sh [dest]` creates `contentforge-backup-<timestamp>.tar.gz` containing:

* `config/` and `.env`
* `data/db/` (SQLite checkpointed first)
* `data/output/` (upload packages + reports)
* `data/archive/` (raw recordings)
* `data/assets/` (logo, music)

Excluded: `data/work` (temporary), `data/models` (re-downloadable), `data/logs`.
Schedule nightly with the cron line in `scripts/cron.example`.

## Restore

```bash
scripts/restore.sh backups/contentforge-backup-20260906-020000.tar.gz
contentforge doctor && contentforge jobs
```
Restoring onto a new machine: install normally first (installation.md), then restore, then let models re-download.

## Cleanup policy

Automatic (daily cron + when disk is low) and manual (`contentforge cleanup [--dry-run]`, dashboard **Storage**):

1. Work dirs of completed/archived jobs → deleted (`delete_temp_after_success`).
2. Work dirs of failed jobs → kept until older than `archive_retention_days` (so you can retry).
3. Archives older than `archive_retention_days` → deleted; empty month folders removed.
4. Outputs older than `output_retention_days` (0 = never).
5. Raw inputs of finished jobs (only if `delete_raw_after_success: true`).
6. If free space < `min_free_disk_gb`: oldest archives, then oldest outputs, until satisfied.
7. Logs older than `logs_retention_days`.

Guards: nothing modified in the last `min_age_minutes`; nothing belonging to `processing`/`queued` jobs; deletions are
restricted to the configured data folders (`safe_rmtree`).

### Low-disk design inside a job (v0.2)

* `pipeline.min_free_disk_gb_to_start` (1 GB): the probe step refuses to start a job below this floor with a clear
  error; retry later with `contentforge retry <id>`.
* `pipeline.delete_intermediates_early` (on): `cut.mp4` is deleted as soon as `final.mp4` has been rendered and
  probed; `synced.mp4` after the thumbnail frame grab. Peak scratch usage drops from ~3× to ~1.5× the final size.
  A later `retry --from render_final` re-renders only what is missing.
* `cleanup_work` verifies the package first: output directory + `manifest.json` exist and the packaged MP4 has exactly
  the rendered size. Otherwise the step fails and the work directory is kept.
* Nothing in the pipeline ever deletes a package; only the retention rules above do, and never for jobs that are
  `processing`/`queued`.

See `docs/fedora-real-system-test.md` §8 for a complete low-disk `config.local.yaml`.

## Database maintenance

```bash
sqlite3 data/db/contentforge.sqlite3 "PRAGMA wal_checkpoint(TRUNCATE); VACUUM;"
contentforge jobs --status failed        # review, then retry or mark cancelled in the dashboard
```

## Rotating logs

Dated files are pruned automatically at start-up and by cleanup (`logging.retention_days`). Console logging can be
disabled with `-q` for services (journald captures stdout anyway).

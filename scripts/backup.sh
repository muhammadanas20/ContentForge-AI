#!/usr/bin/env bash
# Backup config, database, outputs and archives into a dated tarball.
# Usage: scripts/backup.sh [destination_dir]   (default: ./backups)
set -euo pipefail
cd "$(dirname "$0")/.."
DEST="${1:-backups}"
mkdir -p "$DEST"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$DEST/contentforge-backup-$STAMP.tar.gz"
# Checkpoint SQLite WAL so the copy is consistent
if [ -f data/db/contentforge.sqlite3 ]; then
  sqlite3 data/db/contentforge.sqlite3 "PRAGMA wal_checkpoint(TRUNCATE);" 2>/dev/null || true
fi
tar -czf "$OUT" \
  --exclude='data/work/*' --exclude='data/models/*' --exclude='data/logs/*' \
  config .env data/db data/output data/archive data/assets 2>/dev/null || true
echo "Backup written: $OUT ($(du -h "$OUT" | cut -f1))"

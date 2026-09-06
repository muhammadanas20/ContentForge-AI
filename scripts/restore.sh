#!/usr/bin/env bash
# Restore a backup created by scripts/backup.sh
# Usage: scripts/restore.sh path/to/contentforge-backup-XXXX.tar.gz
set -euo pipefail
cd "$(dirname "$0")/.."
[ -n "${1:-}" ] || { echo "usage: $0 backup.tar.gz"; exit 1; }
echo "This will overwrite config/, .env, data/db, data/output, data/archive, data/assets. Continue? [y/N]"
read -r ans; [ "$ans" = "y" ] || exit 1
systemctl --user stop contentforge contentforge-dashboard 2>/dev/null || true
tar -xzf "$1"
echo "Restored. Run: contentforge doctor && contentforge jobs"

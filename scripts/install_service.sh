#!/usr/bin/env bash
# Install ContentForge-AI as user-level systemd services (watcher + dashboard).
set -euo pipefail
cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"

sed "s#__REPO__#$REPO_ROOT#g" scripts/systemd/contentforge.service > "$UNIT_DIR/contentforge.service"
sed "s#__REPO__#$REPO_ROOT#g" scripts/systemd/contentforge-dashboard.service > "$UNIT_DIR/contentforge-dashboard.service"

systemctl --user daemon-reload
systemctl --user enable --now contentforge.service contentforge-dashboard.service
loginctl enable-linger "$USER" || true   # keep running after logout

echo "Installed. Check with: systemctl --user status contentforge contentforge-dashboard"
echo "Logs:               journalctl --user -u contentforge -f"

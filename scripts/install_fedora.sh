#!/usr/bin/env bash
# One-shot installer for Fedora (tested on Fedora 39-42).
# Installs system deps (ffmpeg via RPM Fusion, fonts, python), creates a venv and
# installs ContentForge-AI with all optional extras.
set -euo pipefail

cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"

echo "==> Enabling RPM Fusion (needed for a full ffmpeg build)"
if ! rpm -q rpmfusion-free-release >/dev/null 2>&1; then
  sudo dnf install -y "https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm"
fi

echo "==> Installing system packages"
sudo dnf install -y --allowerasing ffmpeg python3 python3-pip python3-devel gcc \
  dejavu-sans-fonts dejavu-sans-mono-fonts liberation-sans-fonts fontconfig inotify-tools

echo "==> Creating virtual environment"
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --upgrade pip wheel

echo "==> Installing ContentForge-AI"
pip install -r requirements.txt
pip install -e ".[dev]"

echo "==> Preparing data folders and .env"
mkdir -p data/{input,output,work,archive,logs,assets,models,db}
[ -f .env ] || cp .env.example .env

echo "==> Environment check"
contentforge doctor || true

cat <<MSG

Done. Next steps:
  source .venv/bin/activate
  contentforge run              # start watching data/input
  contentforge dashboard        # open http://localhost:8501

Optional: install as a service ->  scripts/install_service.sh
MSG

#!/usr/bin/env bash
# Install arxiv-search on a Raspberry Pi (Raspberry Pi OS / Debian, 64-bit).
# Run from the project directory as the user that should own the service:
#   ./deploy/install.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="$(id -un)"
cd "$APP_DIR"

python3 - <<'EOF'
import sys
if sys.version_info < (3, 11):
    sys.exit(f"Python 3.11+ required, found {sys.version.split()[0]}")
EOF

if ! python3 -c "import venv, ensurepip" 2>/dev/null; then
  echo "Installing python3-venv ..."
  sudo apt-get update && sudo apt-get install -y python3-venv
fi

[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install --upgrade pip >/dev/null
.venv/bin/pip install -e .

if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo "Created .env: fill in GEMINI_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID"
fi
mkdir -p data
.venv/bin/arxiv-search init

for unit in arxiv-search-web.service arxiv-search-update.service arxiv-search-update.timer; do
  sed -e "s|@APP_DIR@|$APP_DIR|g" -e "s|@USER@|$USER_NAME|g" "deploy/systemd/$unit" \
    | sudo tee "/etc/systemd/system/$unit" >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --now arxiv-search-web.service arxiv-search-update.timer

echo
echo "Installed."
echo "  UI:       http://$(hostname).local:$(grep -E '^WEB_PORT=' .env | cut -d= -f2 || echo 8000)"
echo "  Timer:    systemctl list-timers arxiv-search-update.timer"
echo "  Run now:  sudo systemctl start --no-block arxiv-search-update.service && journalctl -fu arxiv-search-update"

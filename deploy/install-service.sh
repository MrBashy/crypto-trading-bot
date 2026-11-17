#!/usr/bin/env bash
# Installs the bot as a systemd *user* service so it starts on boot and restarts if it crashes.
# Run from the project folder:   ./deploy/install-service.sh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${BOT_PORT:-8080}"
HOST="${BOT_HOST:-0.0.0.0}"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/crypto-bot.service"

cd "$PROJECT_DIR"

if [ ! -x .venv/bin/python ]; then
  echo "Creating virtual environment and installing dependencies..."
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip >/dev/null
  .venv/bin/pip install -r requirements.txt
fi

if ! .venv/bin/python -c "from bot import secrets_store; raise SystemExit(0 if secrets_store.password_is_set() else 1)"; then
  echo "No dashboard password yet - let's set one."
  .venv/bin/python -m bot.cli set-password
fi

mkdir -p "$UNIT_DIR"
cat > "$UNIT" <<EOF
[Unit]
Description=Crypto trading bot + dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$PROJECT_DIR
Environment=BOT_HOST=$HOST
Environment=BOT_PORT=$PORT
Environment=PYTHONUNBUFFERED=1
ExecStart=$PROJECT_DIR/.venv/bin/python run.py
Restart=always
RestartSec=10
# Give open orders time to finish when stopping
TimeoutStopSec=40

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now crypto-bot.service
# Keep user services running after logout and start them at boot.
loginctl enable-linger "$USER" 2>/dev/null || echo "Note: run 'sudo loginctl enable-linger $USER' so the bot starts at boot."

sleep 3
systemctl --user --no-pager status crypto-bot.service | head -5
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "Dashboard: http://${IP:-localhost}:$PORT"
echo "Logs:      journalctl --user -u crypto-bot -f"

#!/usr/bin/env bash
# התקנת טיימר systemd (ברמת משתמש) שמריץ את monitor.py כל 10 דקות.
# שימוש: ./install.sh [שם-נושא-ntfy]
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$(command -v python3)"
CONF_DIR="$HOME/.config/ps5-monitor"
UNIT_DIR="$HOME/.config/systemd/user"

topic="${1:-}"
if [ -z "$topic" ] && [ ! -f "$CONF_DIR/env" ]; then
  read -r -p "שם הנושא ב-ntfy: " topic
fi
if [ -n "$topic" ]; then
  if ! [[ "$topic" =~ ^[A-Za-z0-9_-]{1,64}$ ]]; then
    echo "שם נושא לא תקין: רק אותיות אנגליות, ספרות, - ו-_ (עד 64 תווים)" >&2
    exit 1
  fi
  mkdir -p "$CONF_DIR"
  umask 077
  printf 'NTFY_TOPIC=%s\n' "$topic" > "$CONF_DIR/env"
  chmod 600 "$CONF_DIR/env"
  echo "נשמר $CONF_DIR/env"
fi

mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/ps5-monitor.service" <<UNIT
[Unit]
Description=PS5 stock monitor

[Service]
Type=oneshot
EnvironmentFile=$CONF_DIR/env
ExecStart=$PY $REPO/monitor.py
TimeoutStartSec=400
UNIT

cat > "$UNIT_DIR/ps5-monitor.timer" <<UNIT
[Unit]
Description=Run PS5 stock monitor every 10 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=10min
AccuracySec=30s

[Install]
WantedBy=timers.target
UNIT

systemctl --user daemon-reload
systemctl --user enable --now ps5-monitor.timer
echo
systemctl --user list-timers ps5-monitor.timer --no-pager || true
echo
echo "כדי שהטיימר ירוץ גם כשאין משתמש מחובר, הרץ פעם אחת:"
echo "  sudo loginctl enable-linger $USER"

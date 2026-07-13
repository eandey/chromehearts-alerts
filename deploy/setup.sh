#!/usr/bin/env bash
# One-time server setup for the Chrome Hearts appointment monitor.
#
# Run as root ON THE SERVER, from a directory containing monitor.py,
# requirements.txt, setup.sh, and the chromehearts-*.{service,timer}
# unit files (deploy.sh copies all of these up for you):
#
#   NTFY_TOPIC=your-topic bash setup.sh
#
# Safe to re-run; it updates code and units in place. Migrates from
# the old timer-based (v7, Playwright) install automatically.
set -euo pipefail

APP_DIR=/opt/chromehearts
ENV_FILE=/etc/chromehearts-monitor.env

if [[ $EUID -ne 0 ]]; then
    echo "This script must run as root." >&2
    exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3-venv curl

id -u chmonitor &>/dev/null || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin chmonitor

install -d "$APP_DIR" "$APP_DIR/run"
install -m 644 monitor.py resy_monitor.py requirements.txt "$APP_DIR/"

if [[ ! -d "$APP_DIR/venv" ]]; then
    python3 -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
chown -R chmonitor:chmonitor "$APP_DIR"

if [[ ! -f "$ENV_FILE" ]]; then
    cat > "$ENV_FILE" <<EOF
NTFY_TOPIC=${NTFY_TOPIC:-}
POLL_SECONDS=5
EOF
    chmod 600 "$ENV_FILE"
elif [[ -n "${NTFY_TOPIC:-}" ]]; then
    sed -i "s|^NTFY_TOPIC=.*|NTFY_TOPIC=${NTFY_TOPIC}|" "$ENV_FILE"
fi

if ! grep -q '^NTFY_TOPIC=.\+' "$ENV_FILE"; then
    echo "WARNING: NTFY_TOPIC is empty in $ENV_FILE — checks will run but no alerts will be sent." >&2
fi

# Migrate from the v7 timer-based install: the monitor is a continuous
# service now, and Playwright/Chromium are no longer needed.
if systemctl list-unit-files chromehearts-monitor.timer &>/dev/null; then
    systemctl disable --now chromehearts-monitor.timer 2>/dev/null || true
fi
rm -f /etc/systemd/system/chromehearts-monitor.timer
"$APP_DIR/venv/bin/pip" uninstall --quiet --yes playwright 2>/dev/null || true
rm -rf "$APP_DIR/browsers"

install -m 644 chromehearts-monitor.service resy-monitor.service \
    chromehearts-heartbeat.service chromehearts-heartbeat.timer /etc/systemd/system/

systemctl daemon-reload
# Chrome Hearts alerts are switched off (user request, 2026-07-13).
# To turn them back on:  systemctl enable --now chromehearts-monitor.service
systemctl enable resy-monitor.service chromehearts-heartbeat.timer
systemctl restart resy-monitor.service
if systemctl is-enabled --quiet chromehearts-monitor.service; then
    systemctl restart chromehearts-monitor.service
fi
systemctl start chromehearts-heartbeat.timer

echo
echo "Waiting a few seconds, then showing watcher logs..."
sleep 5
journalctl -u resy-monitor.service -n 10 --no-pager

echo
echo "Done. Both watchers poll continuously. Useful commands:"
echo "  systemctl status chromehearts-monitor resy-monitor   # daemon state"
echo "  journalctl -u chromehearts-monitor -f                # live logs (Chrome Hearts)"
echo "  journalctl -u resy-monitor -f                        # live logs (Golden Diner)"
echo "  cat $APP_DIR/run/found.json $APP_DIR/run/resy_found.json  # last availability seen"

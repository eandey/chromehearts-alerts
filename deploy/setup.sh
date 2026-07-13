#!/usr/bin/env bash
# One-time server setup for the Chrome Hearts appointment monitor.
#
# Run as root ON THE SERVER, from a directory containing monitor.py,
# requirements.txt, setup.sh, and the four chromehearts-*.{service,timer}
# unit files (deploy.sh copies all of these up for you):
#
#   NTFY_TOPIC=your-topic bash setup.sh
#
# Safe to re-run; it updates code and units in place.
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
install -m 644 monitor.py requirements.txt "$APP_DIR/"

if [[ ! -d "$APP_DIR/venv" ]]; then
    python3 -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
PLAYWRIGHT_BROWSERS_PATH="$APP_DIR/browsers" "$APP_DIR/venv/bin/playwright" install --with-deps chromium
chown -R chmonitor:chmonitor "$APP_DIR"

if [[ ! -f "$ENV_FILE" ]]; then
    cat > "$ENV_FILE" <<EOF
NTFY_TOPIC=${NTFY_TOPIC:-}
CHECKS_PER_RUN=1
SLEEP_SECONDS=0
EOF
    chmod 600 "$ENV_FILE"
elif [[ -n "${NTFY_TOPIC:-}" ]]; then
    sed -i "s|^NTFY_TOPIC=.*|NTFY_TOPIC=${NTFY_TOPIC}|" "$ENV_FILE"
fi

if ! grep -q '^NTFY_TOPIC=.\+' "$ENV_FILE"; then
    echo "WARNING: NTFY_TOPIC is empty in $ENV_FILE — checks will run but no alerts will be sent." >&2
fi

install -m 644 chromehearts-monitor.service chromehearts-monitor.timer \
    chromehearts-heartbeat.service chromehearts-heartbeat.timer /etc/systemd/system/

systemctl daemon-reload
systemctl enable --now chromehearts-monitor.timer chromehearts-heartbeat.timer

echo
echo "Running one check now to verify..."
systemctl start chromehearts-monitor.service
journalctl -u chromehearts-monitor.service -n 25 --no-pager

echo
echo "Done. The monitor runs every minute. Useful commands:"
echo "  systemctl list-timers chromehearts-*        # next scheduled runs"
echo "  journalctl -u chromehearts-monitor -f       # live logs"
echo "  cat $APP_DIR/run/found.json                 # last check result"

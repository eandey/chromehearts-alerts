#!/usr/bin/env bash
# Deploy the monitor to a server from your local machine.
#
#   NTFY_TOPIC=your-topic ./deploy/deploy.sh root@46.224.129.206
#
# NTFY_TOPIC is only needed the first time (or to change the topic);
# after that, plain ./deploy/deploy.sh root@<ip> re-deploys the code.
set -euo pipefail

HOST=${1:?usage: [NTFY_TOPIC=...] deploy.sh user@host}
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
STAGE=/tmp/chromehearts-deploy

ssh "$HOST" "mkdir -p $STAGE"
scp "$REPO_DIR/monitor.py" "$REPO_DIR/resy_monitor.py" "$REPO_DIR/requirements.txt" \
    "$REPO_DIR"/deploy/setup.sh \
    "$REPO_DIR"/deploy/chromehearts-monitor.service \
    "$REPO_DIR"/deploy/resy-monitor.service \
    "$REPO_DIR"/deploy/chromehearts-heartbeat.service \
    "$REPO_DIR"/deploy/chromehearts-heartbeat.timer \
    "$HOST:$STAGE/"
ssh "$HOST" "cd $STAGE && NTFY_TOPIC='${NTFY_TOPIC:-}' bash setup.sh"

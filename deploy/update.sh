#!/usr/bin/env bash
# Pull the latest commit from origin/main and restart the dashboard service.
# Run as root (or via sudo) on the deployed VPS.

set -euo pipefail

INSTALL_DIR=/opt/markets-brief
SERVICE_USER=markets-brief

if [[ "$EUID" -ne 0 ]]; then
    echo "update.sh must run as root (try: sudo bash $0)"
    exit 1
fi

echo "==> Pulling latest from origin/main"
sudo -u "$SERVICE_USER" git -C "$INSTALL_DIR" fetch origin
sudo -u "$SERVICE_USER" git -C "$INSTALL_DIR" reset --hard origin/main

echo "==> Reinstalling Python deps in case pyproject.toml changed"
sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip
sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install --quiet -e "$INSTALL_DIR"

# Reinstall systemd units in case they changed.
install -m 644 "$INSTALL_DIR/deploy/markets-brief.service" /etc/systemd/system/
install -m 644 "$INSTALL_DIR/deploy/markets-email.service" /etc/systemd/system/
install -m 644 "$INSTALL_DIR/deploy/markets-email.timer"   /etc/systemd/system/
install -m 644 "$INSTALL_DIR/deploy/Caddyfile"             /etc/caddy/Caddyfile

systemctl daemon-reload
systemctl restart markets-brief.service
systemctl reload caddy || systemctl restart caddy

echo "==> Update complete. Tail logs:  sudo journalctl -u markets-brief -f"

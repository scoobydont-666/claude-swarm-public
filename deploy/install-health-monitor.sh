#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="swarm-health-monitor"
SERVICE_FILE="$(dirname "$(realpath "$0")")/${SERVICE_NAME}.service"
SYSTEMD_DIR="/etc/systemd/system"

if [[ ! -f "$SERVICE_FILE" ]]; then
    echo "ERROR: service file not found: $SERVICE_FILE" >&2
    exit 1
fi

echo "Installing ${SERVICE_NAME}.service ..."
cp "$SERVICE_FILE" "${SYSTEMD_DIR}/${SERVICE_NAME}.service"
chmod 644 "${SYSTEMD_DIR}/${SERVICE_NAME}.service"

echo "Reloading systemd daemon ..."
systemctl daemon-reload

echo "Enabling ${SERVICE_NAME} ..."
systemctl enable "${SERVICE_NAME}"

echo "Starting ${SERVICE_NAME} ..."
systemctl start "${SERVICE_NAME}"

echo ""
systemctl status "${SERVICE_NAME}" --no-pager

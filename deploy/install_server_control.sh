#!/usr/bin/env bash
set -Eeuo pipefail
RELEASE="${1:-/opt/odoo-mcp/current}"
install -d -o odoo-mcp -g odoo-mcp -m 2770 /var/lib/odoo-mcp/control /var/lib/odoo-mcp/control/jobs
install -o root -g root -m 0750 "$RELEASE/deploy/server_control.py" /opt/odoo-mcp/deploy/server_control.py
install -o root -g root -m 0644 "$RELEASE/deploy/odoo-mcp-control.path" /etc/systemd/system/odoo-mcp-control.path
install -o root -g root -m 0644 "$RELEASE/deploy/odoo-mcp-control.service" /etc/systemd/system/odoo-mcp-control.service
systemctl daemon-reload
systemctl enable --now odoo-mcp-control.path
install -o root -g odoo-mcp -m 0640 /dev/null /var/lib/odoo-mcp/control/enabled

#!/usr/bin/env bash
set -Eeuo pipefail
RELEASE="${1:-/opt/odoo-mcp/current}"
install -o root -g root -m 0750 "$RELEASE/deploy/purge_expired_workspaces.py" /opt/odoo-mcp/deploy/purge_expired_workspaces.py
for unit in odoo-mcp-retention.service odoo-mcp-retention.timer odoo-mcp-log-retention.service odoo-mcp-log-retention.timer; do
  install -o root -g root -m 0644 "$RELEASE/deploy/$unit" "/etc/systemd/system/$unit"
done
install -d -o root -g root -m 0755 /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/odoo-mcp-retention.conf <<'CONFIG'
[Journal]
MaxRetentionSec=2day
MaxFileSec=1h
CONFIG
cat > /etc/logrotate.d/odoo-mcp <<'CONFIG'
/var/log/odoo-mcp/*.log {
    hourly
    rotate 47
    maxage 2
    missingok
    notifempty
    compress
    delaycompress
    copytruncate
    su root root
}
CONFIG
systemctl daemon-reload
systemctl restart systemd-journald.service
systemctl enable --now odoo-mcp-retention.timer odoo-mcp-log-retention.timer
systemctl start odoo-mcp-retention.service odoo-mcp-log-retention.service

#!/usr/bin/env bash
set -Eeuo pipefail
release="$(cd "$(dirname "$0")/.." && pwd)"
install -d -o root -g root -m 0755 /usr/local/libexec/odoo-mcp
install -d -o root -g root -m 0700 /etc/odoo-mcp/release-approvals
install -o root -g root -m 0755 "$release/deploy/deploy.sh" /usr/local/libexec/odoo-mcp/deploy.sh
install -o root -g root -m 0644 "$release/deploy/release_gate.py" /usr/local/libexec/odoo-mcp/release_gate.py
install -o root -g root -m 0644 "$release/deploy/readiness_monitor.py" /usr/local/libexec/odoo-mcp/readiness_monitor.py
install -d -m 0755 /etc/systemd/system/odoo-mcp-deploy@.service.d
cat > /etc/systemd/system/odoo-mcp-deploy@.service.d/sdlc.conf <<'UNIT'
[Service]
ExecStart=
ExecStart=/usr/bin/env DEPLOY_REQUIRE_RELEASE_APPROVAL=1 /usr/local/libexec/odoo-mcp/deploy.sh %i
UNIT
for unit in odoo-mcp-readiness-monitor.service odoo-mcp-readiness-monitor.timer; do
  install -o root -g root -m 0644 "$release/deploy/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable --now odoo-mcp-readiness-monitor.timer

#!/usr/bin/env bash
# Run from the deployed release after reviewing docs/EXECUTION_WORKER.md.
set -Eeuo pipefail
[[ "$(id -u)" == 0 ]] || { echo "Run as root" >&2; exit 2; }
RELEASE="$(cd "$(dirname "$0")/.." && pwd)"
command -v docker >/dev/null
id odoo-executor >/dev/null 2>&1 || useradd --system --gid odoo-mcp --groups docker --shell /usr/sbin/nologin odoo-executor
usermod -aG docker odoo-executor
install -d -o odoo-mcp -g odoo-mcp -m 2770 /var/lib/odoo-mcp/execution
docker build -f "$RELEASE/deploy/Dockerfile.execution" -t odoo-mcp-execution:local "$RELEASE"
install -m 644 "$RELEASE/deploy/odoo-mcp-execution.service" /etc/systemd/system/odoo-mcp-execution.service
install -d -m 755 /etc/systemd/system/odoo-mcp.service.d
printf '[Service]\nUMask=0007\n' > /etc/systemd/system/odoo-mcp.service.d/execution.conf
python3 - <<'PY'
from pathlib import Path
p = Path('/etc/odoo-mcp/odoo-mcp.env')
lines = p.read_text().splitlines()
key = 'ODOO_AGENT_EXECUTION_ENABLED='
lines = [line for line in lines if not line.startswith(key)]
lines.append(key + 'true')
p.write_text('\n'.join(lines) + '\n')
PY
systemctl daemon-reload
systemctl restart odoo-mcp.service
systemctl enable --now odoo-mcp-execution.service
echo 'ChatGPT-controlled MCP execution enabled; no backend model key is required.'

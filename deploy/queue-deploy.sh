#!/usr/bin/env bash
set -euo pipefail

# This is the only privileged entry point exposed to the webhook account.
# Validate one opaque commit identifier, then invoke one fixed systemd unit.
if [[ "$#" -ne 1 || ! "$1" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Expected exactly one 40-character lowercase commit SHA" >&2
  exit 2
fi

exec /usr/bin/systemctl start "odoo-mcp-deploy@$1.service"

#!/usr/bin/env python3
"""Print a one-use Client Manager sign-in link using the running API's config.

Run as root with the deployed virtualenv's Python. The link expires in ten
minutes. Its fragment is exchanged once; the permanent API token never leaves
this process. No service restart or client change is performed.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import pwd
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description='Create a one-use Client Manager sign-in link.')
    parser.add_argument('--base-url', help='Public connector URL when no public URL is configured in the service.')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('Run this helper with sudo to read the API service configuration.')
    pid = subprocess.check_output(['systemctl', 'show', '-p', 'MainPID', '--value', 'odoo-mcp.service'], text=True).strip()
    if not pid.isdigit() or int(pid) <= 0:
        raise SystemExit('The Odoo MCP API service is not running.')
    process = Path('/proc') / pid
    environment = dict(item.split('=', 1) for item in (process / 'environ').read_text().split('\0') if '=' in item)
    release = (process / 'cwd').resolve()
    user = pwd.getpwnam('odoo-mcp')
    os.environ.update(environment)
    os.environ['HOME'] = user.pw_dir
    os.chdir(release)
    sys.path.insert(0, str(release))
    os.setgroups([])
    os.setgid(user.pw_gid)
    os.setuid(user.pw_uid)
    from odoo_agent_server.config import settings
    from odoo_agent_server.routers.client_admin import issue_login_code
    base = args.base_url or settings.public_base_url or settings.oauth_issuer or 'http://127.0.0.1:8080'
    print(base.rstrip('/') + '/clients#login=' + issue_login_code())


if __name__ == '__main__':
    main()

# Client manager

Open `/clients` on the connector's public URL. Select an existing client, edit
its repository, branch, Odoo connection or SSH settings, then choose **Save &
Validate**. **Save only** applies settings without starting a connection check.
The page shows validation progress and the final coding/deployment readiness.
Validation uses the existing background client-check workflow; saving itself
never edits a checkout, pushes code or upgrades an Odoo module.

Use the existing connector admin token to sign in. For a one-click sign-in,
run this operator helper on Oracle:

```sh
sudo /opt/odoo-mcp/venv/bin/python /opt/odoo-mcp/current/deploy/client_manager_link.py
```

If the service has no public base URL configured, add
`--base-url https://YOUR-CONNECTOR-HOST` to that command.

The helper prints a one-use link valid for ten minutes. The link exchanges its
fragment for a six-hour browser session; it does not expose the permanent API
token. Logout removes the browser cookie. Cookie authentication is admin-only,
HTTP-only and same-site, with CSRF protection on changes. Existing MCP OAuth
connections and permissions are unchanged.

Passwords and access tokens are never returned by the settings API. Blank secret
inputs keep their current values. The form supports existing GitHub and GitLab
clients. Saving stale revisions or changing clients with active jobs/commands is
rejected so operators can reload or wait for current work to finish.

Changes are stored atomically in `client_settings.json` under
`ODOO_AGENT_STATE_DIR`, readable only by the service owner/group. API and worker
read this shared override file over the `ODOO_CLIENTS` environment configuration;
no root-owned environment file write or restart is needed. Both services should
load the same optional `/etc/odoo-mcp/clients.env` base configuration (included in
the execution worker unit). Saved fields take
precedence over later environment-file edits. Use the UI for those fields, or
restore/remove the corresponding saved override during operator recovery.
Back up this file with the rest of the connector state; it can contain updated
credentials. Unreadable overrides fail closed instead of silently reverting to
old environment settings. No SQLite database or frontend build service is used.

The manager updates existing client entries. Adding/removing/renaming clients is
outside this minimal version.

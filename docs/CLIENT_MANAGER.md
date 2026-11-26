# Client manager

Open `/clients` on the connector's public URL. The selector loads real `ODOO_CLIENTS` entries plus clients saved through the UI. There are no seeded demo clients. Select an existing client, edit
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

Use **+ Add client** to create a client. A separate checkout path is generated
from its name (editable in Advanced settings). Environment clients are preserved;
new clients become visible immediately to both the manager and MCP resolver.
No existing client's repository token or password is copied to a new client.
A common, already configured Oracle SSH key path may be suggested. Removing and
renaming clients remain outside this version.

## Oracle update and restart

Install the narrow, root-owned controller once after deploying this release:

```sh
sudo bash /opt/odoo-mcp/current/deploy/install_server_control.sh
```

The header's **Oracle services** menu offers **Update & restart** and **Restart
services**. Update uses the existing master deployment unit, including its full
test/readiness/rollback gates. Restart targets only the MCP API and execution
worker. Neither action can run an arbitrary command or reboot the Oracle VM.
An admin session and CSRF token are required. Active connector work blocks the
action; new MCP mutations wait while maintenance is pending. The browser polls a
persisted action record and reconnects through the short API restart.

The API only queues fixed actions under `/var/lib/odoo-mcp/control`. A systemd
path unit runs `/opt/odoo-mcp/deploy/server_control.py`, installed root-owned.
The API gains no sudo rights or writable root script. Saving client settings
still applies immediately and does not require a restart.

## Runtime, storage and service menus

The control center includes **Clients**, **Runtime & performance**, **Storage &
retention**, and **Oracle services**. Client edits continue to apply immediately.
Server settings show the real effective values, environment variable names,
validated ranges, and whether a value comes from the environment or a saved
UI override. Runtime controls cover worker/queue capacity, watchdog timing and
cache lifetimes; storage controls cover session/workspace retention, job history,
artifacts and command logs.

**Save settings** stages allowlisted, non-secret values atomically in
`runtime_settings.json` under `ODOO_AGENT_STATE_DIR`. Both API and execution worker
load and validate that file on startup. **Oracle services → Restart services**
applies the staged values. Until then, the screen displays **Restart pending**
and retains the current running values. Saving checks revision conflicts and
blocks changes during active work or maintenance. Draft server settings survive
switching between menus; leaving the page warns about unsaved edits.

Saved UI overrides take precedence over environment values for these fields.
Back up `runtime_settings.json` alongside `client_settings.json`. For operator
recovery, restore or remove the runtime override file and restart both services.
Malformed files fail startup rather than silently running unexpected settings.
Authentication, permission flags, execution images, filesystem roots and secrets
remain operator-managed; the environment overview reports only selected safe
configuration values and configured/not-configured flags.

## Tasks, diagnostics, deployments and client knowledge

**Tasks** shows durable task contracts, pinned client/branch, verification claims,
linked workspaces, commits and the bounded event timeline. **Reconcile existing
work** polls existing workspace/jobs read-only; it cannot push, upgrade or replay
an uncertain write. Legacy execution workspaces remain visible separately.
**Diagnostics** projects existing per-client debug sessions, trusted evidence and
verified-fix gates. **Deployments** displays actual job stages, source deployment,
module upgrade and computed deployment confirmation separately. These views show
persisted observations; Refresh does not run business actions or new deployments.
Use the client filter to narrow the view.

**Clients → Client knowledge** reads current profile/source-bound facts. Refresh
fetches bounded authenticated installed-module metadata and current Git manifest
metadata, with provenance. Source manifests are declarations, not proof of runtime
execution. Cached facts expire after 30 minutes and cannot survive a changed
client profile or source SHA; dirty/unavailable source is explicitly unverified.
Runtime connection failures never invent an Odoo version or installed modules.


Large first checkouts use a bounded 900-second preparation budget by default.
`ODOO_AGENT_CLIENT_CHECKOUT_TIMEOUT_SECONDS` can set 30–1800 seconds. The Git
connection preflight still fails quickly for unreachable hosts; healthy downloads
are allowed more time instead of being cancelled at 180 seconds. A failed clone
never replaces the configured checkout with a partial repository.

Repository credentials and Odoo HTTPS credentials are separate. Successful Git
access or Odoo.sh SSH does not validate the Odoo user's HTTPS login. After a new
staging build, check its database URL/name and update its Odoo login/password as
needed. A rejected HTTPS login is reported separately from coding/deployment
readiness; the connector does not reset passwords or bypass user authentication.

# OCI fast deployment (no Docker, no GitHub Actions)

This setup deploys every push to `master` using a signed GitHub webhook. The
webhook receiver uses only Python's standard library. The application remains
a single Uvicorn worker because its current JSON file store and background
workers are not designed for multi-process writes.

## Runtime layout

- `/opt/odoo-mcp/repository.git`: local bare mirror, fetched incrementally.
- `/opt/odoo-mcp/releases/<commit-sha>`: immutable checked-out releases.
- `/opt/odoo-mcp/venvs/<requirements-sha>`: shared dependency environments.
- `/opt/odoo-mcp/current`: atomically switched active release.
- `/etc/odoo-mcp/odoo-mcp.env`: application secrets and ODOO_CLIENTS.
- `/etc/odoo-mcp/webhook.secret`: GitHub webhook HMAC secret.
- `/var/lib/odoo-mcp`: persistent application state.
- `/var/log/odoo-mcp`: deployment and service logs.

## One-time OCI setup

Run as an administrator on an Ubuntu OCI VM. Configure DNS and HTTPS in Caddy
or another reverse proxy before adding the GitHub webhook.

1. Create the service account and directories:

```bash
sudo useradd --system --home /opt/odoo-mcp --shell /usr/sbin/nologin odoo-mcp
sudo mkdir -p /opt/odoo-mcp/{releases,venvs,cache,deploy} /etc/odoo-mcp /var/lib/odoo-mcp /var/log/odoo-mcp
sudo chown -R odoo-mcp:odoo-mcp /opt/odoo-mcp /var/lib/odoo-mcp /var/log/odoo-mcp
sudo chmod 750 /etc/odoo-mcp
```

2. Install prerequisites:

```bash
sudo apt-get update
sudo apt-get install -y git python3 python3-venv curl
```

3. Clone the repo once and install deployment files:

```bash
sudo -u odoo-mcp git clone --mirror https://github.com/Abdul0000/odoo-mcp-unified.git /opt/odoo-mcp/repository.git
sudo -u odoo-mcp git --git-dir=/opt/odoo-mcp/repository.git fetch origin master
sudo install -m 755 deploy/deploy.sh /opt/odoo-mcp/deploy/deploy.sh
sudo install -m 644 deploy/odoo-mcp.service /etc/systemd/system/odoo-mcp.service
sudo install -m 644 deploy/odoo-mcp-webhook.service /etc/systemd/system/odoo-mcp-webhook.service
```

The first release is installed by the deploy script after the environment file
is created. Install the repo's current deploy scripts at the fixed deploy path;
subsequent releases update `current`, not the fixed deployment control path.

4. Create `/etc/odoo-mcp/odoo-mcp.env` (root-owned, mode 600). At minimum:

```env
ODOO_AGENT_API_TOKEN=replace-with-a-long-random-secret
ODOO_AGENT_STATE_DIR=/var/lib/odoo-mcp/state
ODOO_AGENT_SCREENSHOT_DIR=/var/lib/odoo-mcp/screenshots
ODOO_AGENT_ALLOWED_ROOTS=/workspace/repos,/var/lib/odoo-mcp
ODOO_AGENT_REQUIRE_APPROVAL=true
ODOO_AGENT_PUBLIC_BASE_URL=https://mcp.example.com
# Add ODOO_CLIENTS and all existing Odoo/GitHub credentials here.
```

Do not commit this file or any client credentials to GitHub. Copy existing
production values; do not regenerate client configuration during deployments.

5. Create a unique webhook secret (at least 32 bytes):

```bash
openssl rand -hex 32 | sudo tee /etc/odoo-mcp/webhook.secret >/dev/null
sudo chown odoo-mcp:odoo-mcp /etc/odoo-mcp/webhook.secret
sudo chmod 600 /etc/odoo-mcp/webhook.secret
sudo cp deploy/webhook.env.example /etc/odoo-mcp/webhook.env
sudo chown root:odoo-mcp /etc/odoo-mcp/webhook.env
sudo chmod 640 /etc/odoo-mcp/webhook.env
```

6. Install the current release by invoking deploy.sh with the latest master SHA.
Then enable services:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now odoo-mcp.service odoo-mcp-webhook.service
```

## GitHub webhook

In repository Settings → Webhooks → Add webhook:

- Payload URL: `https://mcp.example.com/github-webhook`
- Content type: `application/json`
- Secret: exact contents of `/etc/odoo-mcp/webhook.secret`
- SSL verification: enabled
- Events: Just the push event

Expose only the webhook route through the reverse proxy to
`http://127.0.0.1:9001/github-webhook`. Keep the API on its own upstream.
Do not expose port 9001 directly to the internet.

Example Caddy route (merge into the existing site block):

```caddy
handle /github-webhook {
    reverse_proxy 127.0.0.1:9001
}
```

## Deployment behavior

- Only pushes to this repository's `master` branch are accepted.
- HMAC signature, event, repository, branch, commit SHA, and delivery ID are checked.
- A local bare Git mirror makes later fetches incremental.
- A release is prepared separately; requirements are hashed and virtualenvs reused.
- Python compile/import checks run before activation.
- The active release pointer is switched atomically and systemd restarts one worker.
- `/health` is checked before success is recorded; failure restores the previous release.
- State, environment, credentials, and client settings are outside release directories.
- The deployment lock prevents concurrent release changes; stale commit payloads are rejected.

## Operations

```bash
sudo journalctl -u odoo-mcp -f
sudo journalctl -u odoo-mcp-webhook -f
sudo tail -f /var/log/odoo-mcp/deploy.log
curl -fsS https://mcp.example.com/health
```

The receiver acknowledges the push quickly with HTTP 202; deployment continues
asynchronously. A failed deployment is visible in the deploy log and leaves
the previous release active.

## External monitoring

Configure the external uptime checker to request `https://mcp.example.com/health`
every 1–5 minutes and alert on non-2xx responses. The deployment receiver has a
separate loopback-only health endpoint at port 9001 for local diagnostics.

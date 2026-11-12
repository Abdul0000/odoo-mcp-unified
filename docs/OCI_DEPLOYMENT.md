# OCI fast deployment (no Docker, no GitHub Actions)

Every push to `master` triggers a signed GitHub webhook. The receiver is
stdlib-only Python. Deployment fetches the latest master SHA, prepares a
versioned release and cached dependency environment, then atomically switches
the active release. The app remains a single Uvicorn worker because its JSON
file store and background jobs are not multi-process safe.

## Runtime layout

- `/opt/odoo-mcp/repository.git`: incremental bare Git mirror.
- `/opt/odoo-mcp/releases/<commit-sha>`: immutable code releases.
- `/opt/odoo-mcp/venvs/<requirements-sha>`: reusable dependency environments.
- `/opt/odoo-mcp/current`: active release symlink.
- `/etc/odoo-mcp/odoo-mcp.env`: app secrets and ODOO_CLIENTS.
- `/etc/odoo-mcp/webhook.secret`: GitHub HMAC secret.
- `/var/lib/odoo-mcp`: persistent app state.
- `/var/log/odoo-mcp`: deployment logs.

## One-time Ubuntu OCI setup

1. Create the service account and directories:

```bash
sudo useradd --system --home /opt/odoo-mcp --shell /usr/sbin/nologin odoo-mcp
sudo mkdir -p /opt/odoo-mcp/{releases,venvs,cache,deploy} /etc/odoo-mcp /var/lib/odoo-mcp /var/log/odoo-mcp
sudo chown -R odoo-mcp:odoo-mcp /opt/odoo-mcp /var/lib/odoo-mcp /var/log/odoo-mcp
sudo chmod 750 /etc/odoo-mcp
sudo apt-get update && sudo apt-get install -y git python3 python3-venv curl
```

2. Bootstrap the deployment controller and unit files from a temporary clone:

```bash
git clone --depth 1 --branch master https://github.com/Abdul0000/odoo-mcp-unified.git /tmp/odoo-mcp-bootstrap
sudo install -m 755 /tmp/odoo-mcp-bootstrap/deploy/deploy.sh /opt/odoo-mcp/deploy/deploy.sh
sudo install -m 644 /tmp/odoo-mcp-bootstrap/deploy/odoo-mcp.service /etc/systemd/system/odoo-mcp.service
sudo install -m 644 /tmp/odoo-mcp-bootstrap/deploy/odoo-mcp-webhook.service /etc/systemd/system/odoo-mcp-webhook.service
sudo install -m 440 /tmp/odoo-mcp-bootstrap/deploy/odoo-mcp-sudoers /etc/sudoers.d/odoo-mcp-deploy
sudo visudo -cf /etc/sudoers.d/odoo-mcp-deploy
```

The deploy controller is installed outside the release symlink so a push
cannot replace the currently executing deployment script mid-run.

3. Create `/etc/odoo-mcp/odoo-mcp.env`, root-owned mode 600. Preserve your
existing production values, especially `ODOO_CLIENTS` and credentials:

```env
ODOO_AGENT_API_TOKEN=replace-with-a-long-random-secret
ODOO_AGENT_STATE_DIR=/var/lib/odoo-mcp/state
ODOO_AGENT_SCREENSHOT_DIR=/var/lib/odoo-mcp/screenshots
ODOO_AGENT_ALLOWED_ROOTS=/workspace/repos,/var/lib/odoo-mcp
ODOO_AGENT_REQUIRE_APPROVAL=true
ODOO_AGENT_PUBLIC_BASE_URL=https://mcp.example.com
# Add your existing ODOO_CLIENTS and other credentials here.
```

Do not commit secrets or this file to GitHub. Systemd reads the root-owned
environment file and passes it to the service on each restart.

4. Create the webhook secret and environment:

```bash
openssl rand -hex 32 | sudo tee /etc/odoo-mcp/webhook.secret >/dev/null
sudo chown odoo-mcp:odoo-mcp /etc/odoo-mcp/webhook.secret
sudo chmod 600 /etc/odoo-mcp/webhook.secret
sudo cp /tmp/odoo-mcp-bootstrap/deploy/webhook.env.example /etc/odoo-mcp/webhook.env
sudo chown root:odoo-mcp /etc/odoo-mcp/webhook.env
sudo chmod 640 /etc/odoo-mcp/webhook.env
```

5. Initialize the bare mirror and deploy the current master SHA:

```bash
sudo -u odoo-mcp git clone --mirror https://github.com/Abdul0000/odoo-mcp-unified.git /opt/odoo-mcp/repository.git
SHA="$(git --git-dir=/opt/odoo-mcp/repository.git rev-parse refs/heads/master)"
sudo -u odoo-mcp /opt/odoo-mcp/deploy/deploy.sh "$SHA"
sudo systemctl daemon-reload
sudo systemctl enable --now odoo-mcp.service odoo-mcp-webhook.service
```

6. Configure DNS and HTTPS in Caddy. Add this route to the existing site:

```caddy
handle /github-webhook {
    reverse_proxy 127.0.0.1:9001
}
```

Do not expose port 9001 directly. The application remains behind the existing
HTTPS reverse proxy on localhost port 8080.

## GitHub webhook

Repository Settings → Webhooks → Add webhook:

- Payload URL: `https://mcp.example.com/github-webhook`
- Content type: `application/json`
- Secret: exact contents of `/etc/odoo-mcp/webhook.secret`
- SSL verification: enabled
- Events: Just the push event

## Fast deployment behavior

- Receiver validates HMAC-SHA256, event, repository, branch, commit and delivery ID.
- Pushes to non-master branches are ignored.
- It responds HTTP 202 immediately and deploys asynchronously.
- A local bare mirror makes later fetches incremental.
- Deployments serialize with `flock`; queued triggers coalesce to latest master.
- If the newest commit is already active, no restart is performed.
- Requirements are hashed; unchanged dependencies reuse their existing venv.
- Candidate release gets Python compile/import checks before activation.
- The current symlink switches atomically, then systemd restarts one worker.
- Health must return JSON status `ok`; otherwise the previous release is restored.
- State, client configuration and secrets are outside release directories.
- Previous releases are retained for rollback.

## Monitoring and logs

```bash
sudo journalctl -u odoo-mcp -f
sudo journalctl -u odoo-mcp-webhook -f
sudo tail -f /var/log/odoo-mcp/deploy.log
curl -fsS https://mcp.example.com/health
```

Configure an external uptime monitor against `https://mcp.example.com/health`
every 1–5 minutes and alert on non-2xx responses.

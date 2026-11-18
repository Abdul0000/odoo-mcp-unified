# OCI fast deployment (no Docker, no GitHub Actions)

Every push to `master` triggers a signed GitHub webhook. The unprivileged
stdlib-only receiver validates the payload and queues a fixed systemd template
unit. A root-owned oneshot deployment unit updates the bare Git mirror, builds
a versioned release and cached dependency environment, atomically switches the
active release, restarts the API and performs a health check.

## Runtime layout

- `/opt/odoo-mcp/repository.git`: incremental bare Git mirror.
- `/opt/odoo-mcp/releases/<commit-sha>`: immutable code releases.
- `/opt/odoo-mcp/venvs/<requirements-sha>`: reusable dependency environments.
- `/opt/odoo-mcp/current`: active release symlink.
- `/etc/odoo-mcp/odoo-mcp.env`: app secrets and ODOO_CLIENTS.
- `/etc/odoo-mcp/webhook.secret`: GitHub HMAC secret.
- `/var/lib/odoo-mcp`: persistent app state.
- `/var/log/odoo-mcp`: deployment logs.

## Installation and update

Install prerequisites: `git python3 python3-venv python3-pip curl`. Create
(or retain) the `odoo-mcp` system user and ensure it owns
`/opt/odoo-mcp`, `/var/lib/odoo-mcp`, `/var/log/odoo-mcp`,
`/workspace/repos`, and `/home/odoo`. Keep `/etc/odoo-mcp` root-owned.

Install deployment files from the repository:

```bash
sudo install -m 755 deploy/deploy.sh /opt/odoo-mcp/deploy/deploy.sh
sudo install -m 644 deploy/webhook_receiver.py /opt/odoo-mcp/deploy/webhook_receiver.py
sudo install -m 644 deploy/odoo-mcp.service /etc/systemd/system/odoo-mcp.service
sudo install -m 644 deploy/odoo-mcp-webhook.service /etc/systemd/system/odoo-mcp-webhook.service
sudo install -m 644 deploy/odoo-mcp-deploy@.service /etc/systemd/system/odoo-mcp-deploy@.service
sudo install -m 755 deploy/queue-deploy.sh /usr/local/sbin/odoo-mcp-queue-deploy
sudo chown root:root /usr/local/sbin/odoo-mcp-queue-deploy
sudo install -m 440 deploy/odoo-mcp-sudoers /etc/sudoers.d/odoo-mcp-deploy
sudo visudo -cf /etc/sudoers.d/odoo-mcp-deploy
sudo systemctl daemon-reload
sudo systemctl enable --now odoo-mcp-webhook.service
```

The webhook receiver runs as the unprivileged `odoo-mcp` account. Its
`NoNewPrivileges=false` setting is required only because sudo uses a setuid
transition to invoke one root-owned wrapper: `/usr/local/sbin/odoo-mcp-queue-deploy`.
The wrapper accepts exactly one 40-character lowercase hexadecimal commit SHA
and starts only the corresponding fixed systemd template instance. The template
always executes the fixed root-owned deployment script; no shell command or
arbitrary path is accepted from the request.
Do not grant the webhook account any other sudo permission. The API remains
unprivileged with `NoNewPrivileges=true`.

Create root-owned `/etc/odoo-mcp/odoo-mcp.env` with fresh secrets and client
configuration. Never commit secrets. Create the webhook secret and environment:

```bash
openssl rand -hex 32 | sudo tee /etc/odoo-mcp/webhook.secret >/dev/null
sudo chown odoo-mcp:odoo-mcp /etc/odoo-mcp/webhook.secret
sudo chmod 600 /etc/odoo-mcp/webhook.secret
sudo chown root:odoo-mcp /etc/odoo-mcp/webhook.env
sudo chmod 640 /etc/odoo-mcp/webhook.env
```

Configure the webhook payload URL as
`https://mcp.example.com/github-webhook`, content type JSON, SSL verification
enabled, and the secret matching `/etc/odoo-mcp/webhook.secret`. Subscribe to
push events only. Proxy `/github-webhook` to `127.0.0.1:9001` via Caddy; keep
the API on local port 8080 and do not expose 9001.

## Deployment behavior

- HMAC-SHA256, event, repository, branch, commit SHA and delivery are validated.
- The receiver returns 202 after systemd accepts the one-shot deployment unit.
- Deployments serialize using `flock`; simultaneous pushes coalesce to latest master.
- Release dependencies are keyed by requirements hash and reusable.
- Compile/import checks run before activation.
- Failed restart or health check restores both active symlinks and restarts the old release.
- The deployment unit writes logs to `/var/log/odoo-mcp/deploy.log`.

Monitor with:

```bash
sudo journalctl -u 'odoo-mcp-deploy@*' -f
sudo journalctl -u odoo-mcp-webhook -f
sudo tail -f /var/log/odoo-mcp/deploy.log
curl -fsS https://mcp.example.com/health
```

### Hosted history retention

Run `sudo bash /opt/odoo-mcp/current/deploy/install_retention.sh` after the
first deployment that introduces retention. The installed timers survive releases.
`odoo-mcp-retention.timer` prunes inactive task history, terminal command payloads,
outputs, caches and clean disposable workspaces every five minutes. The hard
maximum age is 48 hours; the Client Manager Storage menu can reduce it.
Active jobs, held workspace locks, uncommitted changes and unpublished commits
remain protected. Client configuration, credentials and source repositories are
outside the cleanup scope. Expired request IDs retain only minimal hash-keyed
markers to prevent replay, without their original task or command data.

`odoo-mcp-log-retention.timer` rotates/vacuums the dedicated host's system journal
and rotates MCP file logs hourly. Journald retains two days with hourly files;
cleanup runs on a schedule, so expiry has up to one timer interval of granularity.
Inspect both service journals and `systemctl list-timers 'odoo-mcp-*retention*'`
to verify cleanup. Mixed-owner expired workspace directories are removed by a
root-owned helper restricted to explicitly expired execution directories.

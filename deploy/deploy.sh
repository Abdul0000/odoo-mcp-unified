#!/usr/bin/env bash
set -Eeuo pipefail
umask 027
ROOT="${DEPLOY_ROOT:-/opt/odoo-mcp}"
REPO="${DEPLOY_REPOSITORY_URL:-https://github.com/Abdul0000/odoo-mcp-unified.git}"
BRANCH="${DEPLOY_BRANCH:-master}"
SERVICE="${DEPLOY_SERVICE:-odoo-mcp.service}"
HEALTH_URL="${DEPLOY_HEALTH_URL:-http://127.0.0.1:8080/health}"
HEALTH_TIMEOUT="${DEPLOY_HEALTH_TIMEOUT:-25}"
PYTHON="${DEPLOY_PYTHON:-python3}"
EVENT_COMMIT="${1:-}"
[[ "$EVENT_COMMIT" =~ ^[0-9a-f]{40}$ ]] || { echo "Invalid commit SHA" >&2; exit 2; }
mkdir -p "$ROOT"/{releases,venvs,cache,deploy}
exec 9>"$ROOT/deploy/deploy.lock"
flock 9
MIRROR="$ROOT/repository.git"
if [[ ! -d "$MIRROR" ]]; then git clone --mirror "$REPO" "$MIRROR"; fi
git -c safe.directory="$MIRROR" --git-dir="$MIRROR" fetch --prune origin "+refs/heads/$BRANCH:refs/heads/$BRANCH"
COMMIT="$(git -c safe.directory="$MIRROR" --git-dir="$MIRROR" rev-parse "refs/heads/$BRANCH")"
if [[ "$COMMIT" != "$EVENT_COMMIT" ]]; then echo "Coalescing push; latest master is $COMMIT (event was $EVENT_COMMIT)."; fi
if [[ "$(basename "$(readlink "$ROOT/current" 2>/dev/null || true)")" == "$COMMIT" ]]; then
  echo "Already running latest commit $COMMIT; no restart required."
  exit 0
fi
RELEASE="$ROOT/releases/$COMMIT"
if [[ ! -d "$RELEASE" ]]; then
  git -c safe.directory="$MIRROR" -c safe.directory="$ROOT" clone --shared --no-checkout "$MIRROR" "$RELEASE"
  git -C "$RELEASE" checkout --detach "$COMMIT"
fi
REQ_HASH="$(sha256sum "$RELEASE/requirements.txt" | awk '{print $1}')"
VENV="$ROOT/venvs/$REQ_HASH"
if [[ ! -x "$VENV/bin/python" ]]; then
  rm -rf "$VENV"
  "$PYTHON" -m venv "$VENV"
  "$VENV/bin/python" -m pip install --disable-pip-version-check --cache-dir "$ROOT/cache/pip" -r "$RELEASE/requirements.txt"
fi
# The deployment unit runs as root, but the API reads the release and venv as odoo-mcp.
chown -R odoo-mcp:odoo-mcp "$RELEASE" "$VENV"
ln -sfn "$VENV" "$RELEASE/.venv"
UVICORN_SHEBANG="$(head -n 1 "$VENV/bin/uvicorn")"
if [[ "$UVICORN_SHEBANG" != "#!$VENV/bin/python"* ]]; then
  echo "Invalid uvicorn interpreter shebang: $UVICORN_SHEBANG" >&2
  exit 1
fi
"$VENV/bin/python" -m uvicorn --version
(cd "$RELEASE" && "$VENV/bin/python" -m compileall -q odoo_agent_server deploy)
(cd "$RELEASE" && "$VENV/bin/python" -c "from odoo_agent_server.main import app; assert app")
OLD_TARGET="$(readlink "$ROOT/current" 2>/dev/null || true)"
OLD_VENV="$(readlink "$ROOT/venv" 2>/dev/null || true)"
switch_links() {
  ln -sfn "$RELEASE" "$ROOT/current.next"; mv -Tf "$ROOT/current.next" "$ROOT/current"
  ln -sfn "$VENV" "$ROOT/venv.next"; mv -Tf "$ROOT/venv.next" "$ROOT/venv"
}
rollback() {
  local code=$?
  trap - ERR
  echo "Deployment failed (exit=$code); restoring previous release." >&2
  if [[ -n "$OLD_TARGET" ]]; then ln -sfn "$OLD_TARGET" "$ROOT/current.rollback"; mv -Tf "$ROOT/current.rollback" "$ROOT/current"; fi
  if [[ -n "$OLD_VENV" ]]; then ln -sfn "$OLD_VENV" "$ROOT/venv.rollback"; mv -Tf "$ROOT/venv.rollback" "$ROOT/venv"; fi
  systemctl restart "$SERVICE" || true
  exit "$code"
}
trap rollback ERR
switch_links
systemctl restart "$SERVICE"
healthy=0
for ((i=0; i<HEALTH_TIMEOUT; i++)); do
  if curl --fail --silent "$HEALTH_URL" | grep -q '"status":"ok"'; then healthy=1; break; fi
  sleep 1
done
if [[ "$healthy" != 1 ]]; then echo "Health check failed for $COMMIT" >&2; false; fi
trap - ERR
printf '%s\n' "$COMMIT" > "$ROOT/deploy/last-successful-commit"
echo "DEPLOY_SUCCESS commit=$COMMIT"
mapfile -t old_releases < <(find "$ROOT/releases" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %f\n' | sort -nr | awk '{print $2}' | grep -E '^[0-9a-f]{40}$' | grep -v "^$COMMIT$" | tail -n +3 || true)
for old in "${old_releases[@]}"; do rm -rf "$ROOT/releases/$old"; done

#!/usr/bin/env bash
set -Eeuo pipefail
umask 027
ROOT="${DEPLOY_ROOT:-/opt/odoo-mcp}"
REPO="${DEPLOY_REPOSITORY_URL:-https://github.com/Abdul0000/odoo-mcp-unified.git}"
BRANCH="${DEPLOY_BRANCH:-master}"
SERVICE="${DEPLOY_SERVICE:-odoo-mcp}"
HEALTH_URL="${DEPLOY_HEALTH_URL:-http://127.0.0.1:8080/health}"
HEALTH_TIMEOUT="${DEPLOY_HEALTH_TIMEOUT:-25}"
PYTHON="${DEPLOY_PYTHON:-python3}"
EVENT_COMMIT="${1:-}"
[[ "$EVENT_COMMIT" =~ ^[0-9a-f]{40}$ ]] || { echo "Invalid commit SHA" >&2; exit 2; }
mkdir -p "$ROOT"/{releases,venvs,cache,deploy}
exec 9>"$ROOT/deploy/deploy.lock"
# Wait rather than drop a push arriving during another deployment.
flock 9
MIRROR="$ROOT/repository.git"
if [[ ! -d "$MIRROR" ]]; then git clone --mirror "$REPO" "$MIRROR"; fi
git --git-dir="$MIRROR" fetch --prune origin "+refs/heads/$BRANCH:refs/heads/$BRANCH"
COMMIT="$(git --git-dir="$MIRROR" rev-parse "refs/heads/$BRANCH")"
if [[ "$COMMIT" != "$EVENT_COMMIT" ]]; then
  echo "Push event is behind current master; deploying latest master $COMMIT instead of $EVENT_COMMIT."
fi
RELEASE="$ROOT/releases/$COMMIT"
if [[ ! -d "$RELEASE" ]]; then
  git clone --shared --no-checkout "$MIRROR" "$RELEASE"
  git -C "$RELEASE" checkout --detach "$COMMIT"
fi
REQ_HASH="$(sha256sum "$RELEASE/requirements.txt" | awk '{print $1}')"
VENV="$ROOT/venvs/$REQ_HASH"
if [[ ! -x "$VENV/bin/python" ]]; then
  TMP_VENV="$ROOT/venvs/.build-$REQ_HASH-$$"
  rm -rf "$TMP_VENV"
  "$PYTHON" -m venv "$TMP_VENV"
  "$TMP_VENV/bin/pip" install --disable-pip-version-check --cache-dir "$ROOT/cache/pip" -r "$RELEASE/requirements.txt"
  mv "$TMP_VENV" "$VENV"
fi
ln -sfn "$VENV" "$RELEASE/.venv"
(cd "$RELEASE" && "$VENV/bin/python" -m compileall -q odoo_agent_server deploy)
(cd "$RELEASE" && "$VENV/bin/python" -c "from odoo_agent_server.main import app; assert app")
OLD_TARGET="$(readlink "$ROOT/current" 2>/dev/null || true)"
OLD_VENV="$(readlink "$ROOT/venv" 2>/dev/null || true)"
switch_links() {
  ln -sfn "$RELEASE" "$ROOT/current.next"; mv -Tf "$ROOT/current.next" "$ROOT/current"
  ln -sfn "$VENV" "$ROOT/venv.next"; mv -Tf "$ROOT/venv.next" "$ROOT/venv"
}
rollback() {
  echo "Deployment failed; restoring previous release." >&2
  if [[ -n "$OLD_TARGET" ]]; then ln -sfn "$OLD_TARGET" "$ROOT/current.rollback"; mv -Tf "$ROOT/current.rollback" "$ROOT/current"; fi
  if [[ -n "$OLD_VENV" ]]; then ln -sfn "$OLD_VENV" "$ROOT/venv.rollback"; mv -Tf "$ROOT/venv.rollback" "$ROOT/venv"; fi
  systemctl restart "$SERVICE" || true
}
switch_links
systemctl restart "$SERVICE"
healthy=0
for ((i=0; i<HEALTH_TIMEOUT; i++)); do
  if curl --fail --silent "$HEALTH_URL" >/dev/null 2>&1; then healthy=1; break; fi
  sleep 1
done
if [[ "$healthy" != 1 ]]; then rollback; exit 1; fi
printf '%s\n' "$COMMIT" > "$ROOT/deploy/last-successful-commit"
echo "DEPLOY_SUCCESS commit=$COMMIT"
mapfile -t old_releases < <(find "$ROOT/releases" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | grep -E '^[0-9a-f]{40}$' | sort -r | grep -v "^$COMMIT$" | tail -n +3 || true)
for old in "${old_releases[@]}"; do rm -rf "$ROOT/releases/$old"; done

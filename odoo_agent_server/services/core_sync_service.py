"""Phase 1 — Lazy on-demand sync of Odoo.sh core source into local cache paths.

Problem this solves:
  core_finder_service.py and syntax_verifier_service.py both read
  /home/odoo/src/{user,enterprise,odoo} via plain local Path() calls. On a
  Render deployment that tree does not exist locally — it only exists on the
  Odoo.sh build server, reachable over the SSH key already configured for
  OdooShService (ODOOSH_SSH_HOST / ODOOSH_SSH_KEY_PATH).

Fix:
  Before either service reports "not found" for a module directory, it calls
  ensure_module_synced() here. On first miss this rsyncs just that module's
  directory from the Odoo.sh build host into the *same local path* the rest
  of the code already expects — so no other file needs to know sync exists.
  Subsequent reads for the same module are local-disk fast until the cached
  copy goes stale (tracked via the live Odoo.sh deployed sha, falling back to
  a simple TTL if the sha can't be read).

Safety:
  - Fully best-effort. Any failure (no SSH host configured, rsync missing,
    network hiccup, timeout) just returns False / leaves the directory as it
    was — callers fall through to their existing "not found" path exactly as
    before this feature existed. Nothing here can break the current flow.
  - Disabled entirely via CORE_SYNC_ENABLED=false.
"""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from threading import Lock

from ..config import settings
from .client_resolver_service import ClientResolverService, ResolvedClient

_registry_lock = Lock()
_module_locks: dict[str, Lock] = {}
_sha_cache: dict[str, tuple[str | None, float]] = {}
_negative_cache: dict[str, float] = {}
_SHA_CACHE_TTL = 30.0  # don't hit odoosh.deployed_sha more than once per 30s
_NEGATIVE_CACHE_TTL = 120.0


def _module_lock(remote_root: str, module_name: str) -> Lock:
    """Per-(root, module) lock — concurrent syncs of *different* modules must
    not block behind each other. Only concurrent requests for the *same*
    module serialize, which is the actual race we need to prevent."""
    key = f"{remote_root}::{module_name}"
    with _registry_lock:
        lock = _module_locks.get(key)
        if lock is None:
            lock = Lock()
            _module_locks[key] = lock
        return lock


def _safe_name(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", value)


def _deployed_sha(client: ResolvedClient | None = None) -> str | None:
    """Best-effort fetch of the currently deployed commit sha on Odoo.sh.

    Cached briefly so a burst of ensure_module_synced() calls in one
    scope_advice/verify_against_core pass doesn't open a new SSH connection
    per module.
    """
    host = (client.odoosh_ssh_host if client else None) or settings.odoosh_ssh_host or ""
    user = (client.odoosh_ssh_user if client else None) or settings.odoosh_ssh_user or "odoo"
    port = (client.odoosh_ssh_port if client else None) or settings.odoosh_ssh_port or 22
    key_hint = (client.odoosh_ssh_key_path if client else None) or settings.odoosh_ssh_key_path or ""
    cache_identity = f"{user}@{host}:{port}:{key_hint}"
    now = time.time()
    cached = _sha_cache.get(cache_identity)
    if cached and (now - cached[1]) < _SHA_CACHE_TTL:
        return cached[0]
    sha: str | None = None
    try:
        from .odoosh_service import OdooShService

        result = OdooShService(client).deployed_sha()
        if result.exit_code == 0 and result.stdout:
            lines = [ln.strip() for ln in result.stdout.strip().splitlines() if ln.strip()]
            sha = lines[-1] if lines else None
    except Exception:  # noqa: BLE001
        sha = None
    _sha_cache[cache_identity] = (sha, now)
    return sha



def _resolve_client_for_sync(client_name: str | None = None, client: ResolvedClient | None = None) -> ResolvedClient | None:
    """Return the selected ODOO_CLIENTS entry for core sync.

    In multi-client mode the SSH settings live on the selected client, not on
    process-wide settings.  This helper keeps core sync isolated per client and
    falls back to global settings only for legacy single-client deployments.
    """
    if client is not None:
        return client
    try:
        return ClientResolverService().resolve_client(client_name=client_name, require_complete=False)
    except Exception:  # noqa: BLE001 - sync is best-effort and must never break callers
        return None


def _target_for_sync(client: ResolvedClient | None):
    try:
        from .odoosh_service import OdooShService

        return OdooShService(client)._target()  # reuse client-aware SSH/key resolution
    except Exception:  # noqa: BLE001 - caller treats None as sync unavailable
        return None


def _normalise_root(remote_root: str, client: ResolvedClient | None) -> str:
    """Normalize source roots to real Odoo.sh addon directories.

    Community addons are under /home/odoo/src/odoo/addons.  Older callers pass
    /home/odoo/src/odoo, which caused rsync to look for /home/odoo/src/odoo/<module>
    and miss every native addon.  Keep compatibility by mapping the Odoo source
    root to its addons subdirectory.
    """
    root = str(remote_root or "").rstrip("/")
    odoo_root = (client.odoosh_odoo_path if client else None) or "/home/odoo/src/odoo"
    odoo_root = str(odoo_root).rstrip("/")
    if root == odoo_root:
        return f"{odoo_root}/addons"
    if root.endswith("/odoo"):
        return f"{root}/addons"
    return root

def ensure_module_synced(remote_root: str, module_name: str, timeout_seconds: int = 15, *, client_name: str | None = None, client: ResolvedClient | None = None) -> bool:
    """Best-effort: make sure {remote_root}/{module_name} exists and is fresh locally.

    remote_root must be one of the existing hardcoded roots
    (/home/odoo/src/user|enterprise|odoo) — the local cache path is the
    identical string, so no other code needs to change.

    Returns True if the local directory exists by the time this returns
    (whether already-fresh, freshly synced, or pre-existing). Never raises.
    """
    if not settings.core_sync_enabled:
        return False
    if not module_name or "/" in module_name or module_name.startswith("."):
        return False

    selected_client = _resolve_client_for_sync(client_name=client_name, client=client)
    target = _target_for_sync(selected_client)
    if target is None:
        return False

    source_root = _normalise_root(remote_root, selected_client)
    local_dir = Path(source_root) / module_name
    marker = local_dir / ".synced_sha"
    sha = _deployed_sha(selected_client)
    key_path = str(target.key_path or "")
    negative_key = f"{target.user}@{target.host}:{target.port}:{key_path}:{source_root}:{module_name}:{sha or 'unknown'}"
    now = time.monotonic()
    negative_until = _negative_cache.get(negative_key, 0.0)
    if negative_until > now and not (local_dir.exists() and any(local_dir.iterdir())):
        return False

    def remember_failure() -> None:
        if len(_negative_cache) > 512:
            expired = [key for key, until in _negative_cache.items() if until <= time.monotonic()]
            for key in expired:
                _negative_cache.pop(key, None)
            if len(_negative_cache) > 512:
                _negative_cache.clear()
        _negative_cache[negative_key] = time.monotonic() + _NEGATIVE_CACHE_TTL

    with _module_lock(remote_root, module_name):
        if local_dir.exists() and local_dir.is_dir():
            has_content = any(local_dir.iterdir())
            if sha and has_content:
                try:
                    cached_sha = marker.read_text(encoding="utf-8").strip() if marker.exists() else None
                except OSError:
                    cached_sha = None
                if cached_sha == sha:
                    return True
            elif not sha and has_content:
                try:
                    age = time.time() - local_dir.stat().st_mtime
                except OSError:
                    age = 0.0
                if age < settings.core_sync_max_age_seconds:
                    return True
            # Empty dir (e.g. a prior sync attempt found nothing) always
            # falls through to retry below, rather than being cached as
            # "fresh" — avoids a transient failure looking permanent.

        try:
            local_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            remember_failure()
            return local_dir.exists()

        ssh_opts = [
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=10",
            "-p", str(target.port),
        ]
        if target.key_path:
            ssh_opts += ["-i", str(target.key_path), "-o", "IdentitiesOnly=yes"]
        remote = f"{target.user}@{target.host}:{source_root}/{module_name}/"
        cmd = [
            "rsync", "-az", "--delete", "--timeout", str(timeout_seconds),
            "-e", "ssh " + " ".join(ssh_opts),
            remote, str(local_dir) + "/",
        ]
        try:
            completed = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout_seconds + 5,
            )
        except (subprocess.TimeoutExpired, OSError):
            remember_failure()
            return local_dir.exists() and any(local_dir.iterdir()) if local_dir.exists() else False

        if completed.returncode != 0:
            # Sync failed (module may genuinely not exist on that root, or
            # transient network issue) — leave whatever was there before and
            # let the caller's normal not-found path handle it.
            remember_failure()
            return local_dir.exists() and any(local_dir.iterdir()) if local_dir.exists() else False

        if sha:
            try:
                marker.write_text(sha, encoding="utf-8")
            except OSError:
                pass
        _negative_cache.pop(negative_key, None)
        return True

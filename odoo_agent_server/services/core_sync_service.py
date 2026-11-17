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
import hashlib
import json
import functools
import inspect
from contextvars import ContextVar
import subprocess
import shlex
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
_deadline = ContextVar("core_inspection_deadline", default=None)
_source_revisions = ContextVar("core_source_revisions", default=None)


def bounded_inspection(fn):
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def async_wrapped(*args, **kwargs):
            if _deadline.get() is not None:
                return await fn(*args, **kwargs)
            token = _deadline.set(time.monotonic() + 30)
            revision_token = _source_revisions.set({})
            try:
                return await fn(*args, **kwargs)
            finally:
                _deadline.reset(token)
                _source_revisions.reset(revision_token)
        return async_wrapped
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        if _deadline.get() is not None:
            return fn(*args, **kwargs)
        token = _deadline.set(time.monotonic() + 20)
        revision_token = _source_revisions.set({})
        try:
            return fn(*args, **kwargs)
        finally:
            _deadline.reset(token)
            _source_revisions.reset(revision_token)
    return wrapped


def _remaining(default):
    deadline = _deadline.get()
    return max(0, min(default, deadline - time.monotonic())) if deadline else default


def cache_root(remote_root, client):
    if client is None:
        return Path(remote_root)
    identity = "|".join(str(getattr(client, name, "") or "") for name in
                        ("name", "repo", "branch", "odoosh_ssh_host", "odoosh_ssh_user", "odoosh_db_name", "odoosh_odoo_path"))
    key = hashlib.sha256(identity.encode()).hexdigest()[:24]
    root_key = hashlib.sha256(str(remote_root).encode()).hexdigest()[:16]
    return Path(settings.state_dir) / "core_sources" / key / root_key


def source_roots(client_name=None):
    client = _resolve_client_for_sync(client_name=client_name)
    roots = ClientResolverService().search_roots(client)
    return [cache_root(root, client) for root in roots]


def _remote_root(root, client):
    if client:
        for remote in ClientResolverService().search_roots(client):
            if Path(root) == cache_root(remote, client):
                return remote
    return root


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

        budget = max(1, int(_remaining(10)))
        try:
            result = OdooShService(client).deployed_sha(timeout_seconds=budget)
        except TypeError:  # compatibility with old integrations/test doubles
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
    if client is not None and not (client.odoosh_ssh_host and client.odoosh_ssh_user):
        return None  # Never borrow another client's process-global SSH identity.
    try:
        from .odoosh_service import OdooShService

        return OdooShService(client)._target()  # reuse client-aware SSH/key resolution
    except Exception:  # noqa: BLE001 - caller treats None as sync unavailable
        return None


def source_transport_ready(client_name):
    client = _resolve_client_for_sync(client_name=client_name)
    target = _target_for_sync(client) if client else None
    if target is None or _remaining(1) <= 0:
        return False
    key = f"transport:{target.user}@{target.host}:{target.port}:{target.key_path or ''}"
    return _negative_cache.get(key, 0) <= time.monotonic()


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
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", module_name or ""):
        return False

    selected_client = _resolve_client_for_sync(client_name=client_name, client=client)
    target = _target_for_sync(selected_client)
    if target is None:
        return False

    source_root = _normalise_root(str(_remote_root(remote_root, selected_client)), selected_client)
    local_dir = cache_root(source_root, selected_client) / module_name
    managed = selected_client is not None
    marker = local_dir / ".synced_sha"
    key_path = str(target.key_path or "")
    transport_key = f"transport:{target.user}@{target.host}:{target.port}:{key_path}"
    negative_key = f"{transport_key}:{source_root}:{module_name}"
    identity_file = local_dir / ".source_identity.json"
    now = time.monotonic()
    if _remaining(timeout_seconds) <= 0 or _negative_cache.get(transport_key, 0) > now:
        return False
    negative_until = _negative_cache.get(negative_key, 0.0)
    if negative_until > now and not (local_dir.exists() and any(local_dir.iterdir())):
        return False
    if managed and negative_until > now:
        return False
    revisions = _source_revisions.get()
    revision_key = (transport_key, source_root)
    if revisions is not None and revision_key in revisions:
        sha = revisions[revision_key]
    else:
        if not managed or source_root.rstrip("/") == str((selected_client.odoosh_user_addons_path if selected_client else None) or "/home/odoo/src/user").rstrip("/"):
            sha = _deployed_sha(selected_client)
        else:
            # Community and Enterprise have their own revisions, unrelated to user HEAD.
            from .odoosh_service import OdooShService
            git_root = source_root[:-7] if source_root.endswith("/addons") else source_root
            result = OdooShService(selected_client).run_shell(
                f"git -C {shlex.quote(git_root)} rev-parse HEAD", timeout_seconds=max(.001, _remaining(timeout_seconds)))
            sha = result.stdout.strip() if result.exit_code == 0 else None
        if revisions is not None:
            revisions[revision_key] = sha
    if managed and not sha:
        return False
    timeout_seconds = max(0, int(_remaining(timeout_seconds)))
    if timeout_seconds <= 0:
        return False

    def remember_failure() -> None:
        if len(_negative_cache) > 512:
            expired = [key for key, until in _negative_cache.items() if until <= time.monotonic()]
            for key in expired:
                _negative_cache.pop(key, None)
            if len(_negative_cache) > 512:
                _negative_cache.clear()
        _negative_cache[negative_key] = time.monotonic() + _NEGATIVE_CACHE_TTL

    lock = _module_lock(str(local_dir.parent), module_name)
    if not lock.acquire(timeout=max(.001, _remaining(timeout_seconds))):
        return False
    try:
        if local_dir.exists() and local_dir.is_dir():
            has_content = any(local_dir.iterdir())
            if sha and has_content:
                try:
                    cached_sha = marker.read_text(encoding="utf-8").strip() if marker.exists() else None
                except OSError:
                    cached_sha = None
                if cached_sha == sha:
                    return True
            elif not managed and not sha and has_content:
                try:
                    age = time.time() - local_dir.stat().st_mtime
                except OSError:
                    age = 0.0
                if age < settings.core_sync_max_age_seconds:
                    return True
            # Empty dir (e.g. a prior sync attempt found nothing) always
            # falls through to retry below, rather than being cached as
            # "fresh" — avoids a transient failure looking permanent.

        marker.unlink(missing_ok=True)
        identity_file.unlink(missing_ok=True)
        try:
            local_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            remember_failure()
            return False if managed else local_dir.exists()

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
                cmd, capture_output=True, text=True, timeout=max(1, _remaining(timeout_seconds)),
            )
        except (subprocess.TimeoutExpired, OSError):
            remember_failure()
            _negative_cache[transport_key] = time.monotonic() + _NEGATIVE_CACHE_TTL
            return False if managed else local_dir.exists() and any(local_dir.iterdir())

        if completed.returncode != 0:
            # Sync failed (module may genuinely not exist on that root, or
            # transient network issue) — leave whatever was there before and
            # let the caller's normal not-found path handle it.
            remember_failure()
            if completed.returncode == 255 or any(word in (getattr(completed, "stderr", "") or "").lower() for word in ("permission denied", "connection timed out", "connection refused")):
                _negative_cache[transport_key] = time.monotonic() + _NEGATIVE_CACHE_TTL
            return False if managed else local_dir.exists() and any(local_dir.iterdir())

        if sha:
            try:
                marker.write_text(sha, encoding="utf-8")
            except OSError:
                pass
        _negative_cache.pop(negative_key, None)
        if managed:
            identity_file.write_text(json.dumps({"root": source_root, "synced_at": time.time(), "sha": sha}))
        return True
    finally:
        lock.release()

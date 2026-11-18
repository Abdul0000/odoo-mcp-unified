"""Scheduled 48-hour history cleanup; no client repositories/configuration touched.

Small hash-only expiry markers prevent old request IDs replaying after payload
removal. Live locks/queued work and unpublished working copies are preserved.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import time

from ..config import settings
from .client_settings_store import atomic_json
from .execution_service import git
from .execution_store import ExecutionStore
from . import mutation_journal

ACTIVE = {"queued", "running", "initializing", "cleanup_failed", "quarantined"}


def stamp(row, fallback):
    for key in ("finished_at", "last_progress_at", "updated_at", "created_at", "timestamp", "updated", "created"):
        value = row.get(key)
        try:
            if isinstance(value, (int, float)):
                return float(value)
            if isinstance(value, str):
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
        except (ValueError, TypeError, OverflowError):
            pass
    return fallback


@contextmanager
def unlocked(path):
    fd = None
    acquired = False
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o660)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            pass
        yield acquired
    finally:
        if fd is not None:
            os.close(fd)


class RetentionService:
    def __init__(self, now=None):
        self.now = time.time() if now is None else now
        self.cutoff = self.now - min(172800, settings.history_retention_seconds)
        self.state = settings.state_path
        self.counts = {"tasks": 0, "jobs": 0, "execution_records": 0, "workspaces": 0, "logs": 0, "history_rows": 0, "sidecars": 0, "protected_workspaces": 0}
        self.active_ids = set()
        self.active_ws = set()
        self.projects = set()
        self.clients = set()

    @staticmethod
    def read(path):
        if path.is_symlink():
            return None
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None

    def old(self, path, row=None):
        try:
            return stamp(row or {}, path.stat().st_mtime) < self.cutoff
        except OSError:
            return False

    def protected(self, row):
        refs = row.get("refs") or {}
        values = [row.get(k) for k in ("id", "job_id", "approval_id", "operation_id", "workspace_id")]
        values += [refs.get("workspace_id"), *(refs.get("job_ids") or [])]
        metadata = row.get("metadata") or {}
        if isinstance(metadata, dict):
            values += [metadata.get(k) for k in ("job_id", "workspace_id", "approval_id")]
        return any(isinstance(v, str) and v in self.active_ids for v in values) or refs.get("workspace_id") in self.active_ws

    def snapshot(self):
        for path in (self.state / "jobs").glob("*.json"):
            row = self.read(path)
            if isinstance(row, dict) and row.get("status") in ACTIVE:
                self.active_ids.add(str(row.get("id")))
                req = row.get("request") or {}
                for key in ("approval_id", "operation_id", "debug_session_id"):
                    if req.get(key):
                        self.active_ids.add(req[key])
                if req.get("project_path"):
                    self.projects.add(req["project_path"])
                if req.get("client_name"):
                    self.clients.add(req["client_name"])
        if not settings.execution_enabled:
            return
        self.store = ExecutionStore()
        with self.store._lock(self.store.root / "queue.lock"):
            rows = [row for kind in self.store.KINDS for row in self.store._rows(kind)]
        for row in rows:
            if row["status"] not in ACTIVE:
                continue
            self.active_ids.add(row["id"])
            request = row["data"].get("request") or {}
            wid = row["id"] if row["kind"] == "workspace" else request.get("workspace_id")
            if wid:
                self.active_ws.add(wid)
                self.active_ids.add(wid)
            for key in ("approval_id", "operation_id"):
                if request.get(key):
                    self.active_ids.add(request[key])
        for row in rows:
            if row["kind"] == "workspace" and row["id"] in self.active_ws:
                if row["data"].get("source_path"):
                    self.projects.add(row["data"]["source_path"])
                if row["data"].get("client_name"):
                    self.clients.add(row["data"]["client_name"])
        return rows

    def execution(self, rows):
        store = self.store
        by_ws = {row["id"]: row for row in rows if row["kind"] == "workspace"}
        for wid, row in by_ws.items():
            if not re.fullmatch("[a-f0-9]{32}", wid) or not self.old(store._path(wid, "workspace"), row):
                continue
            if wid in self.active_ws:
                continue
            if row["status"] == "expired" and not (store.root / "workspaces" / wid).exists():
                continue
            try:
                with store.workspace_lock(wid, blocking=False):
                    with store._lock(store.root / "queue.lock"):
                        # Recheck authoritative queue after taking the workspace lock.
                        active = [r for kind in ("process", "agent", "operation") for r in store._rows(kind) if r["status"] in ACTIVE and r["data"].get("request", {}).get("workspace_id") == wid]
                        if active:
                            self.active_ws.add(wid)
                            continue
                        current = store.get(wid, "workspace")
                        root = store.root / "workspaces" / wid / "repo"
                        protected = False
                        if row["status"] != "expired" and root.is_dir() and not root.is_symlink():
                            try:
                                dirty = git(["status", "--porcelain"], root, timeout=5).strip()
                                head = git(["rev-parse", "HEAD"], root, timeout=5).strip()
                                baseline = current.get("published_commit") if current.get("pushed") else current.get("base_sha")
                                protected = bool(dirty or not baseline or head != baseline)
                            except Exception:
                                protected = True
                        if protected:
                            self.active_ws.add(wid)
                            self.active_ids.add(wid)
                            self.counts["protected_workspaces"] += 1
                            if current.get("source_path"):
                                self.projects.add(current["source_path"])
                            if current.get("client_name"):
                                self.clients.add(current["client_name"])
                            continue
                        tomb = {"id": wid, "kind": "workspace", "status": "expired", "created": 0, "updated": 0, "data": {}}
                        store._save_row(tomb)
                    folder = store.root / "workspaces" / wid
                    if folder.is_symlink():
                        folder.unlink()
                    elif folder.exists():
                        try:
                            shutil.rmtree(folder)
                        except PermissionError:
                            pass  # Root-owned post-step finishes mixed-UID directories.
                    self.counts["workspaces"] += 1
            except BlockingIOError:
                self.active_ws.add(wid)
        # Payload/log expiry is independent of source retention. A protected draft
        # keeps its workspace; old terminal commands and captured output can expire.
        with store._lock(store.root / "queue.lock"):
            for kind in ("process", "agent", "publish", "operation"):
                for row in list(store._rows(kind)):
                    if row["status"] in ACTIVE or row["status"] == "expired" or not self.old(store._path(row["id"], kind), row):
                        continue
                    store._save_row({"id": row["id"], "kind": kind, "status": "expired", "created": 0, "updated": 0, "data": {}})
                    self.counts["execution_records"] += 1
            active_process = {r["id"] for r in store._rows("process") if r["status"] in ACTIVE}
            for path in (store.root / "logs").glob("*.log"):
                if path.stem not in active_process and self.old(path) and not path.is_symlink():
                    path.unlink()
                    self.counts["logs"] += 1

    def run(self):
        rows = self.snapshot()
        if settings.execution_enabled:
            self.execution(rows or [])
        for path in (self.state / "tasks").glob("*.json"):
            with unlocked(path.with_suffix(".lock")) as acquired:
                if not acquired:
                    continue
                row = self.read(path)
                if not isinstance(row, dict) or not self.old(path, row) or self.protected(row):
                    continue
                atomic_json(self.state / "task-tombstones" / path.name, {"expired": True})
                path.unlink(missing_ok=True)
                self.counts["tasks"] += 1
        for path in (self.state / "jobs").glob("*.json"):
            with unlocked(path.with_suffix(".lock")) as acquired:
                if not acquired:
                    continue
                row = self.read(path)
                if isinstance(row, dict) and row.get("status") not in ACTIVE and self.old(path, row):
                    path.unlink(missing_ok=True)
                    self.counts["jobs"] += 1
        patterns = ["audit.json", "approvals.json", "functional_mutations.json", "workflow_progress.json", "debug_sessions__*.json", "operation_handoffs__*.json"]
        for pattern in patterns:
            for path in self.state.glob(pattern):
                with unlocked(path.with_suffix(".lock")) as acquired:
                    if not acquired:
                        continue
                    rows = self.read(path)
                    if not isinstance(rows, list):
                        continue
                    kept = []
                    for row in rows:
                        if not isinstance(row, dict) or not self.old(path, row) or self.protected(row):
                            kept.append(row)
                            continue
                        if path.name == "functional_mutations.json":
                            marker = mutation_journal.path(str(row.get("client_name") or "default"), str(row.get("request_id") or ""))
                            with unlocked(marker.with_suffix(".lock")) as free:
                                if not free:
                                    kept.append(row)
                                    continue
                                atomic_json(marker, {"state": "expired"})
                        self.counts["history_rows"] += 1
                    if len(kept) != len(rows):
                        atomic_json(path, kept)
        for path in (self.state / "mutation-intents").glob("*.json"):
            with unlocked(path.with_suffix(".lock")) as acquired:
                if acquired and self.old(path):
                    row = self.read(path)
                    if isinstance(row, dict) and row.get("state") != "expired":
                        atomic_json(path, {"state": "expired"})
                        self.counts["sidecars"] += 1
        # These are cache/history collections only, never client settings, OAuth,
        # SSH keys, repo checkout paths or environment files.
        for dirname in ("artifacts", "coding_sessions", "coding_workspaces", "mutation-previews", "client-knowledge", "client-admin-login"):
            root = self.state / dirname
            protected_bundles = set()
            for meta in root.glob("*.json"):
                record = self.read(meta)
                if not isinstance(record, dict):
                    continue
                if (root / ".locks" / (meta.stem + ".lock")).exists() or record.get("project_path") in self.projects:
                    bundle = record.get("bundle") or {}
                    if isinstance(bundle, dict) and bundle.get("name"):
                        protected_bundles.add(bundle["name"])
                    for entry in (record.get("modules") or {}).values():
                        if isinstance(entry, dict) and isinstance(entry.get("bundle"), dict):
                            protected_bundles.add(entry["bundle"].get("name"))
            for path in root.iterdir() if root.exists() else []:
                if path.name in protected_bundles or path.is_symlink() or not path.is_file() or path.suffix == ".lock" or path.name.startswith(".") or not self.old(path):
                    continue
                lock = root / ".locks" / (path.stem + ".lock")
                if lock.exists():
                    continue
                row = self.read(path) if path.suffix == ".json" else None
                if isinstance(row, dict) and (row.get("project_path") in self.projects or self.protected(row)):
                    continue
                path.unlink(missing_ok=True)
                self.counts["sidecars"] += 1
        for path in self.state.glob("session_state*.json"):
            row = self.read(path)
            if not isinstance(row, dict) or not self.old(path, row):
                continue
            if row.get("client_name") in self.clients or row.get("project_path") in self.projects:
                continue
            with unlocked(path.with_suffix(".lock")) as acquired:
                if acquired and self.old(path, self.read(path) or {}):
                    path.unlink(missing_ok=True)
                    self.counts["sidecars"] += 1
        if settings.execution_enabled:
            for path in (self.store.root / "input").glob("*.json"):
                if path.stem not in self.active_ids and self.old(path) and not path.is_symlink():
                    path.unlink()
                    self.counts["sidecars"] += 1
        screen = Path(settings.screenshot_dir)
        if not screen.is_symlink() and (screen.is_relative_to(self.state) or str(screen) == "/tmp/odoo_agent_screenshots"):
            for path in screen.glob("*"):
                if path.is_file() and not path.is_symlink() and self.old(path):
                    path.unlink()
                    self.counts["sidecars"] += 1
        from .browser_extension_service import BrowserExtensionService
        self.counts["sidecars"] += BrowserExtensionService().cleanup(self.cutoff)
        return {"retention_seconds": min(172800, settings.history_retention_seconds), "removed": self.counts}


if __name__ == "__main__":
    print(json.dumps(RetentionService().run()))

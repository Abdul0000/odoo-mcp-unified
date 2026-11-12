from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
import zipfile
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from threading import RLock
from typing import Any

from fastapi import HTTPException, status

from ..config import settings


_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")
_SKIP_PARTS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules"}
_SKIP_REL_PREFIXES = ("static/lib/",)
_SKIP_SUFFIXES = (".map", ".pyc")


class CodingSessionService:
    """Durable, SHA-bound module snapshot used by code-change continuations.

    The archive is a transport/checkpoint artifact only. Edits continue to use
    the validated repository checkout, so Git, QA, and deployment ownership do
    not change. A session becomes stale when HEAD or the module fingerprint
    differs from the snapshot that produced the write context.
    """

    _lock = RLock()
    _record_locks_guard = RLock()
    _record_locks: dict[str, RLock] = {}

    def __init__(self) -> None:
        self.root = settings.state_path / "coding_sessions"
        self.root.mkdir(parents=True, exist_ok=True)

    def open(
        self,
        *,
        session_id: str,
        client_name: str | None,
        project_path: str,
        module_name: str,
        module_path: str,
        task_description: str,
        target_files: list[str],
        evidence: dict[str, Any],
        status_value: str = "awaiting_writes",
        create_bundle: bool = True,
        workspace_session_id: str | None = None,
        snapshot_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._validate_id(session_id)
        project = Path(project_path).expanduser().resolve()
        module = Path(module_path).expanduser().resolve()
        self._validate_module(project, module)
        if snapshot_metadata is not None:
            snapshot = self._validated_snapshot_metadata(snapshot_metadata)
        else:
            snapshot = (
                self._snapshot(session_id, module_name, module)
                if create_bundle
                else {"module_fingerprint": "", "bundle": {}}
            )
        if not create_bundle and snapshot_metadata is None:
            self._bundle_path(session_id).unlink(missing_ok=True)
        now = time.time()
        previous = self._read(session_id, required=False) or {}
        record = {
            "id": session_id,
            "client_name": client_name,
            "project_path": str(project),
            "workspace_session_id": workspace_session_id,
            "snapshot_owner": "workspace" if snapshot_metadata is not None else "task",
            "module_name": module_name,
            "module_path": str(module),
            "task_description": str(task_description or "").strip(),
            "task_hash": self.task_hash(task_description),
            "base_sha": self._git_sha(project),
            "module_fingerprint": snapshot["module_fingerprint"],
            "bundle": snapshot["bundle"],
            "bundle_version": int(previous.get("bundle_version") or 0) + 1,
            "target_files": list(dict.fromkeys(str(item) for item in target_files if str(item).strip()))[:100],
            "evidence": evidence,
            "status": status_value,
            "created_at": float(previous.get("created_at") or now),
            "updated_at": now,
            "expires_at": now + self._ttl_seconds(),
        }
        self._write(record)
        self._cleanup()
        return record

    def resume(
        self,
        session_id: str,
        *,
        client_name: str | None,
        project_path: str,
        task_description: str,
    ) -> dict[str, Any]:
        record = self._read(session_id)
        project = Path(project_path).expanduser().resolve()
        if str(record.get("project_path")) != str(project):
            return self._invalid(record, "project_path_changed")
        stored_client = str(record.get("client_name") or "")
        if stored_client and str(client_name or "") != stored_client:
            return self._invalid(record, "client_changed")
        incoming_task_changed = (
            str(record.get("task_hash") or "") != self.task_hash(task_description)
        )
        # ``coding_session_id`` is the server-owned continuation contract.  A
        # provider may paraphrase the task while generating writes, but that
        # must not invalidate otherwise current source evidence.  New records
        # retain the canonical task so the router can restore it.  Legacy
        # records without that scope remain strict rather than guessing.
        if incoming_task_changed and not str(record.get("task_description") or "").strip():
            return self._invalid(record, "task_changed")
        if float(record.get("expires_at") or 0) <= time.time():
            return self._invalid(record, "session_expired")
        module = Path(str(record.get("module_path") or "")).expanduser().resolve()
        try:
            self._validate_module(project, module)
        except HTTPException:
            return self._invalid(record, "module_missing")
        current_sha = self._git_sha(project)
        if current_sha != str(record.get("base_sha") or ""):
            return self._invalid(record, "git_head_changed", current_sha=current_sha)
        current_fingerprint = self._module_fingerprint(module)
        if current_fingerprint != str(record.get("module_fingerprint") or ""):
            return self._invalid(record, "module_changed", current_sha=current_sha)
        record["valid"] = True
        record["resume_reason"] = (
            "snapshot_current_scope_locked"
            if incoming_task_changed
            else "snapshot_current"
        )
        record["incoming_task_changed"] = incoming_task_changed
        record["updated_at"] = time.time()
        record["expires_at"] = time.time() + self._ttl_seconds()
        self._write(record)
        return record

    def refresh(
        self,
        session_id: str,
        *,
        status_value: str,
        target_files: list[str] | None = None,
        evidence: dict[str, Any] | None = None,
        create_bundle: bool = True,
        snapshot_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        record = self._read(session_id)
        project = Path(str(record["project_path"])).resolve()
        module = Path(str(record["module_path"])).resolve()
        self._validate_module(project, module)
        if snapshot_metadata is not None:
            snapshot = self._validated_snapshot_metadata(snapshot_metadata)
        else:
            snapshot = (
                self._snapshot(session_id, str(record["module_name"]), module)
                if create_bundle
                else {
                    "module_fingerprint": self._module_fingerprint(module),
                    "bundle": dict(record.get("bundle") or {}),
                }
            )
        record.update({
            "base_sha": self._git_sha(project),
            "module_fingerprint": snapshot["module_fingerprint"],
            "bundle": snapshot["bundle"],
            "bundle_version": int(record.get("bundle_version") or 0) + (1 if create_bundle or snapshot_metadata is not None else 0),
            "snapshot_owner": "workspace" if snapshot_metadata is not None else record.get("snapshot_owner", "task"),
            "status": status_value,
            "updated_at": time.time(),
            "expires_at": time.time() + self._ttl_seconds(),
        })
        if target_files is not None:
            record["target_files"] = list(dict.fromkeys(str(item) for item in target_files if str(item).strip()))[:100]
        if evidence is not None:
            record["evidence"] = evidence
        self._write(record)
        record["valid"] = True
        return record

    def mark(self, session_id: str, status_value: str, **extra: Any) -> dict[str, Any]:
        record = self._read(session_id)
        record.update(extra)
        record["status"] = status_value
        record["updated_at"] = time.time()
        record["expires_at"] = time.time() + self._ttl_seconds()
        self._write(record)
        return record

    @staticmethod
    def task_hash(task_description: str) -> str:
        normalized = re.sub(r"\s+", " ", str(task_description or "").strip().lower())
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @staticmethod
    def _validated_snapshot_metadata(value: dict[str, Any]) -> dict[str, Any]:
        fingerprint = str(value.get("module_fingerprint") or "")
        bundle = dict(value.get("bundle") or {})
        if not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Workspace module snapshot is incomplete",
            )
        return {"module_fingerprint": fingerprint, "bundle": bundle}

    @staticmethod
    def public_summary(record: dict[str, Any] | None) -> dict[str, Any] | None:
        if not record:
            return None
        bundle = dict(record.get("bundle") or {})
        return {
            "session_id": record.get("id"),
            "workspace_session_id": record.get("workspace_session_id"),
            "status": record.get("status"),
            "valid": record.get("valid", True),
            "resume_reason": record.get("resume_reason"),
            "base_sha": record.get("base_sha"),
            "snapshot_owner": record.get("snapshot_owner"),
            "bundle_version": record.get("bundle_version"),
            "bundle": {
                "name": bundle.get("name"),
                "bytes": bundle.get("bytes"),
                "file_count": bundle.get("file_count"),
                "omitted_file_count": bundle.get("omitted_file_count"),
                "sha256": bundle.get("sha256"),
            },
            "target_files": list(record.get("target_files") or []),
            "expires_at": record.get("expires_at"),
        }

    def _snapshot(
        self,
        session_id: str,
        module_name: str,
        module: Path,
        module_fingerprint: str | None = None,
        module_files: list[Path] | None = None,
    ) -> dict[str, Any]:
        files = module_files if module_files is not None else self._module_files(module)
        max_files = max(1, int(getattr(settings, "coding_session_bundle_max_files", 2000)))
        max_bytes = max(1, int(getattr(settings, "coding_session_bundle_max_bytes", 50_000_000)))
        aggregate_limit = (
            int(getattr(settings, "coding_workspace_max_total_bytes", 500_000_000))
            if isinstance(self, CodingWorkspaceService)
            else int(getattr(settings, "coding_session_max_total_bytes", 250_000_000))
        )
        max_bytes = min(max_bytes, max(1, aggregate_limit))
        selected: list[Path] = []
        total = 0
        for path in files:
            rel = path.relative_to(module).as_posix()
            if rel.startswith(_SKIP_REL_PREFIXES) or path.name.endswith(_SKIP_SUFFIXES):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if len(selected) >= max_files or total + size > max_bytes:
                continue
            selected.append(path)
            total += size
        bundle_path = self._bundle_path(session_id)
        tmp_path = bundle_path.with_suffix(".zip.tmp")
        digest = hashlib.sha256()
        archived_count = 0
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            for path in selected:
                rel = path.relative_to(module).as_posix()
                try:
                    data = path.read_bytes()
                except OSError:
                    continue
                digest.update(rel.encode("utf-8")); digest.update(b"\0"); digest.update(data)
                archive.writestr(f"{module_name}/{rel}", data)
                archived_count += 1
        tmp_path.replace(bundle_path)
        return {
            "module_fingerprint": module_fingerprint or self._module_fingerprint_from_files(module, files),
            "bundle": {
                "name": bundle_path.name,
                "bytes": bundle_path.stat().st_size,
                "file_count": archived_count,
                "omitted_file_count": max(0, len(files) - archived_count),
                "sha256": digest.hexdigest(),
            },
        }

    def _module_fingerprint(self, module: Path) -> str:
        """Content-derived module identity; never trust size/mtime alone."""
        return self._module_fingerprint_from_files(module, self._module_files(module))

    @staticmethod
    def _module_fingerprint_from_files(module: Path, files: list[Path]) -> str:
        digest = hashlib.sha256()
        for path in files:
            rel = path.relative_to(module).as_posix()
            try:
                data = path.read_bytes()
            except OSError:
                continue
            digest.update(rel.encode("utf-8")); digest.update(b"\0"); digest.update(data); digest.update(b"\n")
        return digest.hexdigest()

    @staticmethod
    def _module_files(module: Path) -> list[Path]:
        return sorted(
            (
                path for path in module.rglob("*")
                if path.is_file()
                and not path.is_symlink()
                and not any(part in _SKIP_PARTS for part in path.relative_to(module).parts)
            ),
            key=lambda path: path.relative_to(module).as_posix(),
        )

    @staticmethod
    def _git_sha(project: Path) -> str:
        result = subprocess.run(
            ["git", "-C", str(project), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else "untracked"

    @staticmethod
    def _validate_module(project: Path, module: Path) -> None:
        try:
            module.relative_to(project)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Coding-session module escapes repository") from exc
        if not module.is_dir() or not (module / "__manifest__.py").is_file():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Coding-session module is not available")

    def _record_path(self, session_id: str) -> Path:
        safe = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:40]
        return self.root / f"{safe}.json"

    @classmethod
    def _record_lock(cls, session_id: str) -> RLock:
        with cls._record_locks_guard:
            return cls._record_locks.setdefault(session_id, RLock())

    @contextmanager
    def _record_guard(self, session_id: str):
        """Per-record in-process and cross-process lock for atomic workspace updates."""
        lock = self._record_lock(session_id)
        safe = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:40]
        lock_root = self.root / ".locks"
        lock_dir = lock_root / f"{safe}.lock"
        with lock:
            lock_root.mkdir(parents=True, exist_ok=True)
            deadline = time.monotonic() + 30.0
            while True:
                try:
                    lock_dir.mkdir()
                    break
                except FileExistsError:
                    try:
                        if time.time() - lock_dir.stat().st_mtime > 120:
                            lock_dir.rmdir()
                            continue
                    except OSError:
                        pass
                    if time.monotonic() >= deadline:
                        raise HTTPException(status_code=409, detail="Coding workspace is busy; retry the same continuation.")
                    time.sleep(0.05)
            try:
                yield
            finally:
                try:
                    lock_dir.rmdir()
                except OSError:
                    pass

    def _bundle_path(self, session_id: str) -> Path:
        safe = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:40]
        return self.root / f"{safe}.zip"

    def _read(self, session_id: str, *, required: bool = True) -> dict[str, Any] | None:
        self._validate_id(session_id)
        path = self._record_path(session_id)
        with self._lock:
            if not path.is_file():
                if required:
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Coding session not found")
                return None
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Coding session is unreadable") from exc
        if not isinstance(value, dict) or value.get("id") != session_id:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Coding session identity mismatch")
        return value

    def _write(self, record: dict[str, Any]) -> None:
        session_id = str(record.get("id") or "")
        self._validate_id(session_id)
        path = self._record_path(session_id)
        with self._lock:
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True), encoding="utf-8")
            tmp.replace(path)

    @staticmethod
    def _invalid(record: dict[str, Any], reason: str, **extra: Any) -> dict[str, Any]:
        result = dict(record)
        result.update(extra)
        result["valid"] = False
        result["resume_reason"] = reason
        result["status"] = "stale"
        return result

    @staticmethod
    def _validate_id(session_id: str) -> None:
        if not _SESSION_ID_RE.fullmatch(str(session_id or "")):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid coding_session_id")

    @staticmethod
    def _ttl_seconds() -> int:
        return max(300, int(getattr(settings, "coding_session_ttl_seconds", 3600)))

    def _cleanup(self) -> None:
        with self._lock:
            now = time.time()
            records: list[tuple[float, Path, dict[str, Any]]] = []
            for path in self.root.glob("*.json"):
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                    updated = float(value.get("updated_at") or 0)
                    records.append((updated, path, value))
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
            records.sort(key=lambda item: item[0], reverse=True)
            max_sessions = max(2, int(getattr(settings, "coding_session_max_count", 20)))
            for index, (_updated, path, value) in enumerate(records):
                if index < max_sessions and float(value.get("expires_at") or 0) > now:
                    continue
                session_id = str(value.get("id") or "")
                path.unlink(missing_ok=True)
                if _SESSION_ID_RE.fullmatch(session_id):
                    self._bundle_path(session_id).unlink(missing_ok=True)
            existing = [item for item in records if item[1].exists()]
            total = sum(int((value.get("bundle") or {}).get("bytes") or 0) for _updated, _path, value in existing)
            quota = max(10_000_000, int(getattr(settings, "coding_session_max_total_bytes", 250_000_000)))
            for _updated, path, value in reversed(existing):
                if total <= quota:
                    break
                session_id = str(value.get("id") or "")
                size = int((value.get("bundle") or {}).get("bytes") or 0)
                path.unlink(missing_ok=True)
                if _SESSION_ID_RE.fullmatch(session_id):
                    self._bundle_path(session_id).unlink(missing_ok=True)
                total = max(0, total - size)


class CodingWorkspaceService(CodingSessionService):
    """Repo-bound LRU cache of independently validated Odoo module contexts.

    A workspace is intentionally separate from a task coding session. One task
    still owns one module and one exact write contract, while the workspace can
    retain up to ``coding_workspace_max_modules`` module snapshots for later
    tasks in the same checkout. Module entries are revalidated independently,
    so a Git HEAD change does not force unchanged modules to be re-inspected.
    """

    _sync_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="odoo-workspace-sync")
    _sync_futures_lock = RLock()
    _sync_futures: set[Future] = set()

    def __init__(self) -> None:
        self.root = settings.state_path / "coding_workspaces"
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def default_id(*, client_name: str | None, project_path: str) -> str:
        project = str(Path(project_path).expanduser().resolve())
        identity = f"{str(client_name or '').strip().lower()}\0{project.lower()}"
        return "workspace-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]

    def open_or_resume(
        self,
        *,
        workspace_session_id: str | None,
        client_name: str | None,
        project_path: str,
    ) -> dict[str, Any]:
        project = Path(project_path).expanduser().resolve()
        session_id = workspace_session_id or self.default_id(
            client_name=client_name,
            project_path=str(project),
        )
        self._validate_id(session_id)
        now = time.time()
        with self._record_guard(session_id):
            record = self._read(session_id, required=False)
            if record and float(record.get("expires_at") or 0) <= now:
                self._delete_workspace_bundles(record)
                record = None
            if record:
                if str(record.get("project_path") or "") != str(project):
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="workspace_session_id belongs to a different repository checkout",
                    )
                stored_client = str(record.get("client_name") or "")
                if stored_client and stored_client != str(client_name or ""):
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="workspace_session_id belongs to a different Odoo client",
                    )
                record["resume_reason"] = "workspace_current"
            else:
                record = {
                    "id": session_id,
                    "client_name": client_name,
                    "project_path": str(project),
                    "base_sha": self._git_sha(project),
                    "active_module_name": None,
                    "modules": {},
                    "created_at": now,
                    "resume_reason": "workspace_created",
                }
            record["max_modules"] = self._max_modules()
            record["updated_at"] = now
            record["expires_at"] = now + self._workspace_ttl_seconds()
            self._write(record)
            self._cleanup_workspaces()
            return record

    def activate_module(
        self,
        workspace_session_id: str,
        *,
        module_name: str,
        module_path: str,
        validated_fingerprint: str | None = None,
        validated_base_sha: str | None = None,
    ) -> dict[str, Any]:
        with self._record_guard(workspace_session_id):
            record = self._read(workspace_session_id)
            project = Path(str(record["project_path"])).resolve()
            module = Path(module_path).expanduser().resolve()
            self._validate_module(project, module)
            now = time.time()
            current_sha = self._git_sha(project)
            modules = dict(record.get("modules") or {})
            entry = dict(modules.get(module_name) or {})
            evicted_module: str | None = None

            # A resumed coding session has just byte-hashed this exact module and
            # checked its HEAD. Reuse that trusted result when the workspace entry
            # describes the same module instead of immediately reading every file
            # a second time. New tasks and stale/missing entries still perform the
            # full content-derived scan below.
            trusted_current = bool(
                validated_fingerprint
                and validated_base_sha == current_sha
                and entry
                and str(entry.get("module_path") or "") == str(module)
                and str(entry.get("module_fingerprint") or "") == validated_fingerprint
            )
            module_files: list[Path] | None = None
            if trusted_current:
                current_fingerprint = str(validated_fingerprint)
            else:
                module_files = self._module_files(module)
                current_fingerprint = self._module_fingerprint_from_files(module, module_files)

            if entry and (
                str(entry.get("module_path") or "") == str(module)
                and str(entry.get("module_fingerprint") or "") == current_fingerprint
            ):
                cache_hit = True
                activation_status = (
                    "cache_hit" if str(entry.get("base_sha") or "") == current_sha
                    else "rebound_unchanged"
                )
                entry["base_sha"] = current_sha
                entry["status"] = activation_status
            else:
                cache_hit = False
                if not entry and len(modules) >= self._max_modules():
                    evicted_module = min(
                        modules,
                        key=lambda name: float((modules.get(name) or {}).get("last_used_at") or 0),
                    )
                    evicted = modules.pop(evicted_module)
                    self._delete_bundle_from_entry(evicted)
                snapshot = self._snapshot(
                    self._workspace_bundle_key(workspace_session_id, module_name),
                    module_name,
                    module,
                    current_fingerprint,
                    module_files,
                )
                activation_status = "loaded_on_demand" if not entry else "refreshed_source_changed"
                entry = {
                    "module_name": module_name,
                    "module_path": str(module),
                    "module_root_rel": module.relative_to(project).as_posix(),
                    "base_sha": current_sha,
                    "module_fingerprint": snapshot["module_fingerprint"],
                    "bundle": snapshot["bundle"],
                    "bundle_version": int(entry.get("bundle_version") or 0) + 1,
                    "analysis_index": None,
                    "status": activation_status,
                    "created_at": float(entry.get("created_at") or now),
                }

            entry["last_used_at"] = now
            entry["updated_at"] = now
            modules[module_name] = entry
            record.update({
                "base_sha": current_sha,
                "active_module_name": module_name,
                "modules": modules,
                "updated_at": now,
                "expires_at": now + self._workspace_ttl_seconds(),
            })
            self._write(record)
            return {
                "workspace": record,
                "module": deepcopy(entry),
                "cache_hit": cache_hit,
                "status": activation_status,
                "evicted_module": evicted_module,
            }

    def store_analysis(
        self,
        workspace_session_id: str,
        *,
        module_name: str,
        analysis_index: dict[str, Any],
    ) -> dict[str, Any]:
        with self._record_guard(workspace_session_id):
            record = self._read(workspace_session_id)
            modules = dict(record.get("modules") or {})
            if module_name not in modules:
                raise HTTPException(status_code=404, detail="Module is not active in this coding workspace")
            entry = dict(modules[module_name])
            entry["analysis_index"] = deepcopy(analysis_index)
            entry["updated_at"] = time.time()
            entry["last_used_at"] = entry["updated_at"]
            modules[module_name] = entry
            record["modules"] = modules
            record["updated_at"] = entry["updated_at"]
            record["expires_at"] = entry["updated_at"] + self._workspace_ttl_seconds()
            self._write(record)
            return record

    def refresh_module(
        self,
        workspace_session_id: str,
        *,
        module_name: str,
        status_value: str = "refreshed_after_write",
    ) -> dict[str, Any]:
        with self._record_guard(workspace_session_id):
            record = self._read(workspace_session_id)
            modules = dict(record.get("modules") or {})
            entry = dict(modules.get(module_name) or {})
            if not entry:
                raise HTTPException(status_code=404, detail="Module is not active in this coding workspace")
            project = Path(str(record["project_path"])).resolve()
            module = Path(str(entry["module_path"])).resolve()
            self._validate_module(project, module)
            snapshot = self._snapshot(
                self._workspace_bundle_key(workspace_session_id, module_name),
                module_name,
                module,
            )
            now = time.time()
            entry.update({
                "base_sha": self._git_sha(project),
                "module_fingerprint": snapshot["module_fingerprint"],
                "bundle": snapshot["bundle"],
                "bundle_version": int(entry.get("bundle_version") or 0) + 1,
                "analysis_index": None,
                "status": status_value,
                "last_used_at": now,
                "updated_at": now,
            })
            modules[module_name] = entry
            record.update({
                "base_sha": entry["base_sha"],
                "active_module_name": module_name,
                "modules": modules,
                "updated_at": now,
                "expires_at": now + self._workspace_ttl_seconds(),
            })
            self._write(record)
            return record

    def schedule_project_sync(
        self,
        project_path: str,
        pushed_sha: str | None = None,
        changed_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        """Schedule best-effort cache maintenance without delaying a successful push."""
        future = self._sync_executor.submit(
            self.sync_project_after_push,
            project_path,
            pushed_sha,
            changed_paths,
        )
        with self._sync_futures_lock:
            self._sync_futures.add(future)
        future.add_done_callback(self._discard_sync_future)
        return {"status": "scheduled", "base_sha": pushed_sha, "changed_paths": len(changed_paths or [])}

    @classmethod
    def _discard_sync_future(cls, future: Future) -> None:
        with cls._sync_futures_lock:
            cls._sync_futures.discard(future)

    @classmethod
    def wait_for_pending_syncs(cls, timeout: float = 10.0) -> None:
        """Deterministic test/shutdown hook; normal push paths never wait here."""
        with cls._sync_futures_lock:
            pending = list(cls._sync_futures)
        for future in pending:
            future.result(timeout=timeout)

    def sync_project_after_push(
        self,
        project_path: str,
        pushed_sha: str | None = None,
        changed_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        """Rebind every cached module for a checkout after a successful push.

        Unchanged modules retain their analysis. A module whose content changed
        outside the active workflow is snapshotted again and its analysis is
        invalidated. This is best-effort metadata maintenance and never owns Git.
        """
        project = Path(project_path).expanduser().resolve()
        current_sha = pushed_sha or self._git_sha(project)
        checked = 0
        workspace_count = 0
        refreshed: list[str] = []
        rebound: list[str] = []
        stale: list[str] = []
        changed_set = {str(path).replace("\\", "/").strip("/") for path in (changed_paths or []) if str(path).strip()}
        for path in self.root.glob("*.json"):
            try:
                preview = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if str(preview.get("project_path") or "") != str(project):
                continue
            workspace_id = str(preview.get("id") or "")
            if not workspace_id:
                continue
            with self._record_guard(workspace_id):
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if str(record.get("project_path") or "") != str(project):
                    continue
                workspace_count += 1
                modules = dict(record.get("modules") or {})
                changed = False
                for module_name, raw_entry in list(modules.items()):
                    entry = dict(raw_entry or {})
                    module = Path(str(entry.get("module_path") or "")).resolve()
                    checked += 1
                    try:
                        self._validate_module(project, module)
                        module_root_rel = str(entry.get("module_root_rel") or module.relative_to(project).as_posix()).strip("/")
                        touched = not changed_set or any(
                            rel == module_root_rel or rel.startswith(module_root_rel + "/")
                            for rel in changed_set
                        )
                        if not touched:
                            entry["status"] = "rebound_after_push"
                            entry["base_sha"] = current_sha
                            entry["updated_at"] = time.time()
                            rebound.append(module_name)
                            modules[module_name] = entry
                            changed = True
                            continue
                        module_files = self._module_files(module)
                        current_fingerprint = self._module_fingerprint_from_files(module, module_files)
                        if current_fingerprint != str(entry.get("module_fingerprint") or ""):
                            snapshot = self._snapshot(
                                self._workspace_bundle_key(str(record["id"]), module_name),
                                module_name,
                                module,
                                current_fingerprint,
                                module_files,
                            )
                            entry["module_fingerprint"] = snapshot["module_fingerprint"]
                            entry["bundle"] = snapshot["bundle"]
                            entry["bundle_version"] = int(entry.get("bundle_version") or 0) + 1
                            entry["analysis_index"] = None
                            entry["status"] = "refreshed_after_push"
                            refreshed.append(module_name)
                        else:
                            entry["status"] = "rebound_after_push"
                            rebound.append(module_name)
                        entry["base_sha"] = current_sha
                        entry["updated_at"] = time.time()
                    except Exception:  # noqa: BLE001 - one stale module must not block post-push sync
                        entry["status"] = "stale_module_missing"
                        entry["analysis_index"] = None
                        stale.append(module_name)
                    modules[module_name] = entry
                    changed = True
                if changed:
                    record["modules"] = modules
                    record["base_sha"] = current_sha
                    record["updated_at"] = time.time()
                    record["expires_at"] = time.time() + self._workspace_ttl_seconds()
                    self._write(record)
        return {
            "workspace_count": workspace_count,
            "checked_modules": checked,
            "refreshed_modules": sorted(set(refreshed)),
            "rebound_modules": sorted(set(rebound)),
            "stale_modules": sorted(set(stale)),
            "base_sha": current_sha,
        }

    @staticmethod
    def public_summary(record: dict[str, Any] | None) -> dict[str, Any] | None:
        if not record:
            return None
        modules = dict(record.get("modules") or {})
        ordered = sorted(
            modules.values(),
            key=lambda item: float((item or {}).get("last_used_at") or 0),
            reverse=True,
        )
        return {
            "workspace_session_id": record.get("id"),
            "active_module_name": record.get("active_module_name"),
            "base_sha": record.get("base_sha"),
            "module_count": len(modules),
            "max_modules": record.get("max_modules"),
            "modules": [
                {
                    "module_name": item.get("module_name"),
                    "status": item.get("status"),
                    "base_sha": item.get("base_sha"),
                    "bundle_version": item.get("bundle_version"),
                    "bundle_bytes": (item.get("bundle") or {}).get("bytes"),
                    "analysis_cached": isinstance(item.get("analysis_index"), dict),
                    "last_used_at": item.get("last_used_at"),
                }
                for item in ordered
            ],
            "expires_at": record.get("expires_at"),
        }

    @staticmethod
    def _workspace_bundle_key(workspace_session_id: str, module_name: str) -> str:
        return f"{workspace_session_id}:{module_name}"

    @staticmethod
    def _max_modules() -> int:
        return max(1, min(10, int(getattr(settings, "coding_workspace_max_modules", 5))))

    @staticmethod
    def _workspace_ttl_seconds() -> int:
        return max(300, int(getattr(settings, "coding_workspace_ttl_seconds", 14400)))

    def _delete_bundle_from_entry(self, entry: dict[str, Any]) -> None:
        name = str((entry.get("bundle") or {}).get("name") or "")
        if not re.fullmatch(r"[a-f0-9]{40}\.zip", name):
            return
        (self.root / name).unlink(missing_ok=True)

    def _delete_workspace_bundles(self, record: dict[str, Any]) -> None:
        for entry in (record.get("modules") or {}).values():
            if isinstance(entry, dict):
                self._delete_bundle_from_entry(entry)

    def _cleanup_workspaces(self) -> None:
        now = time.time()
        records: list[tuple[float, Path, dict[str, Any]]] = []
        for path in self.root.glob("*.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                records.append((float(value.get("updated_at") or 0), path, value))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        records.sort(key=lambda item: item[0], reverse=True)
        max_workspaces = max(2, int(getattr(settings, "coding_workspace_max_count", 20)))
        for index, (_updated, path, record) in enumerate(records):
            if index < max_workspaces and float(record.get("expires_at") or 0) > now:
                continue
            self._delete_workspace_bundles(record)
            path.unlink(missing_ok=True)
        # Enforce a hard aggregate bundle quota using workspace LRU order.
        existing = [item for item in records if item[1].exists()]
        total = sum(
            int((entry.get("bundle") or {}).get("bytes") or 0)
            for _updated, _path, record in existing
            for entry in (record.get("modules") or {}).values()
            if isinstance(entry, dict)
        )
        quota = max(10_000_000, int(getattr(settings, "coding_workspace_max_total_bytes", 500_000_000)))
        for _updated, path, record in reversed(existing):
            if total <= quota:
                break
            removed = sum(
                int((entry.get("bundle") or {}).get("bytes") or 0)
                for entry in (record.get("modules") or {}).values()
                if isinstance(entry, dict)
            )
            self._delete_workspace_bundles(record)
            path.unlink(missing_ok=True)
            total = max(0, total - removed)

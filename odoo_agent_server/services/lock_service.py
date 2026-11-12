from __future__ import annotations

import shutil
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException, status

from ..config import settings
from ..schemas import CommandResult
from .path_service import resolve_allowed_path


class RepoLockService:
    """Small cross-process repository lock using atomic directory creation.

    Render/Koyeb may run multiple requests in the same process or process group.
    A mkdir lock is simple, portable, and prevents two agent calls from editing,
    committing, or pushing the same checkout at the same time.
    """

    def __init__(self, stale_after_seconds: int = 1800) -> None:
        self.stale_after_seconds = stale_after_seconds

    def _lock_dir(self, project_path: str) -> Path:
        project = resolve_allowed_path(project_path)
        safe = str(project).strip('/').replace('/', '__').replace('\\', '__').replace(':', '_') or 'root'
        root = Path(settings.state_path) / 'locks'
        root.mkdir(parents=True, exist_ok=True)
        return root / f'{safe}.lock'

    def acquire(self, project_path: str, owner: str | None = None, timeout_seconds: int = 60) -> CommandResult:
        lock_dir = self._lock_dir(project_path)
        owner = owner or uuid4().hex
        deadline = time.time() + timeout_seconds
        while True:
            try:
                lock_dir.mkdir(parents=True, exist_ok=False)
                (lock_dir / 'owner').write_text(owner, encoding='utf-8')
                (lock_dir / 'created_at').write_text(str(time.time()), encoding='utf-8')
                return CommandResult(command=['repo.lock', 'acquire'], cwd=str(resolve_allowed_path(project_path)), exit_code=0, stdout=f'LOCK_ACQUIRED\nowner={owner}\nlock={lock_dir}\n')
            except FileExistsError:
                created_at = 0.0
                try:
                    created_at = float((lock_dir / 'created_at').read_text(encoding='utf-8').strip())
                except Exception:
                    created_at = 0.0
                if created_at and time.time() - created_at > self.stale_after_seconds:
                    shutil.rmtree(lock_dir, ignore_errors=True)
                    continue
                if time.time() >= deadline:
                    current_owner = ''
                    try:
                        current_owner = (lock_dir / 'owner').read_text(encoding='utf-8').strip()
                    except Exception:
                        pass
                    return CommandResult(command=['repo.lock', 'acquire'], cwd=str(resolve_allowed_path(project_path)), exit_code=124, stderr=f'Repo lock is busy: {lock_dir} owner={current_owner}')
                time.sleep(0.25)

    def release(self, project_path: str, owner: str | None = None) -> CommandResult:
        lock_dir = self._lock_dir(project_path)
        if not lock_dir.exists():
            return CommandResult(command=['repo.lock', 'release'], cwd=str(resolve_allowed_path(project_path)), exit_code=0, stdout='LOCK_NOT_PRESENT\n')
        if owner:
            try:
                current_owner = (lock_dir / 'owner').read_text(encoding='utf-8').strip()
            except Exception:
                current_owner = ''
            if current_owner and current_owner != owner:
                return CommandResult(command=['repo.lock', 'release'], cwd=str(resolve_allowed_path(project_path)), exit_code=2, stderr=f'Lock owner mismatch: current={current_owner} requested={owner}')
        shutil.rmtree(lock_dir, ignore_errors=True)
        return CommandResult(command=['repo.lock', 'release'], cwd=str(resolve_allowed_path(project_path)), exit_code=0, stdout='LOCK_RELEASED\n')

    @contextmanager
    def locked(self, project_path: str, owner: str | None = None, timeout_seconds: int = 60):
        owner = owner or uuid4().hex
        acquired = self.acquire(project_path, owner=owner, timeout_seconds=timeout_seconds)
        if acquired.exit_code != 0:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=acquired.stderr or 'Repo lock is busy')
        try:
            yield acquired
        finally:
            self.release(project_path, owner=owner)

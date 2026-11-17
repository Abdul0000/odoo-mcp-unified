from __future__ import annotations

import fcntl
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException, status

from ..config import settings
from ..schemas import CommandResult
from .path_service import resolve_allowed_path


_HELD = {}
_HELD_GUARD = threading.Lock()


class RepoLockService:
    """Cross-process advisory lock with kernel-owned lifetime.

    A persistent lock inode prevents stale-directory races. Process death releases
    ownership automatically; elapsed time never evicts a live operation.
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
        deadline = time.monotonic() + max(0, timeout_seconds)
        lock_dir.mkdir(parents=True, exist_ok=True)
        # Never unlink this inode: doing so lets a new waiter lock a different
        # inode while another process still owns the old one. Kernel ownership
        # automatically ends on process death, including partial initialization.
        stream = (lock_dir / 'lease').open('a+')
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with _HELD_GUARD:
                    _HELD[str(lock_dir)] = (owner, stream)
                (lock_dir / 'owner').write_text(owner, encoding='utf-8')
                return CommandResult(command=['repo.lock', 'acquire'], cwd=str(resolve_allowed_path(project_path)), exit_code=0,
                                     stdout=f'LOCK_ACQUIRED\nowner={owner}\nlock={lock_dir}\n')
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    stream.close()
                    return CommandResult(command=['repo.lock', 'acquire'], cwd=str(resolve_allowed_path(project_path)),
                                         exit_code=124, stderr=f'Repo lock is busy: {lock_dir}')
                time.sleep(min(.1, max(0, deadline - time.monotonic())))
            except BaseException:
                with _HELD_GUARD:
                    _HELD.pop(str(lock_dir), None)
                stream.close()
                raise

    def release(self, project_path: str, owner: str | None = None) -> CommandResult:
        lock_dir = self._lock_dir(project_path)
        with _HELD_GUARD:
            entry = _HELD.get(str(lock_dir))
            if entry and owner and entry[0] != owner:
                return CommandResult(command=['repo.lock', 'release'], cwd=str(resolve_allowed_path(project_path)),
                                     exit_code=2, stderr='Lock owner mismatch')
            if entry:
                _HELD.pop(str(lock_dir))
                fcntl.flock(entry[1], fcntl.LOCK_UN)
                entry[1].close()
        return CommandResult(command=['repo.lock', 'release'], cwd=str(resolve_allowed_path(project_path)), exit_code=0,
                             stdout='LOCK_RELEASED\n' if entry else 'LOCK_NOT_PRESENT\n')

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

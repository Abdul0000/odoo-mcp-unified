"""Stdlib helpers behind JsonStore: change signalling for wait_job and cheap job-history pruning."""
from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Condition


class JobSignal:
    """Lets ``wait_job`` wake the instant a job record is written instead of sleeping a full poll interval."""

    def __init__(self, max_tracked: int = 4000) -> None:
        self._cond = Condition()
        self._versions: dict[str, int] = {}
        self._max = max_tracked

    def version(self, job_id: str) -> int:
        with self._cond:
            return self._versions.get(job_id, 0)

    def bump(self, job_id: str) -> None:
        with self._cond:
            if len(self._versions) > self._max:
                self._versions.clear()
                self._versions[job_id] = 1
            else:
                self._versions[job_id] = self._versions.get(job_id, 0) + 1
            self._cond.notify_all()

    def wait_change(self, job_id: str, seen: int, timeout: float) -> bool:
        with self._cond:
            return self._cond.wait_for(lambda: self._versions.get(job_id, 0) != seen, timeout=max(timeout, 0.0))


def prune_job_dir(jobs_dir: Path, maximum: int, active_statuses: set[str]) -> int:
    """Trim terminal job files to 90% of ``maximum`` once the cap is exceeded; active jobs are never removed.

    Directory entries are counted without opening any file, so the common case (under the cap) is O(1)
    reads instead of parsing every job on every write.
    """
    try:
        total = sum(1 for entry in os.scandir(jobs_dir) if entry.name.endswith(".json"))
    except OSError:
        return 0
    if total <= maximum:
        return 0
    keep = max(10, int(maximum * 0.9))
    rows: list[tuple[str, Path, str]] = []
    for path in Path(jobs_dir).glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rows.append((str(record.get("created_at") or ""), path, str(record.get("status") or "")))
    rows.sort(key=lambda item: item[0])
    removed = 0
    for _created, path, status in rows:
        if len(rows) - removed <= keep:
            break
        if status in active_statuses:
            continue
        path.unlink(missing_ok=True)
        removed += 1
    return removed

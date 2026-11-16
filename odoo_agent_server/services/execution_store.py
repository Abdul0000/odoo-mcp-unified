"""Database-free execution state: atomic JSON checkpoints and process locks."""
from __future__ import annotations

from contextlib import contextmanager
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time

try:
    import fcntl
except ImportError:
    fcntl = None

from fastapi import HTTPException
from ..config import settings


class ExecutionStore:
    KINDS = {"workspace", "process", "agent", "publish", "operation"}

    def __init__(self):
        if fcntl is None:
            raise HTTPException(503, "Task execution requires a POSIX host")
        self.root = Path(settings.execution_root).expanduser().resolve()
        self._directory(self.root)
        self._directory(self.root / "records")
        with self._lock(self.root / "queue.lock"):
            self._index()

    @staticmethod
    def _directory(path):
        path.mkdir(parents=True, exist_ok=True, mode=0o2770)
        if path.stat().st_uid == os.geteuid():
            path.chmod(0o2770)
        return path

    def _kind_dir(self, kind):
        if kind not in self.KINDS:
            raise HTTPException(400, "Invalid execution record type")
        return self._directory(self.root / "records" / kind)

    def _path(self, record_id, kind):
        if not re.fullmatch(r"[a-f0-9]{32}", record_id):
            raise HTTPException(404, "Unknown execution record")
        return self._kind_dir(kind) / f"{record_id}.json"

    @staticmethod
    def _read(path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise HTTPException(500, "Execution state is unreadable; inspect worker storage") from exc

    @staticmethod
    def _write(path, value):
        temp = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
                temp = Path(stream.name)
                os.fchmod(stream.fileno(), 0o660)
                stream.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            temp.replace(path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                try:
                    os.fsync(directory)
                except OSError as exc:
                    if exc.errno not in {errno.EINVAL, errno.ENOTSUP}:
                        raise
            finally:
                os.close(directory)
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)

    @contextmanager
    def _lock(self, path):
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o660)
        try:
            if os.fstat(descriptor).st_uid == os.geteuid():
                os.fchmod(descriptor, 0o660)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @contextmanager
    def workspace_lock(self, workspace_id):
        if not re.fullmatch(r"[a-f0-9]{32}", workspace_id):
            raise HTTPException(404, "Unknown execution workspace")
        locks = self._directory(self.root / "locks")
        with self._lock(locks / f"{workspace_id}.lock"):
            yield

    @staticmethod
    def decode(row):
        return {**row["data"], "id": row["id"], "status": row["status"],
                "created_at": row["created"], "updated_at": row["updated"]}

    @staticmethod
    def _identity(kind, key, request):
        request_key = f"{kind}:{key}"
        record_id = hashlib.sha256(request_key.encode()).hexdigest()[:32]
        fingerprint = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        return record_id, request_key, fingerprint

    def _rows(self, kind):
        for path in self._kind_dir(kind).glob("*.json"):
            row = self._read(path)
            if row is not None:
                yield row

    def _apply_index(self, index, row):
        kind, record_id = row["kind"], row["id"]
        live = index["live"].setdefault(kind, {})
        live.pop(record_id, None)
        if row["status"] in {"queued", "running", "initializing"}:
            live[record_id] = {"status": row["status"], "created": row["created"]}
        recent = index["recent"].setdefault(kind, [])
        index["recent"][kind] = [record_id, *[key for key in recent if key != record_id]][:64]

    def _index(self):
        """Caller holds queue.lock. Journal replay repairs a crash between the
        authoritative record write and its small active/recent index update."""
        path = self.root / "indexes.json"
        index = self._read(path)
        if index is None:
            index = {"version": 1, "live": {}, "recent": {}}
            for kind in sorted(self.KINDS):
                for row in sorted(self._rows(kind), key=lambda row: row["updated"]):
                    self._apply_index(index, row)
            self._write(path, index)
        dirty = self.root / "index-journal.json"
        journal = self._read(dirty)
        if journal:
            row = self._read(self._path(journal["id"], journal["kind"]))
            if row:
                self._apply_index(index, row)
                self._write(path, index)
            dirty.unlink(missing_ok=True)
        return index

    def _save_row(self, row):
        index = self._index()
        journal = self.root / "index-journal.json"
        self._write(journal, {"id": row["id"], "kind": row["kind"]})
        self._write(self._path(row["id"], row["kind"]), row)
        self._apply_index(index, row)
        self._write(self.root / "indexes.json", index)
        journal.unlink(missing_ok=True)

    def existing(self, kind, key, request):
        record_id, request_key, fingerprint = self._identity(kind, key, request)
        row = self._read(self._path(record_id, kind))
        if row is None:
            return None
        if row["fingerprint"] != fingerprint or row["request_key"] != request_key:
            from .. import execution_schemas as schemas
            model = {"workspace": schemas.WorkspaceOpen, "process": schemas.ProcessStart,
                     "agent": schemas.AgentStart, "publish": schemas.WorkspacePublish}.get(kind)
            old = row.get("data", {}).get("request", {})
            if row["request_key"] != request_key or model is None or model.model_validate(old).model_dump() != request:
                raise HTTPException(409, "request_id already belongs to a different request")
        return self.decode(row)

    def create(self, kind, key, request, *, status="queued", data=None):
        with self._lock(self.root / "queue.lock"):
            existing = self.existing(kind, key, request)
            if existing:
                return existing, False
            if kind in {"process", "agent"}:
                pending = len(self._index()["live"].get(kind, {}))
                if pending >= settings.execution_max_pending:
                    raise HTTPException(429, "Execution queue is full; poll existing work before submitting more")
            record_id, request_key, fingerprint = self._identity(kind, key, request)
            now = time.time()
            row = {"id": record_id, "kind": kind, "request_key": request_key, "fingerprint": fingerprint,
                   "status": status, "data": {"request": request, **(data or {})}, "created": now, "updated": now}
            self._save_row(row)
            return self.decode(row), True

    def get(self, record_id, kind):
        row = self._read(self._path(record_id, kind))
        if row is None:
            raise HTTPException(404, f"Unknown execution {kind}")
        return self.decode(row)

    def update(self, record_id, kind, *, status=None, **changes):
        with self._lock(self.root / "queue.lock"):
            path = self._path(record_id, kind)
            row = self._read(path)
            if row is None:
                raise HTTPException(404, f"Unknown execution {kind}")
            row["data"].update(changes)
            row.update(status=status or row["status"], updated=time.time())
            self._save_row(row)
            return self.decode(row)

    def claim(self, kind):
        with self._lock(self.root / "queue.lock"):
            live = self._index()["live"].get(kind, {})
            queued = [(key, value) for key, value in live.items() if value["status"] == "queued"]
            if not queued:
                return None
            record_id, _ = min(queued, key=lambda item: item[1]["created"])
            row = self._read(self._path(record_id, kind))
            row.update(status="running", updated=time.time())
            self._save_row(row)
            return self.decode(row)

    def active(self, kind):
        return [row for row in self.pending(kind) if row["status"] == "running"]

    def pending(self, kind):
        with self._lock(self.root / "queue.lock"):
            ids = self._index()["live"].get(kind, {})
            return [self.get(key, kind) for key, value in ids.items() if value["status"] in {"queued", "running"}]

    def recent(self, kind, limit=8):
        with self._lock(self.root / "queue.lock"):
            ids = self._index()["recent"].get(kind, [])[:min(limit, 64)]
            return [self.get(key, kind) for key in ids if self._path(key, kind).exists()]

    def send_input(self, process_id, data, eof):
        with self._lock(self.root / "queue.lock"):
            record = self.get(process_id, "process")
            if record["status"] not in {"running", "queued"}:
                raise HTTPException(409, "Process is not accepting input")
            path = self._directory(self.root / "input") / f"{process_id}.json"
            rows = self._read(path) or []
            if sum(len(row[0]) for row in rows) + len(data) > 64000:
                raise HTTPException(429, "Pending stdin exceeds 64 KB")
            rows.append([data, eof])
            self._write(path, rows)

    def take_input(self, process_id):
        self._path(process_id, "process")
        with self._lock(self.root / "queue.lock"):
            path = self._directory(self.root / "input") / f"{process_id}.json"
            rows = self._read(path) or []
            path.unlink(missing_ok=True)
            return [(row[0], bool(row[1])) for row in rows]

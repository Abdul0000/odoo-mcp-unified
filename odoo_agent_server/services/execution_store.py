"""Transactional, restart-safe queue shared by the API and one execution worker."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
try:
    import fcntl
except ImportError:  # The optional Oracle worker requires a POSIX host.
    fcntl = None
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from fastapi import HTTPException

from ..config import settings


class ExecutionStore:
    def __init__(self):
        if fcntl is None:
            raise HTTPException(503, "Task execution requires a POSIX host")
        self.root = Path(settings.execution_root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o770)
        self.db_path = self.root / "execution.sqlite3"
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS records (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, request_key TEXT UNIQUE NOT NULL,
                fingerprint TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS queue ON records(kind,status,created)")
            db.execute("""CREATE TABLE IF NOT EXISTS input (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, process_id TEXT NOT NULL,
                data TEXT NOT NULL, eof INTEGER NOT NULL)""")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def decode(row):
        return {**json.loads(row["data"]), "id": row["id"], "status": row["status"],
                "created_at": row["created"], "updated_at": row["updated"]}

    def create(self, kind, key, request, *, status="queued", data=None):
        fingerprint = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM records WHERE request_key=?", (f"{kind}:{key}",)).fetchone()
            if row:
                if row["fingerprint"] != fingerprint:
                    raise HTTPException(409, "request_id already belongs to a different request")
                return self.decode(row), False
            if kind in {"process", "agent"}:
                count = db.execute("SELECT count(*) FROM records WHERE kind=? AND status IN ('queued','running')", (kind,)).fetchone()[0]
                if count >= settings.execution_max_pending:
                    raise HTTPException(429, "Execution queue is full; poll existing work before submitting more")
            record_id, now = uuid4().hex, time.time()
            payload = {"request": request, **(data or {})}
            db.execute("INSERT INTO records VALUES (?,?,?,?,?,?,?,?)",
                       (record_id, kind, f"{kind}:{key}", fingerprint, status, json.dumps(payload), now, now))
            return {**payload, "id": record_id, "status": status, "created_at": now, "updated_at": now}, True

    def get(self, record_id, kind):
        with self.connect() as db:
            row = db.execute("SELECT * FROM records WHERE id=? AND kind=?", (record_id, kind)).fetchone()
        if not row:
            raise HTTPException(404, f"Unknown execution {kind}")
        return self.decode(row)

    def existing(self, kind, key, request):
        with self.connect() as db:
            row = db.execute("SELECT * FROM records WHERE request_key=?", (f"{kind}:{key}",)).fetchone()
        if not row:
            return None
        fingerprint = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        if row["fingerprint"] != fingerprint:
            raise HTTPException(409, "request_id already belongs to a different request")
        return self.decode(row)

    @contextmanager
    def workspace_lock(self, workspace_id):
        locks = self.root / "locks"
        locks.mkdir(exist_ok=True, mode=0o770)
        with (locks / f"{workspace_id}.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def pending(self, kind):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM records WHERE kind=? AND status IN ('queued','running')", (kind,)).fetchall()
        return [self.decode(row) for row in rows]

    def update(self, record_id, kind, *, status=None, **changes):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM records WHERE id=? AND kind=?", (record_id, kind)).fetchone()
            if not row:
                raise HTTPException(404, f"Unknown execution {kind}")
            data = json.loads(row["data"])
            data.update(changes)
            db.execute("UPDATE records SET status=?,data=?,updated=? WHERE id=?",
                       (status or row["status"], json.dumps(data), time.time(), record_id))
        return self.get(record_id, kind)

    def claim(self, kind):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM records WHERE kind=? AND status='queued' ORDER BY created LIMIT 1", (kind,)).fetchone()
            if not row:
                return None
            db.execute("UPDATE records SET status='running',updated=? WHERE id=?", (time.time(), row["id"]))
        return self.get(row["id"], kind)

    def active(self, kind):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM records WHERE kind=? AND status='running'", (kind,)).fetchall()
        return [self.decode(row) for row in rows]

    def send_input(self, process_id, data, eof):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM records WHERE id=? AND kind='process'", (process_id,)).fetchone()
            if not row or row[0] not in {"running", "queued"}:
                raise HTTPException(409, "Process is not accepting input")
            pending = db.execute("SELECT coalesce(sum(length(data)),0) FROM input WHERE process_id=?", (process_id,)).fetchone()[0]
            if pending + len(data) > 64000:
                raise HTTPException(429, "Pending stdin exceeds 64 KB")
            db.execute("INSERT INTO input(process_id,data,eof) VALUES (?,?,?)", (process_id, data, eof))

    def take_input(self, process_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT data,eof FROM input WHERE process_id=? ORDER BY seq", (process_id,)).fetchall()
            db.execute("DELETE FROM input WHERE process_id=?", (process_id,))
        return [(r[0], bool(r[1])) for r in rows]

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from ..config import settings


class VerificationProofService:
    """Opaque, short-lived proof that one exact working-tree state was verified."""

    _lock = RLock()

    def __init__(self) -> None:
        self.root = settings.state_path / "verification_proofs"
        self.root.mkdir(parents=True, exist_ok=True)

    def create(
        self,
        *,
        project: Path,
        module_name: str,
        module_root_rel: str,
        paths: list[str],
        verifier_result: dict[str, Any],
        accepted: bool,
    ) -> str:
        proof_id = "vfy_" + uuid4().hex
        record = {
            "id": proof_id,
            "project_path": str(project.resolve()),
            "module_name": module_name,
            "module_root_rel": module_root_rel.strip("/"),
            "paths": sorted(set(paths)),
            "fingerprint": self.fingerprint(project, module_root_rel, paths),
            "accepted": bool(accepted),
            "verifier_result": verifier_result,
            "created_at": time.time(),
        }
        self._write(proof_id, record)
        self._cleanup()
        return proof_id

    def validate(
        self,
        proof_id: str | None,
        *,
        project: Path,
        module_name: str,
        module_root_rel: str,
        paths: list[str],
    ) -> dict[str, Any] | None:
        if not proof_id or not proof_id.startswith("vfy_"):
            return None
        path = self.root / f"{proof_id}.json"
        with self._lock:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
        if time.time() - float(record.get("created_at") or 0) > 3600:
            return None
        expected = {
            "project_path": str(project.resolve()),
            "module_name": module_name,
            "module_root_rel": module_root_rel.strip("/"),
            "paths": sorted(set(paths)),
        }
        if any(record.get(key) != value for key, value in expected.items()):
            return None
        if not record.get("accepted"):
            return None
        if record.get("fingerprint") != self.fingerprint(project, module_root_rel, paths):
            return None
        return record

    @staticmethod
    def fingerprint(project: Path, module_root_rel: str, paths: list[str]) -> str:
        """Hash HEAD plus the complete binary diff for the bounded module scope."""
        project = project.resolve()
        scope = sorted(set(paths)) or [module_root_rel.strip("/")]
        command = ["git", "-C", str(project), "diff", "--binary", "HEAD", "--", *scope]
        diff = subprocess.run(command, capture_output=True, timeout=60, check=False)
        head = subprocess.run(
            ["git", "-C", str(project), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        digest = hashlib.sha256()
        digest.update((head.stdout.strip() if head.returncode == 0 else "untracked").encode("utf-8"))
        digest.update(b"\0"); digest.update("\n".join(scope).encode("utf-8")); digest.update(b"\0")
        digest.update(diff.stdout if diff.returncode == 0 else diff.stderr)
        # Bind actual bytes as well: Git may trust its stat cache when size and
        # mtime were deliberately preserved, while deployment safety cannot.
        for raw in scope:
            target = (project / raw).resolve()
            try:
                target.relative_to(project)
            except ValueError:
                digest.update(b"\0ESCAPED\0"); digest.update(raw.encode("utf-8"))
                continue
            if target.is_file():
                files = [target]
            elif target.is_dir():
                files = sorted(path for path in target.rglob("*") if path.is_file() and ".git" not in path.parts)
            else:
                files = []
                digest.update(b"\0MISSING\0"); digest.update(raw.encode("utf-8"))
            for path in files:
                rel = path.relative_to(project).as_posix()
                digest.update(b"\0F\0"); digest.update(rel.encode("utf-8")); digest.update(b"\0")
                digest.update(path.read_bytes())
        return digest.hexdigest()

    def _write(self, proof_id: str, record: dict[str, Any]) -> None:
        path = self.root / f"{proof_id}.json"
        with self._lock:
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True), encoding="utf-8")
            tmp.replace(path)

    def _cleanup(self) -> None:
        cutoff = time.time() - 3600
        with self._lock:
            files = sorted(self.root.glob("vfy_*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
            for index, path in enumerate(files):
                try:
                    if index >= 200 or path.stat().st_mtime < cutoff:
                        path.unlink(missing_ok=True)
                except OSError:
                    continue

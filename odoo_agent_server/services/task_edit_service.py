"""Prevalidated task edits with compare-and-swap and rollback on write failures."""
import hashlib
from pathlib import Path
import tempfile

from fastapi import HTTPException


def replace_file(path, content, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temp = Path(stream.name)
            stream.write(content)
        temp.chmod(mode)
        temp.replace(path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def apply_edits(service, workspace_id, edits):
    if not edits:
        raise HTTPException(400, "batch requires edits")
    originals, desired = {}, {}

    def reserve(path):
        if path in originals or any(path in p.parents or p in path.parents for p in originals):
            raise HTTPException(400, "Batch edit paths must be distinct and non-overlapping")
        if path.exists() and (not path.is_file() or path.stat().st_size > 2_000_000):
            raise HTTPException(400, "Edit target must be a file of at most 2 MB")
        originals[path] = (path.read_bytes(), path.stat().st_mode & 0o777) if path.exists() else (None, 0o660)
        return originals[path]

    for edit in edits:
        target = service.target(workspace_id, edit.path)
        raw, mode = reserve(target)
        digest = hashlib.sha256(raw).hexdigest() if raw is not None else None
        if digest != edit.expected_sha256:
            raise HTTPException(409, f"Read current file and supply expected_sha256: {edit.path}")
        if edit.operation != "write" and raw is None:
            raise HTTPException(404, f"File not found: {edit.path}")
        if edit.operation == "write":
            if edit.content is None:
                raise HTTPException(400, "write requires content")
            desired[target] = (edit.content.encode(), mode)
        elif edit.operation == "replace":
            if not edit.old_text or edit.new_text is None:
                raise HTTPException(400, "replace requires old_text and new_text")
            old = edit.old_text.encode()
            if raw.count(old) != 1:
                raise HTTPException(409, "Replacement must match exactly once; narrow old_text")
            desired[target] = (raw.replace(old, edit.new_text.encode(), 1), mode)
        elif edit.operation == "delete":
            desired[target] = (None, mode)
        else:
            if not edit.destination:
                raise HTTPException(400, "rename requires destination")
            destination = service.target(workspace_id, edit.destination)
            previous, _ = reserve(destination)
            if previous is not None:
                raise HTTPException(409, "Rename destination already exists; existing files preserved")
            desired[target], desired[destination] = (None, mode), (raw, mode)
    for path, (raw, _) in desired.items():
        if raw is not None and len(raw) > 2_000_000:
            raise HTTPException(413, "Edited file exceeds 2 MB")
        # Resolve every parent before mutating anything, including missing-file creates.
        ancestor = path.parent
        while not ancestor.exists():
            ancestor = ancestor.parent
        if not ancestor.is_dir():
            raise HTTPException(400, "Edit parent is not a directory")
    try:
        for path, (raw, mode) in desired.items():
            if raw is None:
                path.unlink(missing_ok=True)
            else:
                replace_file(path, raw, mode)
    except Exception:
        try:
            for path, (raw, mode) in originals.items():
                if raw is None:
                    path.unlink(missing_ok=True)
                else:
                    replace_file(path, raw, mode)
        except Exception as exc:
            service.store.update(workspace_id, "workspace", status="quarantined", error="Edit rollback failed; inspect task files")
            raise HTTPException(409, "Edit rollback failed; workspace quarantined") from exc
        raise
    service.store.update(workspace_id, "workspace", phase="edit")
    root = service.root(workspace_id).resolve()
    return {"status": "written", "deployment": "not_deployed", "files": [
        {"path": str(path.relative_to(root)), "sha256": hashlib.sha256(raw).hexdigest() if raw is not None else None}
        for path, (raw, _) in desired.items()]}

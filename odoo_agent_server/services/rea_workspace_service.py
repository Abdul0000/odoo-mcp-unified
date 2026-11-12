"""Bounded source snapshots for REA over the stateless GitHub workspace.

The snapshot is rebuilt from the workspace branch/WIP journal on demand and
materialized only in a temporary directory for legacy path-based analyzers.
No persistent checkout or local Odoo source tree is required.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Any, Callable, Iterator

from .workspace_factory import workspace_service


_SKIP_PARTS = {".git", ".mcp-wip", "__pycache__", "static", "tests", "node_modules"}


@contextmanager
def rea_workspace_snapshot(
    ws_id: str, *, max_files: int = 80, max_total_chars: int = 1_500_000,
) -> Iterator[dict[str, Any]]:
    """Yield a bounded addon source snapshot and temporary module directory."""
    if not isinstance(ws_id, str) or not ws_id.startswith("ws_") or len(ws_id) > 512:
        raise ValueError("ws_id must be the value returned by ws_open")
    service = workspace_service()
    workspace = service.get(ws_id)
    file_limit = max(5, min(int(max_files), 120))
    char_limit = max(100_000, min(int(max_total_chars), 2_000_000))
    prefix = f"{workspace.root}/" if workspace.root else ""
    manifest_path = f"{prefix}__manifest__.py"

    with workspace.lock:
        candidates = [
            path for path in workspace.module_paths()
            if path == manifest_path or (
                path.endswith((".py", ".xml"))
                and not any(part in _SKIP_PARTS for part in PurePosixPath(path).parts)
            )
        ]
        candidates.sort(key=lambda path: (0 if path == manifest_path else 1, path))
        selected = candidates[:file_limit]
        omitted = list(candidates[file_limit:])
        workspace.load(selected, strict=False)
        view = workspace.view()
        files: dict[str, str] = {}
        total_chars = 0
        unreadable: list[str] = []
        for absolute in selected:
            raw_relative = absolute[len(prefix):] if prefix and absolute.startswith(prefix) else absolute
            relative = PurePosixPath(raw_relative)
            if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
                unreadable.append(raw_relative)
                continue
            source = view.get(absolute)
            if not isinstance(source, str):
                unreadable.append(raw_relative)
                continue
            size = len(source.encode("utf-8"))
            if total_chars + size > char_limit:
                omitted.append(absolute)
                continue
            files[relative.as_posix()] = source
            total_chars += size

    if "__manifest__.py" not in files:
        raise ValueError("REA workspace snapshot could not read the addon manifest; refresh with ws_open")
    with TemporaryDirectory(prefix="odoo-mcp-rea-") as temp_dir:
        root = Path(temp_dir).resolve()
        for relative, source in files.items():
            target = (root / relative).resolve()
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise ValueError("workspace snapshot contains an unsafe source path") from exc
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source, encoding="utf-8")
        yield {
            "workspace": workspace,
            "module_name": workspace.module,
            "module_path": root,
            "files": files,
            "source": {
                "provider": "github_workspace",
                "client": workspace.client,
                "branch": workspace.branch,
                "base_commit": workspace.base_commit,
                "wip_branch": workspace.wip_branch if workspace.wip_head else None,
            },
            "coverage": {
                "candidate_files": len(candidates),
                "scanned_files": len(files),
                "omitted_files": len(omitted) + len(unreadable),
                "omitted_paths": sorted(set(omitted))[:100],
                "unreadable_paths": unreadable[:100],
                "source_chars": total_chars,
                "complete": not omitted and not unreadable,
            },
        }


def run_rea_workspace(
    ws_id: str, module_name: str, analyzer: Callable[[dict[str, Any]], dict[str, Any]],
    *, max_files: int = 80,
) -> dict[str, Any]:
    """Run one REA analyzer in the caller's worker thread within a temp snapshot."""
    with rea_workspace_snapshot(ws_id, max_files=max_files) as snapshot:
        if snapshot["module_name"] != module_name:
            return {
                "status": "error",
                "error_code": "workspace_module_mismatch",
                "error": "module_name must match the module encoded in ws_id",
                "workspace_module": snapshot["module_name"],
            }
        return analyzer(snapshot)

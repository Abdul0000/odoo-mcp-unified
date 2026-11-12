"""Canonical addon paths and source ownership for Odoo code discovery."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable


_IGNORED_DIRECTORY_NAMES = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "env",
    "node_modules",
}
_IGNORED_DIRECTORY_PREFIXES = {("static", "lib")}
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:/")


def canonical_module_relative_path(
    module_name: str,
    relative_path: str,
    module_root_rel: str | None = None,
) -> str:
    """Return one unambiguous project-relative path inside an addon root.

    Accepted inputs are the discovered project-relative addon path, a path
    prefixed by the technical module name, or a path relative to the addon.
    Absolute paths and traversal segments are rejected instead of being
    silently rewritten to a different in-module file.
    """
    value = str(relative_path or "").replace("\\", "/").strip()
    if not value or "\x00" in value:
        raise ValueError("relative_path must be a non-empty safe path")
    if value.startswith(("/", "//", "~/")) or _WINDOWS_ABSOLUTE_RE.match(value):
        raise ValueError("relative_path must be relative to the repository or addon")

    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("relative_path must not contain empty, '.' or '..' segments")

    root = str(module_root_rel or module_name).replace("\\", "/").strip()
    if not root or root.startswith(("/", "~/")) or _WINDOWS_ABSOLUTE_RE.match(root):
        raise ValueError("module_root_rel must be a safe repository-relative path")
    root_parts = root.split("/")
    if any(part in {"", ".", ".."} for part in root_parts):
        raise ValueError("module_root_rel must not contain empty, '.' or '..' segments")

    if parts[: len(root_parts)] == root_parts:
        return "/".join(parts)
    if parts[0] == module_name:
        parts = parts[1:]
        if not parts:
            return "/".join(root_parts)
    elif module_name in parts[1:]:
        raise ValueError(
            "relative_path contains the module name under an unverified repository prefix; "
            "use the exact discovered module root or an addon-relative path"
        )
    return "/".join([*root_parts, *parts])


def is_ignored_source_path(parts: Iterable[str]) -> bool:
    """Return whether path components belong to generated/vendor trees."""
    normalized = tuple(str(part).casefold() for part in parts)
    if any(part in _IGNORED_DIRECTORY_NAMES for part in normalized):
        return True
    return any(
        normalized[index : index + len(prefix)] == prefix
        for prefix in _IGNORED_DIRECTORY_PREFIXES
        for index in range(0, len(normalized) - len(prefix) + 1)
    )


def module_name_for_repo_relative(project: Path, relative_path: str) -> str | None:
    """Resolve the owning addon by walking to its nearest manifest."""
    value = str(relative_path or "").replace("\\", "/").strip()
    if not value or "\x00" in value or value.startswith(("/", "~/")) or _WINDOWS_ABSOLUTE_RE.match(value):
        return None
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return None

    project = project.resolve()
    target = (project / Path(*parts)).resolve()
    if target != project and project not in target.parents:
        return None
    current = target if target.is_dir() else target.parent
    while current != project and project in current.parents:
        if (current / "__manifest__.py").is_file() or (current / "__openerp__.py").is_file():
            return current.name
        current = current.parent
    return None


def source_ownership(path: str | Path, project: Path | None = None) -> str:
    """Classify a discovered Odoo source without granting write permission."""
    resolved = Path(path).resolve()
    if project is not None:
        root = project.resolve()
        if resolved == root or root in resolved.parents:
            return "client_custom"
    normalized = resolved.as_posix().casefold()
    if "/src/enterprise/" in normalized:
        return "enterprise_readonly"
    if "/src/odoo/addons/" in normalized or "/src/odoo/odoo/addons/" in normalized:
        return "community_core_readonly"
    if "/src/user/" in normalized:
        return "related_custom_readonly"
    return "external_readonly"

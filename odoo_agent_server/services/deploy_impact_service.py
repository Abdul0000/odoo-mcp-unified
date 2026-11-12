from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable


@dataclass(frozen=True)
class DeployImpactResult:
    impact: str
    upgrade_required: bool
    reasons: tuple[str, ...]
    changed_files: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "impact": self.impact,
            "upgrade_required": self.upgrade_required,
            "reasons": list(self.reasons),
            "changed_files": list(self.changed_files),
        }


_STATIC_EXTENSIONS = {
    ".js", ".css", ".scss", ".sass", ".less", ".xml", ".svg", ".png",
    ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".woff", ".woff2", ".ttf",
}
_FORCE_UPGRADE_DIRS = {"views", "data", "security", "report", "reports", "wizard", "demo", "i18n", "migrations", "upgrades"}
_FORCE_UPGRADE_FILES = {"__manifest__.py", "__openerp__.py", "__init__.py", "ir.model.access.csv"}
_PYTHON_STRUCTURAL_PATTERNS = [
    r"\bfields\.[A-Z][A-Za-z0-9_]*\s*\(",
    r"^\s*_(?:name|inherit|inherits|table|rec_name|order|auto|sql_constraints)\s*=",
    r"@api\.(?:depends|depends_context|constrains|onchange|model_create_multi)\b",
    r"\b(?:compute|inverse|search|related|comodel_name|store|required|readonly|index|company_dependent|translate|selection)\s*=",
    r"\b(?:pre_init_hook|post_init_hook|uninstall_hook|post_load)\b",
    r"\b(?:CREATE|ALTER|DROP)\s+(?:TABLE|INDEX|TYPE|VIEW)\b",
]


def _normalise_path(path: str) -> str:
    return PurePosixPath(str(path).replace("\\", "/")).as_posix().lstrip("./")


def _diff_changed_lines(diff_text: str, relative_path: str) -> list[str]:
    target = _normalise_path(relative_path)
    lines: list[str] = []
    active = False
    for raw in (diff_text or "").splitlines():
        if raw.startswith("diff --git "):
            active = raw.endswith(f" a/{target} b/{target}") or f" b/{target}" in raw
            continue
        if not active or raw.startswith(("+++", "---", "@@")):
            continue
        if raw[:1] in {"+", "-"}:
            lines.append(raw[1:])
    return lines


def _python_requires_upgrade(path: str, diff_text: str) -> tuple[bool, str]:
    changed = _diff_changed_lines(diff_text, path)
    if not changed:
        return True, "Python diff could not be classified safely"
    joined = "\n".join(changed)
    for pattern in _PYTHON_STRUCTURAL_PATTERNS:
        if re.search(pattern, joined, flags=re.IGNORECASE | re.MULTILINE):
            return True, "Python model/registry/schema metadata changed"
    return False, "Only Python executable method logic changed"


def classify_deploy_impact(
    *,
    project_path: str | Path,
    module_name: str,
    changed_paths: Iterable[str],
    git_diff: str,
    deploy_mode: str = "auto",
) -> DeployImpactResult:
    paths = tuple(dict.fromkeys(_normalise_path(path) for path in changed_paths if str(path).strip()))
    reasons: list[str] = []
    upgrade_required = False
    impact = "source_only"

    if not paths:
        return DeployImpactResult("module_upgrade", True, ("No changed files were supplied; conservative fallback",), paths)

    for path in paths:
        p = PurePosixPath(path)
        name = p.name
        parts = set(p.parts)
        suffix = p.suffix.lower()
        module_relative = path.split(f"{module_name}/", 1)[-1] if f"{module_name}/" in path else path
        rel_parts = PurePosixPath(module_relative).parts

        if name in _FORCE_UPGRADE_FILES or suffix in {".csv", ".po", ".pot"}:
            upgrade_required = True
            reasons.append(f"{path}: module metadata, imports, translations, or CSV data changed")
            continue
        if any(part in _FORCE_UPGRADE_DIRS for part in rel_parts[:-1]):
            if "static" not in rel_parts:
                upgrade_required = True
                reasons.append(f"{path}: database-backed XML/data/security/migration content changed")
                continue
        if "static" in parts and "src" in parts and suffix in _STATIC_EXTENSIONS:
            reasons.append(f"{path}: existing static frontend asset changed")
            if impact == "source_only":
                impact = "asset_only"
            continue
        if suffix == ".py":
            required, reason = _python_requires_upgrade(path, git_diff)
            reasons.append(f"{path}: {reason}")
            if required:
                upgrade_required = True
            elif impact not in {"asset_only"}:
                impact = "python_restart_only"
            continue
        upgrade_required = True
        reasons.append(f"{path}: unknown or structural file type; conservative upgrade")

    if upgrade_required:
        impact = "module_upgrade"

    mode = (deploy_mode or "auto").strip().lower()
    if mode == "always_upgrade":
        upgrade_required = True
        impact = "module_upgrade"
        reasons.append("Deployment mode explicitly forces module upgrade")
    elif mode == "source_only" and upgrade_required:
        raise ValueError("source_only cannot bypass a structural/database/registry change")

    return DeployImpactResult(impact, upgrade_required, tuple(dict.fromkeys(reasons)), paths)

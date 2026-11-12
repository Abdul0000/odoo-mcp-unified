from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException, status

from ..config import settings


def resolve_allowed_path(path: str) -> Path:
    resolved = Path(path).expanduser().resolve()
    allowed_roots = settings.allowed_roots
    if not allowed_roots:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No allowed roots configured",
        )
    if not any(resolved == root or root in resolved.parents for root in allowed_roots):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Path is outside allowed roots: {resolved}",
        )
    return resolved

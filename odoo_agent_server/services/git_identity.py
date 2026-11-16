"""Compare Git identities without conflating unrelated same-name repositories."""
from pathlib import Path
import re
from urllib.parse import urlsplit


def repo_identity(url: str) -> tuple[str, str]:
    url = url.strip().rstrip("/")
    if "://" in url:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if parsed.port and parsed.port not in {22, 80, 443}:
            host += f":{parsed.port}"
        path = parsed.path.strip("/")
        if parsed.scheme == "file":
            return "local", str(Path(parsed.path).resolve())
    else:
        match = re.fullmatch(r"(?:[^@/:]+@)?([^/:]+):(.+)", url)
        if match:
            host, path = match.group(1).lower(), match.group(2).strip("/")
        else:
            return "local", str(Path(url).resolve())
    path = path.removesuffix(".git")
    if host in {"github.com", "www.github.com"}:
        host, path = "github.com", path.lower()
    return host, path


def same_repository(left: str, right: str) -> bool:
    try:
        return bool(left and right) and repo_identity(left) == repo_identity(right)
    except ValueError:
        return False

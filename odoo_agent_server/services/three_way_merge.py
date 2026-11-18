"""Conservative text merges shared by GitHub and GitLab workspaces."""

from pathlib import Path
import os
import subprocess
import tempfile


def merge_text(base, ours, theirs):
    if ours == theirs or theirs == base:
        return ours, False
    if ours == base:
        return theirs, False
    # Add/add and modify/delete require an explicit decision.
    if any(value is None for value in (base, ours, theirs)):
        return None, True
    if any("\0" in value or len(value.encode()) > 2_000_000 for value in (base, ours, theirs)):
        return None, True
    with tempfile.TemporaryDirectory() as directory:
        paths = [Path(directory) / name for name in ("ours", "base", "theirs")]
        for path, value in zip(paths, (ours, base, theirs)):
            path.write_bytes(value.encode())
        result = subprocess.run(
            ["git", "merge-file", "-p", *map(str, paths)],
            env={"PATH": os.defpath, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"},
            capture_output=True, timeout=15,
        )
        return (result.stdout.decode(), False) if result.returncode == 0 else (None, True)

#!/usr/bin/env python3
"""Root-owned sweeper for already-expired execution directories only.

Never imports app/source code or runs Git. Directory FDs and O_NOFOLLOW keep
untrusted symlink swaps inside the fixed execution storage boundary.
"""

import json
import os
from pathlib import Path
import re
import shutil
import stat


def purge(root=Path("/var/lib/odoo-mcp/execution")):
    if not shutil.rmtree.avoids_symlink_attacks:
        raise RuntimeError("Host lacks safe directory deletion support")
    descriptors = []
    removed = 0
    try:
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        descriptors.append(fd)
        for component in Path(root).absolute().parts[1:]:
            fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            descriptors.append(fd)
        records = os.open("records", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        descriptors.append(records)
        records = os.open("workspace", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=records)
        descriptors.append(records)
        workspaces = os.open("workspaces", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        descriptors.append(workspaces)
        for name in os.listdir(records):
            if not re.fullmatch("[a-f0-9]{32}\\.json", name):
                continue
            try:
                item = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=records)
                with os.fdopen(item) as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_size > 1024:
                        continue
                    row = json.load(stream)
                wid = name[:-5]
                if not isinstance(row, dict):
                    continue
                if row.get("id") != wid or row.get("kind") != "workspace" or row.get("status") != "expired" or row.get("data") != {}:
                    continue
                info = os.stat(wid, dir_fd=workspaces, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    os.unlink(wid, dir_fd=workspaces)
                elif stat.S_ISDIR(info.st_mode):
                    shutil.rmtree(wid, dir_fd=workspaces)
                else:
                    continue
                removed += 1
            except (OSError, ValueError):
                continue
    except FileNotFoundError:
        pass
    finally:
        for fd in reversed(descriptors):
            os.close(fd)
    return {"expired_directories_removed": removed}


if __name__ == "__main__":
    print(json.dumps(purge()))

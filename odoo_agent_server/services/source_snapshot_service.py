"""Pruned source walks and bounded incremental content digests for inspection.

Final publish authorization still uses Git's exact prospective commit tree.
"""
from __future__ import annotations

import hashlib
import os
from collections import OrderedDict
from pathlib import Path
from threading import Lock

_hashes: OrderedDict[tuple, bytes] = OrderedDict()
_guard = Lock()


def source_files(root: Path, ignored=()):
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in ignored and not Path(directory, d).is_symlink())
        for name in sorted(files):
            path = Path(directory, name)
            if not path.is_symlink() and path.is_file():
                yield path


def content_digest(path: Path) -> bytes:
    st = path.stat()
    key = (str(path), st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_mode)
    with _guard:
        cached = _hashes.get(key)
        if cached is not None:
            _hashes.move_to_end(key)
            return cached
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(65536), b''):
            digest.update(chunk)
    # Never cache a digest read across a concurrent modification.
    after = path.stat()
    if (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise OSError('Source changed during inspection; retry the focused read')
    value = digest.digest()
    with _guard:
        _hashes[key] = value
        while len(_hashes) > 4096:
            _hashes.popitem(last=False)
    return value

"""Pruned source walks and bounded incremental content digests for inspection.

Final publish authorization still uses Git's exact prospective commit tree.
"""
from __future__ import annotations

import hashlib
import os
import stat as stat_types
from collections import OrderedDict
from pathlib import Path
from threading import Lock

_hashes: OrderedDict[tuple, bytes] = OrderedDict()
_directories: OrderedDict[str, tuple[tuple, tuple, tuple]] = OrderedDict()
_guard = Lock()


def _directory_entries(path: Path):
    """Reuse directory inventories; directory ctime catches preserved-mtime edits."""
    stat = path.stat()
    signature = (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns)
    key = str(path)
    with _guard:
        cached = _directories.get(key)
        if cached and cached[0] == signature:
            _directories.move_to_end(key)
            return cached[1], cached[2]
    dirs, files = [], []
    with os.scandir(path) as entries:
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                dirs.append(entry.name)
            elif entry.is_file(follow_symlinks=False):
                files.append(entry.name)
    result = (tuple(sorted(dirs)), tuple(sorted(files)))
    after = path.stat()
    if signature == (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns):
        with _guard:
            _directories[key] = (signature, *result)
            while len(_directories) > 4096:
                _directories.popitem(last=False)
    return result


def source_files(root: Path, ignored=()):
    pending = [root]
    while pending:
        directory = pending.pop()
        if directory.is_symlink():
            continue
        try:
            dirs, files = _directory_entries(directory)
        except OSError:
            continue
        pending.extend(directory / name for name in reversed(dirs) if name not in ignored)
        for name in files:
            path = directory / name
            try:
                if stat_types.S_ISREG(path.lstat().st_mode):
                    yield path
            except OSError:
                continue


def content_digest(path: Path) -> bytes:
    return _content_digest(path, path.stat())


def content_snapshot(path: Path) -> tuple[int, bytes]:
    """Capture mode and bytes from one fresh stat; never follow a source symlink."""
    st = path.lstat()
    if not stat_types.S_ISREG(st.st_mode):
        raise OSError("Source is no longer a regular file")
    return st.st_mode, _content_digest(path, st)


def _content_digest(path: Path, st) -> bytes:
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
        while len(_hashes) > 16384:
            _hashes.popitem(last=False)
    return value

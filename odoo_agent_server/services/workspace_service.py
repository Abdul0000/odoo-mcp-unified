"""Stateless-first coding workspace for Odoo addons.

A workspace is an overlay of edits on top of one commit of a GitHub or GitLab branch.
Nothing depends on local disk: file contents are fetched through the configured Git provider API,
edits live in memory *and* in a journal that is checkpointed to a WIP branch
(``mcp/wip/<client>-<module>``), so a Render restart loses nothing - the next
call rebuilds the workspace by replaying the journal on the current branch head.

Every edit is linted instantly (python/xml/xpath/js/csv/manifest/imports) and
mechanical closure work (``from . import x`` and manifest ``data`` entries) is
auto-applied instead of being left to fail the install later.
"""
from __future__ import annotations

import base64
import difflib
import fnmatch
import hashlib
import json
import os
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from . import lint_service as lint
from . import outline_service, scaffold_service, symbol_service
from concurrent.futures import ThreadPoolExecutor

from .github_commit_service import (
    FileChange,
    GitHubError,
    GitHubRepo,
    NonFastForward,
    git_blob_sha,
    operation_key,
    push_changes,
)

TEXT_EXT = (".py", ".xml", ".csv", ".js", ".mjs", ".json", ".scss", ".css", ".md", ".txt", ".po", ".pot", ".html", ".yml", ".yaml", ".cfg", ".rst")
MAX_FILE_BYTES = 400_000
MAX_EAGER_BYTES = 3_000_000
PACKAGE_DIRS = {"models", "controllers", "wizard", "wizards", "report", "reports", "tests", "populate", "lib", "tools", "utils", "services"}
DATA_DIRS = {"views", "security", "data", "report", "reports", "wizard", "wizards", "templates", "demo"}
SKIP_IMPORT_DIRS = {"static", "migrations", "i18n", "data", "views", "security", "demo", "doc", "docs"}
PROTECTED_DEFAULT = "main,master,production,prod,live"
_CKPT_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ws-ckpt")
JOURNAL_PREFIX = ".mcp-wip-"
MAX_WIP_FILES = 60
MAX_WIP_BYTES = 2_000_000


class WorkspaceError(RuntimeError):
    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra

    def as_dict(self) -> dict[str, Any]:
        return {"status": "error", "error_code": self.code, "error": self.message, **self.extra}


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "x"


def encode_ws_id(client: str, module: str, branch: str) -> str:
    raw = json.dumps([client, module, branch], separators=(",", ":")).encode()
    return "ws_" + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_ws_id(ws_id: str) -> tuple[str, str, str]:
    if not ws_id.startswith("ws_"):
        raise WorkspaceError("ws_id_invalid", "ws_id must be the value returned by ws_open")
    body = ws_id[3:]
    try:
        client, module, branch = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except Exception as exc:  # noqa: BLE001
        raise WorkspaceError("ws_id_invalid", "ws_id could not be decoded; call ws_open again") from exc
    return client, module, branch


# ----------------------------------------------------------------------- edit application
def _closest(text: str, needle: str, limit: int = 3) -> list[dict[str, Any]]:
    """Nearest windows to ``needle``. Prefilters by the first line (difflib's cheap ratio bounds),
    so a miss on a 6,000-line file costs milliseconds instead of a full window scan."""
    lines = text.splitlines()
    want = needle.strip("\n").splitlines() or [needle]
    first = want[0].strip()
    if not first or not lines:
        return []
    n = max(1, min(len(want), 12))
    probe = "\n".join(want[:n])
    positions: dict[str, list[int]] = {}
    for i, row in enumerate(lines):
        positions.setdefault(row.strip(), []).append(i)
    scored = []
    for cand in difflib.get_close_matches(first, list(positions), n=8, cutoff=0.6):
        for i in positions[cand][:3]:
            window = "\n".join(lines[i : i + n])
            scored.append((difflib.SequenceMatcher(None, probe, window).ratio(), i))
    scored.sort(reverse=True)
    return [{"line": i + 1, "similarity": round(r, 2), "text": "\n".join(lines[i : i + n])[:400]} for r, i in scored[:limit]]


def _lead(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _heal_replace(current: str, search: str, replacement: str) -> str | None:
    """Apply a replace whose search differs from the file only by per-line leading/trailing
    whitespace (the usual agent indentation slip). Only a UNIQUE match is healed; the
    replacement is re-indented to the file's own indentation. Returns the new text or None."""
    want = [row.strip() for row in search.strip("\n").splitlines()]
    if not want or not any(want):
        return None
    lines = current.splitlines(keepends=True)
    stripped = [row.strip() for row in lines]
    n = len(want)
    hits = [i for i in range(len(lines) - n + 1) if stripped[i : i + n] == want]
    if len(hits) != 1:
        return None
    i = hits[0]
    block = "".join(lines[i : i + n])
    eol = "\r\n" if block.endswith("\r\n") else "\n" if block.endswith("\n") else ""
    want_rows = search.strip("\n").splitlines()
    search_indent = next((_lead(r) for r in want_rows if r.strip()), "")
    file_indent = _lead(lines[i])
    out = []
    for row in replacement.strip("\n").splitlines():
        if not row.strip():
            out.append("")
        elif search_indent and row.startswith(search_indent):
            out.append(file_indent + row[len(search_indent):])
        elif not search_indent:
            out.append(file_indent + row)
        else:
            out.append(row)
    new_block = (eol.join(out) + eol) if out else ""
    return "".join(lines[:i]) + new_block + "".join(lines[i + n :])


def infer_op(edit: dict[str, Any]) -> str | None:
    return edit.get("op") or ("write" if edit.get("content") is not None else "replace" if edit.get("search") is not None else None)


def apply_edit(current: str | None, edit: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    """Return (new_text_or_None_for_delete, error). ``current`` is None when the file is absent."""
    text, err, _healed = apply_edit_ex(current, edit)
    return text, err


def apply_edit_ex(current: str | None, edit: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None, bool]:
    """Like apply_edit, plus ``healed``: True when a whitespace-only search mismatch was repaired."""
    text, err = _apply_edit(current, edit)
    if err and err.get("code") == "no_match" and current is not None and not edit.get("regex"):
        healed = _heal_replace(current, edit.get("search") or "", edit.get("replace", ""))
        if healed is not None:
            return healed, None, True
    return text, err, False


def _apply_edit(current: str | None, edit: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    op = edit.get("op") or ("write" if edit.get("content") is not None else "replace" if edit.get("search") is not None else None)
    path = edit["path"]
    if op == "delete":
        if current is None:
            return None, {"code": "not_found", "msg": f"{path} does not exist"}
        return None, None
    if op == "ensure_import":  # replayable wiring op produced by autofix
        line = f"from . import {edit['name']}"
        if current is None:
            return line + "\n", None
        if _package_imports(current, edit["name"]):
            return current, None
        return current + ("" if current.endswith("\n") or not current else "\n") + line + "\n", None
    if op == "remove_import":  # replayable wiring op produced by autofix
        if current is None:
            return None, None
        return _remove_import(current, edit["name"]), None
    if op == "manifest_remove":  # replayable wiring op produced by autofix
        if current is None:
            return None, None
        return manifest_remove(current, edit["entry"]), None
    if op == "manifest_add":  # replayable wiring op produced by autofix
        if current is None:
            return None, {"code": "not_found", "msg": f"{path} does not exist"}
        updated = manifest_add(current, edit["key"], edit["entry"], front_security=bool(edit.get("front_security")))
        if updated is None:
            return None, {"code": "manifest_unparseable", "msg": "could not safely edit the manifest; add the entry manually"}
        return updated, None
    if op == "write":
        if edit.get("content") is None:
            return None, {"code": "missing_content", "msg": "write needs content"}
        return edit["content"], None
    if op == "append":
        text = edit.get("content")
        if text is None:
            return None, {"code": "missing_content", "msg": "append needs content"}
        base = current or ""
        if base and not base.endswith("\n"):
            base += "\n"
        return base + text, None
    if op != "replace":
        return None, {"code": "bad_op", "msg": f"unknown op '{op}' (use replace, write, append, delete)"}
    if current is None:
        return None, {"code": "not_found", "msg": f"{path} does not exist; use op=write to create it"}
    search = edit.get("search")
    if search is None or search == "":
        return None, {"code": "missing_search", "msg": "replace needs a non-empty search"}
    replacement = edit.get("replace", "")
    expect = int(edit.get("expect", 1))
    if edit.get("regex"):
        try:
            pattern = re.compile(search, re.M)
        except re.error as exc:
            return None, {"code": "bad_regex", "msg": str(exc)}
        hits = list(pattern.finditer(current))
        if not hits:
            return None, {"code": "no_match", "msg": "regex matched nothing", "closest": _closest(current, search)}
        if expect and len(hits) != expect:
            return None, {"code": "match_count", "msg": f"regex matched {len(hits)} places, expected {expect}", "lines": [current.count("\n", 0, h.start()) + 1 for h in hits[:10]]}
        return pattern.sub(replacement, current, count=0 if expect == 0 else expect), None
    needle = search
    if needle not in current and "\r\n" in current and "\r\n" not in needle:
        needle = needle.replace("\n", "\r\n")
        replacement = replacement.replace("\r\n", "\n").replace("\n", "\r\n")
    count = current.count(needle)
    if count == 0:
        return None, {"code": "no_match", "msg": "search text not found (must match exactly, including indentation)", "closest": _closest(current, search)}
    if expect and count != expect:
        starts, pos = [], 0
        while len(starts) < 10:
            idx = current.find(needle, pos)
            if idx < 0:
                break
            starts.append(current.count("\n", 0, idx) + 1)
            pos = idx + max(1, len(needle))
        return None, {"code": "match_count", "msg": f"search found {count} places, expected {expect}; add surrounding lines to make it unique or set expect=0 to replace all", "lines": starts}
    return current.replace(needle, replacement) if expect == 0 else current.replace(needle, replacement, expect), None


# ----------------------------------------------------------------------- manifest editing
def _find_list_span(text: str, key: str) -> tuple[int, int] | None:
    m = re.search(r"""(['"])%s\1\s*:\s*\[""" % re.escape(key), text)
    if not m:
        return None
    start = m.end() - 1
    depth, i, quote = 0, start, None
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 1
            elif ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "#":
            i = text.find("\n", i)
            if i < 0:
                return None
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return start, i
        i += 1
    return None


def manifest_add(text: str, key: str, entry: str, *, front_security: bool = False) -> str | None:
    """Insert ``entry`` into manifest list ``key``. Returns None when it cannot be done safely."""
    span = _find_list_span(text, key)
    if span is None:
        m = re.search(r"\}\s*$", text)
        if not m:
            return None
        body = text[: m.start()].rstrip()
        sep = "" if body.endswith(",") or body.endswith("{") else ","
        return f"{body}{sep}\n    '{key}': [\n        '{entry}',\n    ],\n{text[m.start():]}"
    start, end = span
    inner = text[start + 1 : end]
    if re.search(r"""(['"])%s\1""" % re.escape(entry), inner):
        return text
    indent_m = re.search(r"\n([ \t]+)['\"]", inner)
    indent = indent_m.group(1) if indent_m else "        "
    if front_security:
        entries = list(re.finditer(r"""(['"])([^'"]+)\1""", inner))
        for found in entries:
            if not found.group(2).startswith("security/"):
                at = start + 1 + found.start()
                line_start = text.rfind("\n", 0, at) + 1
                lead = text[line_start:at]
                if lead.strip() == "":
                    return text[:line_start] + f"{lead}'{entry}',\n" + text[line_start:]
                return text[:at] + f"'{entry}', " + text[at:]
    if "\n" in inner:
        closing = re.search(r"\n([ \t]*)$", inner)
        close_indent = closing.group(1) if closing else ""
        body = inner.rstrip()
        comma = "" if not body.strip() or body.endswith(",") else ","
        new_inner = f"{body}{comma}\n{indent}'{entry}',\n{close_indent}"
    else:
        body = inner.rstrip()
        sep = ", " if body else ""
        new_inner = f"{body}{sep}'{entry}'"
    return text[: start + 1] + new_inner + text[end:]


def manifest_remove(text: str, entry: str) -> str:
    """Remove one quoted entry from any manifest list (line-wise, keeps formatting and comments)."""
    quoted = re.compile(r"""^[ \t]*(['"])%s\1[ \t]*,?[ \t]*(#.*)?\r?\n""" % re.escape(entry), re.M)
    updated, count = quoted.subn("", text, count=1)
    if count:
        return updated
    inline = re.compile(r"""(['"])%s\1\s*,?\s*""" % re.escape(entry))
    return inline.sub("", text, count=1)


def _remove_import(init_text: str, name: str) -> str:
    out = []
    for row in init_text.splitlines(True):
        m = re.match(r"^(\s*from\s+\.\s+import\s+)([^#\n]+?)(\s*(?:#.*)?)(\r?\n?)$", row)
        if m:
            names = [n.strip() for n in m.group(2).split(",") if n.strip()]
            if name in names:
                names.remove(name)
                if names:
                    out.append(f"{m.group(1)}{', '.join(names)}{m.group(3)}{m.group(4)}")
                continue
        out.append(row)
    return "".join(out)


# ----------------------------------------------------------------------- autofix
def _package_imports(init_text: str, name: str) -> bool:
    return bool(re.search(r"^\s*from\s+\.\s+import\s+[^#\n]*\b%s\b" % re.escape(name), init_text, re.M) or re.search(r"^\s*from\s+\.%s\s+import" % re.escape(name), init_text, re.M) or re.search(r"^\s*import\s+%s\b" % re.escape(name), init_text, re.M))


def plan_autofix(read, exists, new_paths: list[str], root: str, deleted_paths: list[str] | None = None) -> list[dict[str, Any]]:
    """Return replayable wiring ops for files created or deleted in this batch.

    ``read(path)`` returns current text (or None), ``exists(path)`` says whether the path is present.
    Only *new* files are wired and only *deleted* files are unwired: editing an existing file never
    touches imports or the manifest.
    """
    ops: list[dict[str, Any]] = []
    prefix = f"{root}/" if root else ""
    staged: dict[str, str | None] = {}
    manifest_path = prefix + "__manifest__.py"

    def cur(path: str) -> str | None:
        return staged[path] if path in staged else read(path)

    def present(path: str) -> bool:
        return (staged[path] is not None) if path in staged else exists(path)

    def run(op: dict[str, Any]) -> bool:
        path = op["path"]
        before = cur(path)
        new_text, err = apply_edit(before, op)
        if err or new_text == before:
            return False
        staged[path] = new_text
        ops.append(op)
        return True

    def wire_import(init_path: str, child: str) -> bool:
        """Ensure init_path imports child. Returns True when init_path itself was created."""
        existed = present(init_path)
        if existed and _package_imports(cur(init_path) or "", child):
            return False
        run({"op": "ensure_import", "path": init_path, "name": child, "auto": True})
        return not existed

    def relevant(rel_parts: list[str], path: str) -> bool:
        if rel_parts[0] in SKIP_IMPORT_DIRS and not (len(rel_parts) > 1 and rel_parts[0] == "tests"):
            return path.endswith((".xml", ".csv")) and rel_parts[0] in DATA_DIRS and len(rel_parts) >= 2
        return True

    for path in new_paths:
        if (root and not path.startswith(prefix)) or not present(path):
            continue
        rel = path[len(prefix):]
        parts = rel.split("/")
        if not relevant(parts, path):
            continue
        if path.endswith(".py") and not path.endswith(("__init__.py", "__manifest__.py")):
            stem = parts[-1][:-3]
            if stem.startswith("_"):
                continue
            pkg, child = parts[:-1], stem
            while True:
                created = wire_import(prefix + "/".join(pkg + ["__init__.py"]), child)
                if not pkg or not created:
                    break
                child, pkg = pkg[-1], pkg[:-1]
        elif path.endswith((".xml", ".csv")) and parts[0] in DATA_DIRS and len(parts) >= 2:
            if cur(manifest_path) is None:
                continue
            key = "demo" if parts[0] == "demo" else "data"
            run({"op": "manifest_add", "path": manifest_path, "key": key, "entry": rel, "front_security": parts[0] == "security", "auto": True})

    for path in deleted_paths or []:
        if root and not path.startswith(prefix):
            continue
        rel = path[len(prefix):]
        parts = rel.split("/")
        if path.endswith(".py") and not path.endswith(("__init__.py", "__manifest__.py")) and len(parts) >= 1 and parts[0] not in SKIP_IMPORT_DIRS:
            init_path = prefix + "/".join(parts[:-1] + ["__init__.py"])
            if present(init_path):
                run({"op": "remove_import", "path": init_path, "name": parts[-1][:-3], "auto": True})
        elif path.endswith((".xml", ".csv")) and parts[0] in DATA_DIRS and len(parts) >= 2 and cur(manifest_path) is not None:
            run({"op": "manifest_remove", "path": manifest_path, "entry": rel, "auto": True})
    return ops


# ----------------------------------------------------------------------- workspace
class LazyView:
    """Dict-like view of the workspace (branch tree + overlay). Content is fetched on first read,
    so linting a one-file edit never downloads the rest of the module."""

    def __init__(self, ws: "Workspace") -> None:
        self.ws = ws

    def __contains__(self, path: str) -> bool:
        return self.ws.exists(path)

    def get(self, path: str, default: Any = None) -> Any:
        ws = self.ws
        if not ws.exists(path) or not ws._is_text(path):
            return default
        try:
            return ws.read_text(path)
        except WorkspaceError:
            return default

    def __getitem__(self, path: str) -> str:
        value = self.get(path)
        if value is None:
            raise KeyError(path)
        return value

    def keys(self) -> list[str]:
        ws = self.ws
        names = {p for p in ws.tree if ws.overlay.get(p, "") is not None}
        names |= {p for p, t in ws.overlay.items() if t is not None}
        return sorted(names)


@dataclass
class Workspace:
    ws_id: str
    client: str
    module: str
    root: str
    repo: GitHubRepo
    branch: str
    base_commit: str
    base_tree_sha: str
    tree: dict[str, dict[str, str]]
    files: dict[str, str] = field(default_factory=dict)  # loaded base text (lazy)
    base_text: dict[str, str] = field(default_factory=dict)
    overlay: dict[str, str | None] = field(default_factory=dict)
    journal: list[dict[str, Any]] = field(default_factory=list)
    wip_branch: str = ""
    wip_head: str | None = None
    touched_at: float = field(default_factory=time.time)
    lock: threading.RLock = field(default_factory=threading.RLock)
    ckpt_lock: threading.Lock = field(default_factory=threading.Lock)
    ckpt_idle: threading.Event = field(default_factory=threading.Event)
    ckpt_dirty: bool = False
    ckpt_running: bool = False
    ckpt_last: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.ckpt_idle.set()

    # -------- view helpers
    def view(self) -> LazyView:
        return LazyView(self)

    def exists(self, path: str) -> bool:
        if path in self.overlay:
            return self.overlay[path] is not None
        return path in self.tree

    def _is_text(self, path: str) -> bool:
        return path.lower().endswith(TEXT_EXT) or os.path.basename(path) in {"__manifest__.py", "ir.model.access.csv"}

    def load(self, paths: list[str], *, strict: bool = True) -> dict[str, str]:
        need = {}
        for p in paths:
            if p in self.overlay or p in self.files:
                continue
            meta = self.tree.get(p)
            if meta is None:
                continue
            if int(meta.get("size", 0)) > MAX_FILE_BYTES:
                if strict:
                    raise WorkspaceError("file_too_large", f"{p} is larger than {MAX_FILE_BYTES} bytes and is not editable through the workspace", path=p)
                continue
            need[p] = meta["sha"]
        if need:
            fetched = self.repo.blobs_text(need)
            self.files.update(fetched)
            self.base_text.update(fetched)
        return {p: self.read_text(p) for p in paths if self.exists(p) and (p in self.overlay or p in self.files)}

    def read_text(self, path: str) -> str:
        if path in self.overlay:
            text = self.overlay[path]
            if text is None:
                raise WorkspaceError("not_found", f"{path} was deleted in this workspace", path=path)
            return text
        if path not in self.files:
            self.load([path])
        return self.files[path]

    def module_paths(self) -> list[str]:
        prefix = f"{self.root}/" if self.root else ""
        paths = {p for p in self.tree if p.startswith(prefix) and self._is_text(p)}
        paths |= {p for p, t in self.overlay.items() if t is not None and p.startswith(prefix)}
        paths -= {p for p, t in self.overlay.items() if t is None}
        return sorted(paths)

    def changed_paths(self) -> list[str]:
        changed = []
        for path, text in self.overlay.items():
            if text is None:
                if path in self.tree:
                    changed.append(path)
            elif path not in self.tree or git_blob_sha(text) != self.tree[path]["sha"]:
                changed.append(path)
        return sorted(changed)

    def base_for(self, path: str) -> str | None:
        if path not in self.tree:
            return None
        if path not in self.base_text:
            self.base_text[path] = self.repo.blob_text(self.tree[path]["sha"])
        return self.base_text[path]


class WorkspaceRegistry:
    """Bounded in-memory cache. Misses are rebuilt from the WIP branch, so eviction is harmless."""

    def __init__(self, capacity: int = 6, ttl_seconds: int = 7200) -> None:
        self._items: OrderedDict[str, Workspace] = OrderedDict()
        self._lock = threading.Lock()
        self.capacity = capacity
        self.ttl = ttl_seconds

    def get(self, ws_id: str) -> Workspace | None:
        with self._lock:
            ws = self._items.get(ws_id)
            if ws and time.time() - ws.touched_at > self.ttl:
                self._items.pop(ws_id, None)
                return None
            if ws:
                self._items.move_to_end(ws_id)
                ws.touched_at = time.time()
            return ws

    def put(self, ws: Workspace) -> None:
        with self._lock:
            self._items[ws.ws_id] = ws
            self._items.move_to_end(ws.ws_id)
            while len(self._items) > self.capacity:
                self._items.popitem(last=False)

    def drop(self, ws_id: str) -> None:
        with self._lock:
            self._items.pop(ws_id, None)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


REGISTRY = WorkspaceRegistry()


@dataclass
class ClientTarget:
    name: str
    repo: GitHubRepo
    branch: str


class WorkspaceService:
    def __init__(self, resolve_client, *, protected: str | None = None, autocheckpoint: bool = True, checkpoint_mode: str = "sync", registry: WorkspaceRegistry | None = None, core_repo_factory=None, wip_files: bool = True) -> None:
        """``resolve_client(name) -> ClientTarget`` keeps this module free of web-framework imports.

        ``checkpoint_mode``: ``sync`` persists the journal before each edit returns; ``async`` does it in
        a background thread (edits return immediately; commit/discard/open wait for it).
        """
        self.resolve_client = resolve_client
        raw = PROTECTED_DEFAULT if protected is None else protected
        self.protected = {b.strip() for b in raw.split(",") if b.strip()}
        self.autocheckpoint = autocheckpoint
        self.checkpoint_mode = checkpoint_mode
        self.wip_files = wip_files
        self.registry = registry or REGISTRY
        self.core_repo_factory = core_repo_factory or (lambda ws: GitHubRepo("odoo", "odoo", ws.repo.token, transport=ws.repo.transport, sleep=ws.repo.sleep))

    # ------------------------------------------------------------------ open
    def open(self, client_name: str, module_name: str, *, create: bool = False, fresh: bool = False, branch: str | None = None) -> dict[str, Any]:
        target = self.resolve_client(client_name)
        use_branch = branch or target.branch
        repo = target.repo
        head = repo.head(use_branch)
        tree = repo.tree(head["tree"])
        roots = sorted({p.rsplit("/", 1)[0] if "/" in p else "" for p in tree if p.endswith("/__manifest__.py") or p == "__manifest__.py"})
        by_name: dict[str, list[str]] = {}
        for r in roots:
            by_name.setdefault(os.path.basename(r) or r, []).append(r)
        wanted = module_name.strip()
        if wanted in by_name:
            if len(by_name[wanted]) > 1:
                raise WorkspaceError("module_ambiguous", f"'{wanted}' exists at several paths", candidates=by_name[wanted])
            root = by_name[wanted][0]
        elif create:
            if not re.fullmatch(r"[a-z][a-z0-9_]*", wanted):
                raise WorkspaceError("module_name_invalid", "new module names must be lowercase technical names like my_module")
            root = wanted
        else:
            close = difflib.get_close_matches(wanted, list(by_name), n=5, cutoff=0.5)
            raise WorkspaceError("module_not_found", f"no module '{wanted}' on {use_branch}. Pass create=true to start a new one.", did_you_mean=close, modules=sorted(by_name)[:60])
        ws_id = encode_ws_id(target.name, root or wanted, use_branch)
        slug = f"{slugify(target.name)}-{slugify(root or wanted)}"
        ws = Workspace(ws_id=ws_id, client=target.name, module=os.path.basename(root) or wanted, root=root, repo=repo, branch=use_branch, base_commit=head["commit"], base_tree_sha=head["tree"], tree=tree, wip_branch=f"mcp/wip/{slug}")
        # Lazy: only the manifest and package __init__ files are needed up front (summary + wiring).
        prefix = f"{root}/" if root else ""
        eager = [p for p in tree if p.startswith(prefix) and (p.endswith("/__init__.py") or p == prefix + "__init__.py" or p == prefix + "__manifest__.py") and int(tree[p].get("size", 0)) <= MAX_FILE_BYTES]
        if eager:
            fetched = repo.blobs_text({p: tree[p]["sha"] for p in eager})
            ws.files.update(fetched)
            ws.base_text.update(fetched)
        resumed: dict[str, Any] | None = None
        if fresh:
            self._drop_wip(ws)
        else:
            resumed = self._resume_from_wip(ws)
        self.registry.put(ws)
        return self._open_summary(ws, resumed, is_new=root not in set(roots))

    def _open_summary(self, ws: Workspace, resumed: dict[str, Any] | None, is_new: bool) -> dict[str, Any]:
        paths = ws.module_paths()
        index = []
        for p in paths:
            text = ws.overlay.get(p)
            size = len(text) if text is not None else int(ws.tree.get(p, {}).get("size", 0))
            index.append({"path": p[len(ws.root) + 1 :] if ws.root else p, "bytes": size})
        manifest_text = ws.view().get(f"{ws.root}/__manifest__.py" if ws.root else "__manifest__.py")
        depends: list[str] = []
        version = None
        if manifest_text:
            try:
                import ast as _ast

                data = _ast.literal_eval(manifest_text.strip())
                depends = list(data.get("depends", []))
                version = data.get("version")
            except Exception:  # noqa: BLE001
                pass
        out: dict[str, Any] = {
            "status": "ok",
            "ws_id": ws.ws_id,
            "client": ws.client,
            "module": ws.module,
            "module_path": ws.root,
            "branch": ws.branch,
            "base_commit": ws.base_commit[:12],
            "new_module": is_new,
            "depends": depends,
            "version": version,
            "file_count": len(index),
            "files": index[:150],
            "hint": "Use ws_read/ws_grep to inspect, ws_edit for small exact edits (lint runs instantly), ws_commit when lint is clean.",
        }
        if len(index) > 150:
            out["files_truncated"] = len(index) - 150
        if resumed:
            out["resumed"] = resumed
        return out

    # ------------------------------------------------------------------ resolve
    def get(self, ws_id: str) -> Workspace:
        ws = self.registry.get(ws_id)
        if ws:
            return ws
        client, module, branch = decode_ws_id(ws_id)
        self.open(client, module, create=True, branch=branch)
        ws = self.registry.get(ws_id)
        if not ws:
            raise WorkspaceError("ws_lost", "workspace could not be rebuilt; call ws_open again")
        return ws

    # ------------------------------------------------------------------ read / grep
    def read(self, ws_id: str, paths: list[str], *, start_line: int = 1, max_lines: int = 300) -> dict[str, Any]:
        ws = self.get(ws_id)
        with ws.lock:
            out, missing = [], []
            resolved = [self._abs(ws, p) for p in paths[:10]]
            ws.load([p for p in resolved if ws.exists(p) and ws._is_text(p)], strict=False)
            for path in resolved:
                if not ws.exists(path):
                    missing.append({"path": path, "did_you_mean": difflib.get_close_matches(path, ws.module_paths(), n=3, cutoff=0.5)})
                    continue
                if not ws._is_text(path):
                    missing.append({"path": path, "error": "binary or non-text file"})
                    continue
                text = ws.read_text(path)
                lines = text.splitlines()
                start = max(1, start_line)
                chunk = lines[start - 1 : start - 1 + max_lines]
                numbered = "\n".join(f"{start + i}|{row}" for i, row in enumerate(chunk))
                item = {"path": path, "total_lines": len(lines), "from": start, "to": start + len(chunk) - 1 if chunk else start, "text": numbered}
                if start - 1 + max_lines < len(lines):
                    item["more"] = f"call again with start_line={start + max_lines}"
                out.append(item)
            res: dict[str, Any] = {"status": "ok" if out else "error", "files": out}
            if missing:
                res["missing"] = missing
            return res

    def grep(self, ws_id: str, pattern: str, *, glob: str | None = None, regex: bool = False, scope: str = "module", context: int = 0, max_matches: int = 40) -> dict[str, Any]:
        ws = self.get(ws_id)
        with ws.lock:
            try:
                rx = re.compile(pattern if regex else re.escape(pattern), re.I if pattern.islower() else 0)
            except re.error as exc:
                raise WorkspaceError("bad_regex", str(exc)) from exc
            if scope == "repo":
                candidates = [p for p in ws.tree if ws._is_text(p) and int(ws.tree[p].get("size", 0)) <= MAX_FILE_BYTES]
            else:
                candidates = ws.module_paths()
            if glob:
                candidates = [p for p in candidates if fnmatch.fnmatch(p, glob) or fnmatch.fnmatch(os.path.basename(p), glob)]
            if scope == "repo" and len(candidates) > 250:
                return {"status": "error", "error_code": "scope_too_wide", "error": f"{len(candidates)} files match; narrow with glob (e.g. 'sale_*/**/*.py')", "files": len(candidates)}
            ws.load(candidates, strict=False)
            view = ws.view()
            hits, files_with = [], set()
            for path in candidates:
                text = view.get(path)
                if text is None:
                    continue
                lines = text.splitlines()
                for i, row in enumerate(lines, 1):
                    if rx.search(row):
                        files_with.add(path)
                        entry: dict[str, Any] = {"path": path, "line": i, "text": row.strip()[:240]}
                        if context:
                            entry["ctx"] = "\n".join(lines[max(0, i - 1 - context) : i + context])
                        hits.append(entry)
                        if len(hits) >= max_matches:
                            return {"status": "ok", "matches": hits, "truncated": True, "files_with_matches": len(files_with)}
            return {"status": "ok", "matches": hits, "files_with_matches": len(files_with)}

    # ------------------------------------------------------------------ edit
    def _abs(self, ws: Workspace, path: str) -> str:
        """Module-relative paths are expanded; ``repo:<path>`` addresses the repository root."""
        if path.startswith("repo:"):
            path = path[5:].lstrip("/")
            explicit_repo = True
        else:
            path = path.lstrip("/")
            explicit_repo = False
        if ".." in path.split("/"):
            raise WorkspaceError("path_invalid", "paths must not contain '..'")
        if explicit_repo or not ws.root or path.startswith(ws.root + "/") or path in ws.tree or path in ws.overlay:
            return path
        return f"{ws.root}/{path}"

    def _guard(self, ws: Workspace, edit: dict[str, Any]) -> dict[str, Any] | None:
        op = edit.get("op") or ("write" if edit.get("content") is not None else "replace" if edit.get("search") is not None else None)
        path = edit["path"]
        if op == "delete":
            return None
        if not ws._is_text(path):
            return {"code": "binary_unsupported", "msg": f"{path} is not a text file type; ws_edit only handles text ({', '.join(TEXT_EXT[:6])}, ...). Add binary assets directly in git."}
        for key in ("content", "replace"):
            value = edit.get(key)
            if isinstance(value, str) and len(value.encode("utf-8")) > MAX_FILE_BYTES:
                return {"code": "too_large", "msg": f"{key} is larger than {MAX_FILE_BYTES} bytes; split the file"}
        return None

    def edit(self, ws_id: str, edits: list[dict[str, Any]], *, strict: bool = False, autofix: bool = True) -> dict[str, Any]:
        ws = self.get(ws_id)
        with ws.lock:
            normalized = []
            for raw in edits:
                item = dict(raw)
                item["path"] = self._abs(ws, item["path"])
                normalized.append(item)
            staged = dict(ws.overlay)
            results, failed = [], False
            healed_edits: list[dict[str, Any]] = []
            applied: list[dict[str, Any]] = []
            for index, edit in enumerate(normalized):
                path = edit["path"]
                bad = self._guard(ws, edit)
                if bad:
                    failed = True
                    results.append({"i": index, "path": path, "ok": False, **bad})
                    continue
                if path in staged:
                    current = staged[path]
                elif ws.exists(path):
                    current = ws.load([path]).get(path)
                else:
                    current = None
                new_text, err, healed = apply_edit_ex(current, edit)
                if err:
                    failed = True
                    results.append({"i": index, "path": path, "ok": False, **err})
                    continue
                staged[path] = new_text
                applied.append({**edit, "op": infer_op(edit)})  # journal entries always carry an explicit op
                results.append({"i": index, "path": path, "ok": True, "op": infer_op(edit)})
                if healed:
                    healed_edits.append({"i": index, "path": path, "note": "search differed only by whitespace/indentation; matched the one place it fits and kept the file's indentation"})
            if failed:
                return {"status": "rejected", "reason": "edit_failed", "applied": 0, "results": results, "hint": "Nothing was changed. Fix only the failing edits and resend the whole batch."}
            saved_overlay, saved_journal = dict(ws.overlay), list(ws.journal)
            ws.overlay = staged
            ws.journal.extend(applied)
            touched = sorted({e["path"] for e in applied})
            auto_ops: list[dict[str, Any]] = []
            if autofix:
                new_paths = [p for p in touched if p not in ws.tree and p not in saved_overlay and ws.overlay.get(p) is not None]
                gone = [p for p in touched if ws.overlay.get(p) is None and p in ws.tree]
                view = ws.view()
                auto_ops = plan_autofix(view.get, ws.exists, new_paths, ws.root, gone)
                for op in auto_ops:
                    path = op["path"]
                    cur = ws.overlay[path] if path in ws.overlay else (ws.load([path]).get(path) if ws.exists(path) else None)
                    new_text, err = apply_edit(cur, op)
                    if err:
                        continue
                    ws.overlay[path] = new_text
                    ws.journal.append(op)
            all_touched = sorted(set(touched) | {o["path"] for o in auto_ops})
            deleted_now = [p for p in all_touched if ws.overlay.get(p) is None]
            findings = lint.lint_workspace(ws.view(), all_touched, deleted_now, roots=[ws.root])
            errors = [f for f in findings if f["severity"] == "error"]
            if strict and errors:
                ws.overlay, ws.journal = saved_overlay, saved_journal
                return {"status": "rejected", "reason": "lint_errors", "applied": 0, "findings": findings, "hint": "strict mode rolled the batch back; fix the findings and resend."}
            checkpoint = self._schedule_checkpoint(ws)
            changed = ws.changed_paths()
            summary = []
            for path in changed:
                new = ws.overlay.get(path)
                old = ws.base_for(path) if path in ws.tree else None
                summary.append({"path": path, "status": "deleted" if new is None else "added" if old is None else "modified", **_count_delta(old, new)})
            return {
                "status": "ok" if not errors else "ok_with_errors",
                "applied": len(applied),
                "autofixed": [{"path": o["path"], "what": _auto_label(o)} for o in auto_ops],
                **({"healed": healed_edits} if healed_edits else {}),
                "findings": findings,
                "errors": len(errors),
                "changed": summary,
                "checkpoint": checkpoint,
                "published": False,
                "where": f"unpublished work-in-progress on GitHub branch '{ws.wip_branch}'; NOT on '{ws.branch}' until ws_commit",
                "commit_ready": not errors,
                "next": "fix the lint errors with ws_edit" if errors else "ws_commit when the change is complete",
            }

    # ------------------------------------------------------------------ outline / scaffold / extend
    def _series(self, ws: Workspace, override: int | None = None) -> tuple[int | None, str]:
        if override:
            return override, "given"
        manifest = ws.view().get(f"{ws.root}/__manifest__.py" if ws.root else "__manifest__.py")
        if manifest:
            try:
                import ast as _ast

                found = outline_service.series_of(_ast.literal_eval(manifest.strip()).get("version"))
                if found:
                    return found, "manifest"
            except Exception:  # noqa: BLE001
                pass
        return None, "unknown"

    def outline(self, ws_id: str, paths: list[str] | None = None, max_files: int = 200) -> dict[str, Any]:
        ws = self.get(ws_id)
        with ws.lock:
            if paths:
                candidates = [self._abs(ws, p) for p in paths]
            else:
                candidates = [p for p in ws.module_paths() if (p.endswith(".py") or p.endswith(".xml")) and "/static/" not in p]
            truncated = max(0, len(candidates) - max_files)
            candidates = candidates[:max_files]
            ws.load([p for p in candidates if ws.exists(p)], strict=False)
            view = ws.view()
            files = {p: view.get(p) for p in candidates if view.get(p) is not None}
            result = outline_service.build_outline(files, ws.root)
            series, source = self._series(ws)
            result.update({"series": series, "series_source": source, "files_scanned": len(files)})
            if truncated:
                result["files_skipped"] = truncated
            return result

    def symbol(self, ws_id: str, model: str, field: str | None = None, series: int | None = None) -> dict[str, Any]:
        """Verify a model/field against upstream Odoo source for this module's series (plus local code)."""
        ws = self.get(ws_id)
        with ws.lock:
            found_series, source = self._series(ws, series)
            if not found_series:
                return {"status": "error", "error_code": "bad_spec", "error": "Odoo series unknown (new module without manifest): pass series, e.g. 18"}
            local_paths = [p for p in ws.module_paths() if p.endswith(".py") and not p.endswith(("__init__.py", "__manifest__.py")) and "/static/" not in p]
            ws.load(local_paths, strict=False)
            view = ws.view()
            local: list[dict[str, Any]] = []
            for path in local_paths:
                text = view.get(path)
                if text is not None:
                    local.extend(outline_service.outline_python(path[len(ws.root) + 1 :] if ws.root else path, text).get("classes", []))
            depends: list[str] = []
            manifest = view.get(f"{ws.root}/__manifest__.py" if ws.root else "__manifest__.py")
            if manifest:
                try:
                    import ast as _ast

                    depends = [d for d in _ast.literal_eval(manifest.strip()).get("depends", []) if isinstance(d, str)]
                except Exception:  # noqa: BLE001
                    depends = []
            core = symbol_service.CoreSource(self.core_repo_factory(ws), f"{found_series}.0")
            result = symbol_service.lookup(core, local, depends, model, field)
            result["series"] = {"value": found_series, "source": source}
            return result

    def _spec_error(self, exc: Exception) -> dict[str, Any]:
        return {"status": "error", "error_code": "bad_spec", "error": str(exc)}

    def _ensure_manifest_edit(self, ws: Workspace, series: int | None, mail: bool, depends_extra: list[str]) -> dict[str, Any] | None:
        path = f"{ws.root}/__manifest__.py" if ws.root else "__manifest__.py"
        if ws.exists(path):
            return None
        if not series:
            raise scaffold_service.SpecError("this is a new module: pass series (the Odoo major version, e.g. 18) so the right syntax and manifest version are generated")
        deps = sorted({"base", *(["mail"] if mail else []), *depends_extra})
        title_name = ws.module.replace("_", " ").title()
        content = "{\n" + f"    'name': {title_name!r},\n    'version': '{series}.0.1.0.0',\n    'category': 'Customizations',\n    'summary': {title_name!r},\n    'depends': {deps!r},\n    'data': [],\n    'license': 'LGPL-3',\n    'installable': True,\n    'application': False,\n" + "}\n"
        return {"path": "__manifest__.py", "op": "write", "content": content}

    def scaffold(self, ws_id: str, model: str, fields: list[dict[str, Any]], *, with_views: bool = True, mail: bool = False, menu: str | None = None, menu_parent: str | None = None, access_group: str = "base.group_user", series: int | None = None, description: str | None = None) -> dict[str, Any]:
        ws = self.get(ws_id)
        with ws.lock:
            try:
                found_series, source = self._series(ws, series)
                s = scaffold_service.snake(model)
                if ws.exists(f"{ws.root}/models/{s}.py" if ws.root else f"models/{s}.py"):
                    return {"status": "error", "error_code": "exists", "error": f"models/{s}.py already exists; use ws_extend to add fields or ws_edit to change it"}
                access_path = f"{ws.root}/security/ir.model.access.csv" if ws.root else "security/ir.model.access.csv"
                access_text = ws.view().get(access_path) if ws.exists(access_path) else None
                if access_text is not None and f"access_{s}_user," in access_text:
                    return {"status": "error", "error_code": "access_exists", "error": f"access rule access_{s}_user already exists in security/ir.model.access.csv"}
                manifest_edit = self._ensure_manifest_edit(ws, found_series, mail, [])
                depends: list[str] = ["mail"] if manifest_edit and mail else []
                if not manifest_edit:
                    try:
                        import ast as _ast

                        depends = list(_ast.literal_eval(ws.view().get(f"{ws.root}/__manifest__.py" if ws.root else "__manifest__.py").strip()).get("depends", []))
                    except Exception:  # noqa: BLE001
                        depends = []
                edits = scaffold_service.scaffold_edits(module=ws.module, model=model, fields=fields, series=found_series, mail=mail, menu=menu, menu_parent=menu_parent, with_views=with_views, access_exists=access_text is not None, access_group=access_group, depends=depends, description=description)
                if manifest_edit:
                    edits.insert(0, manifest_edit)
                    edits = [e for e in edits if not (e.get("op") == "manifest_add" and e.get("key") == "depends")]
            except scaffold_service.SpecError as exc:
                return self._spec_error(exc)
            result = self.edit(ws_id, edits)
            result["generated"] = [self._abs(ws, e["path"]) for e in edits]
            result["series"] = {"value": found_series, "source": source}
            if found_series is None:
                result["warning"] = "Odoo series unknown (no manifest version); generated for <=17 syntax. Pass series=18 for list views."
            return result

    def extend(self, ws_id: str, model: str, fields: list[dict[str, Any]], *, view_ref: str | None = None, after_field: str | None = None, position: str = "after", view_type: str = "form", file_name: str | None = None, series: int | None = None) -> dict[str, Any]:
        ws = self.get(ws_id)
        with ws.lock:
            try:
                found_series, source = self._series(ws, series)
                edits = scaffold_service.extend_edits(module=ws.module, model=model, fields=fields, series=found_series, view_ref=view_ref, anchor=after_field, position=position, view_type=view_type, file_name=file_name)
                clash = [e["path"] for e in edits if ws.exists(self._abs(ws, e["path"]))]
                if clash:
                    return {"status": "error", "error_code": "exists", "error": f"{', '.join(clash)} already exist; pass file_name to choose another name or edit with ws_edit"}
                manifest_edit = self._ensure_manifest_edit(ws, found_series, False, [])
                if manifest_edit:
                    edits.insert(0, manifest_edit)
            except scaffold_service.SpecError as exc:
                return self._spec_error(exc)
            result = self.edit(ws_id, edits)
            result["generated"] = [self._abs(ws, e["path"]) for e in edits]
            result["reminder"] = f"'{model}' must be defined by a module in this module's depends; verify with ws_symbol." if "." in model else ""
            return result

    def status(self, ws_id: str) -> dict[str, Any]:
        """Where is my work? Branch, WIP branch, changed files, last checkpoint, links."""
        ws = self.get(ws_id)
        with ws.lock:
            changed = ws.changed_paths()
            files = []
            for p in changed:
                new = ws.overlay.get(p)
                files.append({"path": p, "status": "deleted" if new is None else "added" if p not in ws.tree else "modified"})
            repo = ws.repo
            default = repo.default_branch()
            return {
                "status": "ok",
                "client_branch": ws.branch,
                "default_branch": default,
                "base_commit": ws.base_commit[:12],
                "unpublished_files": files,
                "published": not changed,
                "wip_branch": ws.wip_branch if ws.wip_head else None,
                "wip_files_visible": self.wip_files,
                "last_checkpoint": ws.ckpt_last or None,
                "urls": {"client_branch": repo.web_url("tree", ws.branch), "wip_branch": repo.web_url("tree", ws.wip_branch) if ws.wip_head else None},
                "explain": "Edits are only on the WIP branch until ws_commit pushes them to the client branch." if changed else f"Nothing unpublished. Latest work is on '{ws.branch}'" + (f", which is not the default branch '{default}'." if default and default != ws.branch else "."),
            }

    # ------------------------------------------------------------------ diff / lint
    def diff(self, ws_id: str, path: str | None = None, max_chars: int = 12000) -> dict[str, Any]:
        ws = self.get(ws_id)
        with ws.lock:
            paths = [self._abs(ws, path)] if path else ws.changed_paths()
            chunks, total = [], 0
            for p in paths:
                old = ws.base_for(p)
                new = ws.overlay.get(p) if p in ws.overlay else old
                text = "".join(difflib.unified_diff((old or "").splitlines(True), (new or "").splitlines(True), f"a/{p}", f"b/{p}", n=2))
                if total + len(text) > max_chars:
                    chunks.append({"path": p, "diff": text[: max(0, max_chars - total)] + "\n...truncated", "truncated": True})
                    break
                total += len(text)
                chunks.append({"path": p, "diff": text})
            return {"status": "ok", "files": chunks, "changed": len(paths)}

    def _lint_changed(self, ws: Workspace) -> list[dict[str, Any]]:
        changed = ws.changed_paths()
        deleted = [p for p in changed if ws.overlay.get(p) is None]
        live = [p for p in changed if ws.overlay.get(p) is not None]
        extra = [p for p in (f"{ws.root}/__manifest__.py" if ws.root else "__manifest__.py",) if ws.exists(p)] if changed else []
        return lint.lint_workspace(ws.view(), sorted(set(live) | set(extra)), deleted, roots=[ws.root])

    def lint(self, ws_id: str) -> dict[str, Any]:
        ws = self.get(ws_id)
        with ws.lock:
            findings = self._lint_changed(ws)
            errors = sum(1 for f in findings if f["severity"] == "error")
            return {"status": "ok" if not errors else "errors", "findings": findings, "errors": errors, "commit_ready": errors == 0, "changed_files": len(ws.changed_paths())}

    # ------------------------------------------------------------------ WIP persistence
    def _compact_journal(self, ws: Workspace) -> list[dict[str, Any]]:
        """Collapse history without losing replayability: files fully written by the agent become one
        write of their final content; duplicate wiring ops disappear; created-then-deleted files vanish."""
        by_path: "OrderedDict[str, list[dict[str, Any]]]" = OrderedDict()
        for op in ws.journal:
            by_path.setdefault(op["path"], []).append(op)
        out: list[dict[str, Any]] = []
        for path, ops in by_path.items():
            final = ws.overlay.get(path, "\0missing")
            plain = all(not o.get("auto") for o in ops)
            if plain and ops[0].get("op") == "write" and final != "\0missing":
                if final is None:
                    if path in ws.tree:
                        out.append({"path": path, "op": "delete"})
                else:
                    out.append({"path": path, "op": "write", "content": final})
                continue
            seen = set()
            for o in ops:
                sig = json.dumps(o, sort_keys=True) if o.get("auto") else None
                if sig and sig in seen:
                    continue
                if sig:
                    seen.add(sig)
                out.append(o)
        return out

    def _journal_name(self, ws: Workspace) -> str:
        return f"{JOURNAL_PREFIX}{slugify(ws.client)}-{slugify(ws.module)}.json"

    def _journal_blob(self, ws: Workspace, ops: list[dict[str, Any]]) -> str:
        return json.dumps({"v": 2, "base": ws.base_commit, "branch": ws.branch, "module": ws.root, "ops": ops}, separators=(",", ":"))

    def _schedule_checkpoint(self, ws: Workspace) -> dict[str, Any]:
        if not self.autocheckpoint:
            return {"saved": False, "reason": "disabled"}
        if self.checkpoint_mode == "sync":
            return self._checkpoint(ws)
        ws.ckpt_dirty = True
        with ws.ckpt_lock:
            if not ws.ckpt_running:
                ws.ckpt_running = True
                ws.ckpt_idle.clear()
                _CKPT_POOL.submit(self._checkpoint_worker, ws)
        return {"saved": "queued", **({"last": ws.ckpt_last} if ws.ckpt_last and not ws.ckpt_last.get("saved") else {})}

    def _checkpoint_worker(self, ws: Workspace) -> None:
        try:
            while ws.ckpt_dirty:
                ws.ckpt_dirty = False
                ws.ckpt_last = self._checkpoint(ws)
        finally:
            with ws.ckpt_lock:
                ws.ckpt_running = False
                ws.ckpt_idle.set()

    def flush(self, ws: Workspace, timeout: float = 30.0) -> None:
        """Wait for any background checkpoint so later WIP deletion cannot race a late write."""
        ws.ckpt_idle.wait(timeout)

    def _checkpoint(self, ws: Workspace) -> dict[str, Any]:
        """Persist the compacted journal and (by default) the changed files, so the work is visible on the Git provider.

        Blobs are content-addressed, so only files whose content changed since the last checkpoint are uploaded.
        """
        try:
            with ws.lock:
                ws.journal = self._compact_journal(ws)
                if not ws.journal:
                    return {"saved": True, "ops": 0}
                payload = self._journal_blob(ws, ws.journal)
                ops, parent, base_tree = len(ws.journal), ws.wip_head or ws.base_commit, ws.base_tree_sha
                files: list[FileChange] = []
                files_visible = False
                if self.wip_files:
                    changed = ws.changed_paths()
                    total = sum(len(ws.overlay.get(p) or "") for p in changed)
                    if len(changed) <= MAX_WIP_FILES and total <= MAX_WIP_BYTES:
                        files = [FileChange(path=p, content=ws.overlay.get(p)) for p in changed]
                        files_visible = True
            repo = ws.repo
            changes = files + [FileChange(path=self._journal_name(ws), content=payload)]
            message = f"mcp wip: {ws.module} ({ops} ops, {len(files)} files)"
            if hasattr(repo, "commit_to_branch"):
                created = repo.commit_to_branch(
                    ws.wip_branch, changes, message,
                    expected_head=ws.wip_head or (None if not repo.ref_exists(ws.wip_branch) else parent),
                    create_from=ws.base_commit,
                )
            else:
                created = repo.create_tree_commit(parent=parent, base_tree=base_tree, changes=changes, message=message)
                try:
                    if ws.wip_head is None and not repo.ref_exists(ws.wip_branch):
                        repo.move_ref(ws.wip_branch, created["commit"], create=True)
                    else:
                        repo.move_ref(ws.wip_branch, created["commit"])
                except NonFastForward:
                    head = repo.head(ws.wip_branch)
                    created = repo.create_tree_commit(parent=head["commit"], base_tree=base_tree, changes=changes, message=message)
                    repo.move_ref(ws.wip_branch, created["commit"])
            ws.wip_head = created["commit"]
            return {"saved": True, "branch": ws.wip_branch, "ops": ops, "files_visible_on_github": files_visible, "url": repo.web_url("tree", ws.wip_branch)}
        except Exception as exc:  # noqa: BLE001 - persistence is best effort, never blocks editing
            return {"saved": False, "reason": str(exc)[:200]}

    def _resume_from_wip(self, ws: Workspace) -> dict[str, Any] | None:
        repo = ws.repo
        try:
            head = repo.head(ws.wip_branch)
            sha = repo.root_blob(head["tree"], self._journal_name(ws))
            if not sha:
                return None
            data = json.loads(repo.blob_text(sha))
        except GitHubError as exc:
            if exc.status == 404:
                return None
            return None
        except (ValueError, KeyError):
            return None
        ops = data.get("ops", [])
        ws.wip_head = head["commit"]
        conflicts = self._replay(ws, ops)
        return {"ops": len(ops), "replayed": len(ops) - len(conflicts), "conflicts": conflicts, "from_base": str(data.get("base", ""))[:12], "note": "unfinished work restored from the WIP branch; ws_open fresh=true discards it"}

    def _replay(self, ws: Workspace, ops: list[dict[str, Any]]) -> list[dict[str, Any]]:
        conflicts = []
        for i, op in enumerate(ops):
            path = op["path"]
            current = ws.overlay[path] if path in ws.overlay else (ws.load([path]).get(path) if ws.exists(path) else None)
            new_text, err = apply_edit(current, op)
            if err:
                conflicts.append({"i": i, "path": path, **{k: v for k, v in err.items() if k != "closest"}})
                continue
            ws.overlay[path] = new_text
            ws.journal.append(op)
        return conflicts

    def _drop_wip(self, ws: Workspace) -> None:
        self.flush(ws)
        try:
            ws.repo.delete_branch(ws.wip_branch)
        except GitHubError:
            pass
        ws.wip_head = None

    def discard(self, ws_id: str) -> dict[str, Any]:
        ws = self.get(ws_id)
        self._drop_wip(ws)
        self.registry.drop(ws_id)
        return {"status": "ok", "discarded": ws.ws_id, "wip_branch_deleted": ws.wip_branch}

    # ------------------------------------------------------------------ commit
    def commit(self, ws_id: str, message: str | None = None, *, push: bool = True, branch: str | None = None, allow_lint_errors: bool = False) -> dict[str, Any]:
        ws = self.get(ws_id)
        with ws.lock:
            changed = ws.changed_paths()
            if not changed:
                return {"status": "no_changes", "message": "workspace has no differences from the branch"}
            findings = self._lint_changed(ws)
            errors = [f for f in findings if f["severity"] == "error"]
            if errors and not allow_lint_errors:
                return {"status": "blocked_by_lint", "errors": len(errors), "findings": errors[:40], "hint": "Fix with ws_edit. Only pass allow_lint_errors=true if the user explicitly wants a known-broken push."}
            if not message or len(message.strip()) < 3:
                message = _auto_message(ws.module, ws.overlay, changed)
            target_branch = branch or ws.branch
            if target_branch in self.protected:
                return {"status": "blocked_protected_branch", "branch": target_branch, "hint": f"'{target_branch}' is protected. Commit to the client's staging branch or pass branch='<new-branch>' to create one; merge via pull request."}
            if not push:
                return {"status": "ready", "changed": changed, "lint_errors": len(errors), "branch": target_branch}
            ws.journal = self._compact_journal(ws)
            final_files = {p: ws.overlay.get(p) for p in changed}
            key = operation_key(ws.base_commit, [FileChange(p, t) for p, t in final_files.items()])
            repo = ws.repo
            if target_branch != ws.branch:
                if repo.ref_exists(target_branch):
                    return {"status": "error", "error_code": "branch_exists", "error": f"branch '{target_branch}' already exists; open a workspace on it with ws_open(branch='{target_branch}') or choose a new branch name"}
                repo.move_ref(target_branch, ws.base_commit, create=True)
            expected = ws.base_commit
            overwrote: list[str] = []
            journal_by_path: dict[str, list[dict[str, Any]]] = {}
            for op in ws.journal:
                journal_by_path.setdefault(op["path"], []).append(op)

            def build(head: dict[str, str], get_tree, moved: bool):
                if not moved:
                    return [FileChange(p, final_files[p]) for p in changed], []
                head_tree = get_tree()
                changes: list[FileChange] = []
                conflicts: list[dict[str, Any]] = []
                for path in changed:
                    final = final_files[path]
                    upstream_same = (path not in ws.tree and path not in head_tree) or (path in ws.tree and path in head_tree and ws.tree[path]["sha"] == head_tree[path]["sha"])
                    if upstream_same:
                        changes.append(FileChange(path, final))
                        continue
                    ops = journal_by_path.get(path, [])
                    current = repo.blob_text(head_tree[path]["sha"]) if path in head_tree else None
                    if not ops:
                        changes.append(FileChange(path, final))
                        continue
                    if ops[0].get("op") == "write" or (ops[0].get("content") is not None and ops[0].get("search") is None):
                        overwrote.append(path)
                    text, bad = current, None
                    for n, op in enumerate(ops):
                        text, bad = apply_edit(text, op)
                        if bad:
                            conflicts.append({"path": path, "op_index": n, "reason": bad["msg"], "code": bad["code"], "upstream_snippets": bad.get("closest", [])})
                            break
                    if bad:
                        continue
                    if text is None and path not in head_tree:
                        continue
                    changes.append(FileChange(path, text))
                if not conflicts:
                    post = []
                    for ch in changes:
                        if ch.content is not None:
                            post.extend(lint.lint_file(ch.path, ch.content))
                    bad_lint = [f for f in post if f["severity"] == "error"]
                    if bad_lint and not allow_lint_errors:
                        conflicts.append({"path": bad_lint[0]["path"], "reason": "merged result fails lint after rebasing onto newer upstream commits", "findings": bad_lint[:10]})
                return changes, conflicts

            outcome = (repo.push_workspace_changes(target_branch, message=message, key=key, build_changes=build, expected_base=expected) if hasattr(repo, "push_workspace_changes") else push_changes(repo, target_branch, message=message, key=key, build_changes=build, expected_base=expected))
            result: dict[str, Any] = {"status": outcome.status, "branch": target_branch, "attempts": outcome.attempts, "api_calls": outcome.api_calls}
            if outcome.status in {"pushed", "already_pushed", "no_changes"} and outcome.commit:
                result.update({"commit": outcome.commit, "commit_short": outcome.commit[:12], "rebased_onto_newer_head": outcome.rebased})
                if overwrote:
                    result["overwrote_upstream_changes"] = sorted(set(overwrote))
                if outcome.status != "no_changes":
                    self._after_push(ws, target_branch, outcome)
                result["published"] = True
                result["urls"] = {"commit": repo.web_url("commit", outcome.commit), "branch": repo.web_url("tree", target_branch)}
                default = repo.default_branch()
                if default and default != target_branch:
                    result["where"] = f"pushed to branch '{target_branch}', NOT the default branch '{default}': open urls.commit to inspect the pushed branch"
                else:
                    result["where"] = f"pushed to '{target_branch}'"
                result["next_call"] = {"tool": "workflow_module_install_and_check", "arguments": {"client_name": ws.client, "module_name": ws.module, "expected_sha": outcome.commit, "branch": target_branch}}
                result["deploy_note"] = "Pushed to the configured Git provider. This is not deployment: run next_call (Odoo.sh build + module upgrade proof)."
            elif outcome.status == "conflict":
                result["conflicts"] = outcome.conflicts
                result["hint"] = "Upstream changed the same lines. Re-read those files with ws_read (they show the current branch state after ws_open fresh=true), then re-apply your intent."
            return result

    def _after_push(self, ws: Workspace, branch: str, outcome) -> None:
        """Advance the workspace to the pushed commit, reusing what we already know when possible."""
        self.flush(ws)
        ws.base_commit = outcome.commit
        if outcome.status == "pushed" and not outcome.rebased and outcome.tree:
            ws.base_tree_sha = outcome.tree
            for ch in outcome.changes:
                if ch.content is None:
                    ws.tree.pop(ch.path, None)
                else:
                    ws.tree[ch.path] = {"sha": git_blob_sha(ch.content), "mode": ch.mode, "size": str(len(ch.content.encode("utf-8")))}
        else:  # rebased onto a newer head or already landed earlier: the tree is not ours to infer
            head = ws.repo.head(branch)
            ws.base_tree_sha = head["tree"]
            ws.tree = ws.repo.tree(head["tree"])
            ws.files.clear()
            ws.base_text.clear()
        for path, text in list(ws.overlay.items()):
            if text is None:
                ws.files.pop(path, None)
                ws.base_text.pop(path, None)
            else:
                ws.files[path] = text
                ws.base_text[path] = text
        ws.overlay.clear()
        ws.journal.clear()
        self._drop_wip(ws)
        ws.branch = branch


def _auto_message(module: str, overlay: dict[str, Any], changed: list[str]) -> str:
    """Conventional one-line commit message so the agent never stalls composing one."""
    added = [p for p in changed if overlay.get(p) is not None and p.rsplit("/", 1)[-1] not in {"__manifest__.py"}]
    names = ", ".join(sorted({p.rsplit("/", 1)[-1] for p in changed})[:4])
    more = len(changed) - 4
    suffix = f" (+{more} more)" if more > 0 else ""
    verb = "update"
    if changed and all(overlay.get(p) is None for p in changed):
        verb = "remove files from"
    elif added and len(added) == len(changed):
        verb = "update"
    return f"[IMP] {module}: {verb} {names}{suffix}"[:200]


def _auto_label(op: dict[str, Any]) -> str:
    kind = op["op"]
    if kind == "ensure_import":
        return f"import {op['name']}"
    if kind == "remove_import":
        return f"unimport {op['name']}"
    if kind == "manifest_add":
        return f"manifest {op['key']} += {op['entry']}"
    if kind == "manifest_remove":
        return f"manifest -= {op['entry']}"
    return kind


def _count_delta(old: str | None, new: str | None) -> dict[str, int]:
    a = (old or "").splitlines()
    b = (new or "").splitlines()
    added = removed = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return {"added": added, "removed": removed}

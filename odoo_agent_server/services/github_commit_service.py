"""Stateless GitHub access via the Git Data / Contents REST API.

No clone, no git binary, no local disk: every operation is a handful of HTTPS
calls, so it behaves identically on an ephemeral Render free instance after
any restart. The HTTP transport is injectable for tests.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

API = "https://api.github.com"
Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, dict[str, str], Any]]


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str, *, retryable: bool = False, path: str = "") -> None:
        super().__init__(f"GitHub {status} {path}: {message}")
        self.status = status
        self.message = message
        self.retryable = retryable
        self.path = path


class NonFastForward(GitHubError):
    pass


_LOCAL = threading.local()
# One shared pool so worker threads (and their keep-alive connections) outlive a single call.
_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="gh-api")


def git_blob_sha(text: str) -> str:
    data = text.encode("utf-8")
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()  # noqa: S324 - git object id, not security


def _decode(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"message": raw.decode("utf-8", "replace")[:300]}


def default_transport(method: str, url: str, headers: dict[str, str], body: bytes | None):
    """HTTPS with a per-thread keep-alive connection: saves a TLS handshake on every call after the first."""
    parts = urllib.parse.urlsplit(url)
    if parts.hostname != "api.github.com":
        return _urllib_transport(method, url, headers, body)
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    last_error = "unknown"
    for attempt in (0, 1):
        conn = getattr(_LOCAL, "conn", None)
        if conn is None:
            conn = http.client.HTTPSConnection("api.github.com", timeout=25)
            _LOCAL.conn = conn
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            resp_headers = {k.lower(): v for k, v in resp.getheaders()}
            if resp_headers.get("connection", "").lower() == "close":
                conn.close()
                _LOCAL.conn = None
            return resp.status, resp_headers, _decode(raw)
        except (http.client.HTTPException, OSError) as exc:  # stale keep-alive socket, reset, timeout
            last_error = str(exc)
            try:
                conn.close()
            finally:
                _LOCAL.conn = None
            if attempt == 0 and not isinstance(exc, TimeoutError):
                continue
    return 599, {}, {"message": f"network error: {last_error}"}


def _urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None):
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:  # noqa: S310
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, _decode(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, _decode(exc.read())
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 599, {}, {"message": f"network error: {exc}"}


_REPO_RE = re.compile(r"(?:github\.com[:/])(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+?)(?:\.git)?/?$")


def parse_repo(url: str) -> tuple[str, str]:
    cleaned = re.sub(r"https://[^@/]+@", "https://", (url or "").strip())
    m = _REPO_RE.search(cleaned)
    if not m:
        raise ValueError("not a github.com repository URL (the GitHub API engine needs one)")
    return m.group("owner"), m.group("repo")


@dataclass
class FileChange:
    path: str
    content: str | None  # None = delete
    mode: str = "100644"


@dataclass
class GitHubRepo:
    owner: str
    repo: str
    token: str
    transport: Transport = default_transport
    sleep: Callable[[float], None] = time.sleep
    max_attempts: int = 5
    calls: int = field(default=0, init=False)
    known_blobs: set = field(default_factory=set, init=False, repr=False)
    _default_branch: str | None = field(default=None, init=False, repr=False)

    # ------------------------------------------------------------ low level
    def request(self, method: str, path: str, payload: Any = None, *, ok: tuple[int, ...] = (200, 201)) -> Any:
        url = path if path.startswith("http") else f"{API}/repos/{self.owner}/{self.repo}{path}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "odoo-mcp-agent",
        }
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        last: GitHubError | None = None
        for attempt in range(self.max_attempts):
            self.calls += 1
            status, resp_headers, data = self.transport(method, url, headers, body)
            if status in ok:
                return data
            message = (data or {}).get("message", "") if isinstance(data, dict) else str(data)
            retry_after = resp_headers.get("retry-after")
            limited = status in (403, 429) and ("rate limit" in message.lower() or "abuse" in message.lower() or retry_after)
            if status >= 500 or status == 599 or limited:
                delay = float(retry_after) if retry_after and retry_after.isdigit() else min(0.5 * (2 ** attempt), 8.0)
                last = GitHubError(status, message, retryable=True, path=path)
                self.sleep(min(delay, 30.0))
                continue
            if status == 422 and "fast forward" in message.lower():
                raise NonFastForward(status, message, path=path)
            hint = ""
            if status == 401:
                hint = " (token invalid or expired)"
            elif status == 403:
                hint = " (token lacks contents:write for this repository)"
            elif status == 404:
                hint = " (repo/branch not found, or token cannot see this private repo)"
            raise GitHubError(status, message + hint, path=path)
        assert last is not None
        raise last

    def web_url(self, kind: str = "", ref: str = "") -> str:
        base = f"https://github.com/{self.owner}/{self.repo}"
        return f"{base}/{kind}/{ref}" if kind else base

    def default_branch(self) -> str | None:
        """The branch GitHub shows by default (cached). None when it cannot be read."""
        if getattr(self, "_default_branch", None) is None:
            try:
                self._default_branch = (self.request("GET", "") or {}).get("default_branch") or ""
            except GitHubError:
                self._default_branch = ""
        return self._default_branch or None

    # ------------------------------------------------------------ refs & trees
    def head(self, branch: str) -> dict[str, str]:
        ref = self.request("GET", f"/git/ref/heads/{branch}")
        sha = ref["object"]["sha"]
        commit = self.request("GET", f"/git/commits/{sha}")
        return {"commit": sha, "tree": commit["tree"]["sha"], "message": commit.get("message", "")}

    def ref_exists(self, branch: str) -> bool:
        try:
            self.request("GET", f"/git/ref/heads/{branch}")
            return True
        except GitHubError as exc:
            if exc.status == 404:
                return False
            raise

    def tree(self, tree_sha: str) -> dict[str, dict[str, str]]:
        """Return {path: {sha, mode, size}} for blobs. Falls back to per-directory walk when truncated."""
        data = self.request("GET", f"/git/trees/{tree_sha}?recursive=1")
        if not data.get("truncated"):
            found = {i["path"]: {"sha": i["sha"], "mode": i["mode"], "size": str(i.get("size", 0))} for i in data["tree"] if i["type"] == "blob"}
            self.known_blobs.update(v["sha"] for v in found.values())
            return found
        out: dict[str, dict[str, str]] = {}

        def walk(sha: str, prefix: str) -> None:
            level = self.request("GET", f"/git/trees/{sha}")
            for item in level["tree"]:
                full = f"{prefix}{item['path']}"
                if item["type"] == "blob":
                    out[full] = {"sha": item["sha"], "mode": item["mode"], "size": str(item.get("size", 0))}
                elif item["type"] == "tree":
                    walk(item["sha"], full + "/")

        walk(tree_sha, "")
        return out

    def tree_at(self, tree_sha: str, path: str) -> dict[str, dict[str, str]] | None:
        """List one directory (non-recursive) by walking path segments. Cheap on huge repos like odoo/odoo."""
        current = tree_sha
        for segment in [p for p in path.split("/") if p]:
            data = self.request("GET", f"/git/trees/{current}")
            nxt = next((i["sha"] for i in data.get("tree", []) if i["path"] == segment and i["type"] == "tree"), None)
            if nxt is None:
                return None
            current = nxt
        data = self.request("GET", f"/git/trees/{current}")
        return {i["path"]: {"sha": i["sha"], "type": i["type"], "size": str(i.get("size", 0))} for i in data.get("tree", [])}

    def root_blob(self, tree_sha: str, name: str) -> str | None:
        """Find one top-level file with a single non-recursive tree call (cheap even on huge repos)."""
        data = self.request("GET", f"/git/trees/{tree_sha}")
        for item in data.get("tree", []):
            if item["path"] == name and item["type"] == "blob":
                return item["sha"]
        return None

    def blob_text(self, sha: str) -> str:
        data = self.request("GET", f"/git/blobs/{sha}")
        raw = base64.b64decode(data["content"]) if data.get("encoding") == "base64" else data["content"].encode()
        return raw.decode("utf-8", "replace")

    def blobs_text(self, shas: dict[str, str], workers: int = 8) -> dict[str, str]:
        """Fetch many blobs concurrently: {path: sha} -> {path: text}."""
        if not shas:
            return {}
        if len(shas) == 1:
            path, sha = next(iter(shas.items()))
            return {path: self.blob_text(sha)}
        futures = {path: _POOL.submit(self.blob_text, sha) for path, sha in shas.items()}
        return {path: fut.result() for path, fut in futures.items()}

    def recent_commit_messages(self, branch: str, count: int = 8) -> list[dict[str, str]]:
        rows = self.request("GET", f"/commits?sha={branch}&per_page={count}")
        return [{"sha": r["sha"], "message": r["commit"]["message"]} for r in rows or []]

    # ------------------------------------------------------------ writing
    def create_tree_commit(self, *, parent: str, base_tree: str, changes: list[FileChange], message: str, workers: int = 8) -> dict[str, str]:
        def make_entry(change: FileChange) -> dict[str, Any]:
            if change.content is None:
                return {"path": change.path, "mode": change.mode, "type": "blob", "sha": None}
            sha = git_blob_sha(change.content)
            if sha not in self.known_blobs:  # content-addressed: never upload the same bytes twice
                blob = self.request("POST", "/git/blobs", {"content": base64.b64encode(change.content.encode("utf-8")).decode("ascii"), "encoding": "base64"})
                sha = blob["sha"]
                self.known_blobs.add(sha)
            return {"path": change.path, "mode": change.mode, "type": "blob", "sha": sha}

        entries = list(_POOL.map(make_entry, changes)) if len(changes) > 1 else [make_entry(c) for c in changes]
        tree = self.request("POST", "/git/trees", {"base_tree": base_tree, "tree": entries})
        commit = self.request("POST", "/git/commits", {"message": message, "tree": tree["sha"], "parents": [parent]})
        return {"commit": commit["sha"], "tree": tree["sha"]}

    def move_ref(self, branch: str, sha: str, *, create: bool = False) -> None:
        if create:
            self.request("POST", "/git/refs", {"ref": f"refs/heads/{branch}", "sha": sha})
        else:
            self.request("PATCH", f"/git/refs/heads/{branch}", {"sha": sha, "force": False})

    def delete_branch(self, branch: str) -> None:
        self.request("DELETE", f"/git/refs/heads/{branch}", ok=(200, 204, 422))


def operation_key(base_commit: str, changes: list[FileChange]) -> str:
    digest = hashlib.sha256()
    digest.update(base_commit.encode())
    for change in sorted(changes, key=lambda c: c.path):
        digest.update(b"\0" + change.path.encode() + b"\0")
        digest.update(b"DEL" if change.content is None else hashlib.sha256(change.content.encode()).digest())
    return digest.hexdigest()[:16]


TRAILER = "MCP-Op"


def with_trailer(message: str, key: str) -> str:
    return f"{message.rstrip()}\n\n{TRAILER}: {key}"


def find_trailer(messages: list[dict[str, str]], key: str) -> str | None:
    needle = f"{TRAILER}: {key}"
    for row in messages:
        if needle in row["message"]:
            return row["sha"]
    return None


@dataclass
class CommitOutcome:
    status: str  # pushed | already_pushed | conflict | no_changes
    commit: str | None = None
    tree: str | None = None
    attempts: int = 0
    rebased: bool = False
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    changes: list[FileChange] = field(default_factory=list)
    api_calls: int = 0


def push_changes(
    repo: GitHubRepo,
    branch: str,
    *,
    message: str,
    key: str,
    build_changes: Callable[[dict[str, str], Callable[[], dict[str, dict[str, str]]], bool], tuple[list[FileChange], list[dict[str, Any]]]],
    expected_base: str | None,
    max_rebases: int = 4,
) -> CommitOutcome:
    """Commit onto ``branch`` and move the ref without force, surviving a moving head.

    ``key`` is a deterministic idempotency key stored as a commit trailer, so a retried call whose
    first attempt actually landed returns ``already_pushed``. The trailer lookup and the recursive
    tree fetch are skipped on the hot path (head still equals ``expected_base``): nothing newer
    exists, so nothing can have landed and no merge is needed.

    ``build_changes(head, get_tree, moved)`` returns the final ``FileChange`` list relative to the
    *current* head plus unresolvable conflicts; ``get_tree()`` lazily fetches the head tree.
    """
    outcome = CommitOutcome(status="no_changes")
    for attempt in range(1, max_rebases + 2):
        outcome.attempts = attempt
        head = repo.head(branch)
        moved = expected_base is not None and head["commit"] != expected_base
        if moved or expected_base is None or attempt > 1:
            landed = find_trailer(repo.recent_commit_messages(branch), key)
            if landed:
                outcome.status, outcome.commit, outcome.api_calls = "already_pushed", landed, repo.calls
                return outcome
        cache: dict[str, dict[str, dict[str, str]]] = {}

        def get_tree(head=head, cache=cache) -> dict[str, dict[str, str]]:
            if "t" not in cache:
                cache["t"] = repo.tree(head["tree"])
            return cache["t"]

        changes, conflicts = build_changes(head, get_tree, moved)
        if conflicts:
            outcome.status, outcome.conflicts, outcome.api_calls = "conflict", conflicts, repo.calls
            return outcome
        if not changes:
            outcome.status, outcome.commit, outcome.api_calls = "no_changes", head["commit"], repo.calls
            return outcome
        created = repo.create_tree_commit(parent=head["commit"], base_tree=head["tree"], changes=changes, message=with_trailer(message, key))
        try:
            repo.move_ref(branch, created["commit"])
        except NonFastForward:
            outcome.rebased = True
            continue
        outcome.status, outcome.commit, outcome.tree, outcome.changes, outcome.api_calls = "pushed", created["commit"], created["tree"], changes, repo.calls
        outcome.rebased = outcome.rebased or moved
        return outcome
    outcome.status = "conflict"
    outcome.conflicts = [{"path": "*", "reason": "branch kept moving; retry limit reached", "retry": True}]
    outcome.api_calls = repo.calls
    return outcome

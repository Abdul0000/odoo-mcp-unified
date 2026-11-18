"""GitLab REST API adapter for the stateless Odoo coding workspace."""
from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
import json
import re
import urllib.parse
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import Any, Callable

from .github_commit_service import FileChange, GitHubError, CommitOutcome, git_blob_sha


def parse_gitlab_repo(url: str) -> tuple[str, str]:
    raw = (url or "").strip()
    if raw.startswith("git@") and ":" in raw:
        host, path = raw[4:].split(":", 1)
        base = "https://" + host
    else:
        parsed = urllib.parse.urlsplit(raw if "://" in raw else "https://" + raw)
        if not parsed.hostname:
            raise ValueError("invalid GitLab repository URL")
        host, path, base = parsed.hostname, parsed.path.lstrip("/"), f"{parsed.scheme}://{parsed.netloc}"
    path = re.sub(r"\.git$", "", path).strip("/")
    if not path or "/" not in path:
        raise ValueError("GitLab URL must include a group and project path")
    if host.lower() in {"github.com", "www.github.com"}:
        raise ValueError("repository is hosted on GitHub, not GitLab")
    return base.rstrip("/"), path


class GitLabRepo:
    """Implements the workspace repository contract using GitLab's v4 API."""

    def __init__(self, base_url: str, project_path: str, token: str, transport=None):
        self.base_url = base_url.rstrip("/")
        self.project_path = project_path
        self.project = urllib.parse.quote(project_path, safe="")
        self.token = token
        self.transport = transport or self._transport
        self.calls = 0
        self._default_branch = None
        self.known_blobs: set[str] = set()
        self._trees: dict[str, dict] = {}

    def _transport(self, method, url, headers, body):
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                raw = response.read(8_000_001)
                if len(raw) > 8_000_000:
                    raise GitHubError(413, "GitLab response exceeded bounded transport size")
                try:
                    data = json.loads(raw) if raw else None
                except (json.JSONDecodeError, UnicodeDecodeError):
                    data = raw.decode("utf-8", "replace")
                return response.status, {k.lower(): v for k, v in response.headers.items()}, data
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try: data = json.loads(raw) if raw else {}
            except Exception: data = {"message": raw.decode("utf-8", "replace")[:500]}
            return exc.code, {k.lower(): v for k, v in exc.headers.items()}, data
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return 599, {}, {"message": str(exc)}

    def request(self, method: str, path: str, payload=None, *, ok=(200, 201, 202, 204)):
        url = path if path.startswith("http") else f"{self.base_url}/api/v4/projects/{self.project}{path}"
        headers = {"PRIVATE-TOKEN": self.token, "Accept": "application/json", "User-Agent": "odoo-mcp-agent"}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        self.calls += 1
        status, _, data = self.transport(method, url, headers, body)
        if status in ok:
            return data
        message = data.get("message", str(data)) if isinstance(data, dict) else str(data)
        if status == 409 or (status == 400 and any(word in str(message).lower() for word in ("branch", "changed", "last_commit", "already exists", "does not exist"))):
            raise GitHubError(422, str(message), path=path)
        raise GitHubError(status, str(message), retryable=status >= 500 or status == 599, path=path)

    def web_url(self, kind="", ref=""):
        base = f"{self.base_url}/{self.project_path}"
        if kind == "tree": return f"{base}/-/tree/{urllib.parse.quote(ref, safe='/')}"
        if kind == "commit": return f"{base}/-/commit/{ref}"
        return base

    def default_branch(self):
        if self._default_branch is None:
            project = self.request("GET", "") or {}
            self._default_branch = project.get("default_branch") or ""
        return self._default_branch or None

    def head(self, branch: str):
        encoded = urllib.parse.quote(branch, safe="")
        row = self.request("GET", f"/repository/branches/{encoded}")
        commit = row["commit"]
        return {"commit": commit["id"], "tree": commit["id"], "message": commit.get("message", "")}

    def ref_exists(self, branch: str):
        try:
            self.head(branch)
            return True
        except GitHubError as exc:
            if exc.status == 404: return False
            raise

    def tree(self, tree_sha: str):
        immutable = bool(re.fullmatch(r"[0-9a-f]{40,64}", tree_sha))
        if immutable and tree_sha in self._trees:
            return {p: dict(v) for p, v in self._trees[tree_sha].items()}
        rows, page = {}, 1
        while True:
            data = self.request("GET", f"/repository/tree?ref={urllib.parse.quote(tree_sha, safe='')}&recursive=true&per_page=100&page={page}")
            if not data: break
            for item in data:
                if item.get("type") == "blob":
                    rows[item["path"]] = {"sha": item["id"], "mode": item.get("mode", "100644"), "size": str(item.get("size", 0))}
                    self.known_blobs.add(item["id"])
            if len(data) < 100: break
            page += 1
        if immutable:
            if len(self._trees) >= 8:
                self._trees.pop(next(iter(self._trees)))
            self._trees[tree_sha] = rows
        return {p: dict(v) for p, v in rows.items()}

    def _directory_blobs(self, tree_sha, prefix):
        rows, page = {}, 1
        while True:
            query = urllib.parse.urlencode({"ref": tree_sha, "path": prefix, "recursive": "false", "per_page": 100, "page": page})
            data = self.request("GET", f"/repository/tree?{query}") or []
            for item in data:
                if item.get("type") == "blob":
                    rows[item["path"]] = {"sha": item["id"], "mode": item.get("mode", "100644"), "size": str(item.get("size", 0))}
                    self.known_blobs.add(item["id"])
            if len(data) < 100:
                return rows
            page += 1

    def tree_at(self, tree_sha: str, path: str):
        prefix = path.strip("/")
        rows = self._trees.get(tree_sha)
        if rows is None:
            rows = self._directory_blobs(tree_sha, prefix)
        offset = len(prefix) + 1 if prefix else 0
        return {p[offset:]: {**v, "type": "blob"} for p, v in rows.items()
                if (not prefix or p.startswith(prefix + "/")) and "/" not in p[offset:]}

    def root_blob(self, tree_sha: str, name: str):
        rows = self._trees.get(tree_sha)
        if rows is None:
            rows = self._directory_blobs(tree_sha, "")
        return rows.get(name, {}).get("sha")

    def blob_text(self, sha: str):
        path = f"/repository/blobs/{urllib.parse.quote(sha, safe='')}/raw"
        data = self.request("GET", path)
        text = data if isinstance(data, str) else str(data or "")
        if len(text.encode("utf-8")) > 400_000:
            raise GitHubError(413, "Blob exceeds workspace file size limit; use native execution", path=sha)
        return text

    def blobs_text(self, shas: dict[str, str], workers: int = 8):
        if not shas:
            return {}
        with ThreadPoolExecutor(max_workers=max(1, min(workers, 4)), thread_name_prefix="gitlab_blob") as pool:
            return dict(zip(shas, pool.map(self.blob_text, shas.values())))

    def recent_commit_messages(self, branch: str, count: int = 8):
        ref = urllib.parse.quote(branch, safe="")
        rows = self.request("GET", f"/repository/commits?ref_name={ref}&per_page={count}")
        return [{"sha": row["id"], "message": row.get("message", "")} for row in rows or []]

    def commit_to_branch(self, branch: str, changes: list[FileChange], message: str, *, expected_head: str | None = None, create_from: str | None = None):
        exists = self.ref_exists(branch)
        if not exists:
            if not create_from:
                raise GitHubError(404, f"branch '{branch}' does not exist")
            self.request("POST", "/repository/branches", {"branch": branch, "ref": create_from})
        current = self.head(branch)["commit"]
        if expected_head and current != expected_head:
            raise GitHubError(409, "branch head changed before commit", path=branch)
        current_tree = self.tree(current)
        actions = []
        for change in changes:
            action = "delete" if change.content is None else ("update" if change.path in current_tree else "create")
            row = {"action": action, "file_path": change.path}
            if action in {"update", "delete"}:
                encoded = urllib.parse.quote(change.path, safe="")
                metadata = self.request("GET", f"/repository/files/{encoded}?ref={urllib.parse.quote(current, safe='')}")
                last_commit = metadata.get("last_commit_id")
                if not last_commit:
                    raise GitHubError(409, "Missing file concurrency proof; use native Git execution", path=change.path)
                row["last_commit_id"] = last_commit
            if change.content is not None: row["content"] = change.content
            actions.append(row)
        if not actions:
            return {"commit": current, "tree": current}
        result = self.request("POST", "/repository/commits", {"branch": branch, "commit_message": message, "actions": actions})
        return {"commit": result["id"], "tree": result["id"]}

    def push_workspace_changes(self, branch, *, message, key, build_changes, expected_base, max_rebases=2):
        outcome = CommitOutcome(status="no_changes")
        for attempt in range(1, max_rebases + 2):
            outcome.attempts = attempt
            head = self.head(branch)
            moved = expected_base is not None and head["commit"] != expected_base
            landed = next((row["sha"] for row in self.recent_commit_messages(branch, 30) if ("MCP-Op: " + key) in row["message"]), None)
            if landed:
                outcome.status, outcome.commit, outcome.api_calls = "already_pushed", landed, self.calls
                return outcome
            cache = {}
            def get_tree():
                if "tree" not in cache: cache["tree"] = self.tree(head["tree"])
                return cache["tree"]
            changes, conflicts = build_changes(head, get_tree, moved)
            if conflicts:
                outcome.status, outcome.conflicts = "conflict", conflicts
                outcome.api_calls = self.calls
                return outcome
            if not changes:
                outcome.status, outcome.commit = "no_changes", head["commit"]
                outcome.api_calls = self.calls
                return outcome
            try:
                created = self.commit_to_branch(branch, changes, f"{message}\n\nMCP-Op: {key}", expected_head=head["commit"])
            except GitHubError as exc:
                if exc.status in (409, 422) and attempt <= max_rebases: 
                    outcome.rebased = True
                    continue
                raise
            outcome.status, outcome.commit, outcome.tree = "pushed", created["commit"], created["tree"]
            outcome.changes, outcome.rebased, outcome.api_calls = changes, moved or outcome.rebased, self.calls
            return outcome
        outcome.status = "conflict"
        outcome.conflicts = [{"path": "*", "reason": "GitLab branch kept moving; retry limit reached", "retry": True}]
        outcome.api_calls = self.calls
        return outcome

    def move_ref(self, branch: str, sha: str, *, create: bool = False):
        if not create:
            current = self.head(branch)["commit"]
            if current == sha:
                return
            raise GitHubError(422, "GitLab ref updates are performed through branch commits, not detached SHA updates", path=branch)
        self.request("POST", "/repository/branches", {"branch": branch, "ref": sha})

    def delete_branch(self, branch: str):
        encoded = urllib.parse.quote(branch, safe="")
        self.request("DELETE", f"/repository/branches/{encoded}", ok=(200, 202, 204))

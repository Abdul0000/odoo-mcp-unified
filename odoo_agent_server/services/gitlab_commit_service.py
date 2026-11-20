"""GitLab REST API adapter for the stateless Odoo coding workspace."""
from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
import json
import os
import subprocess
import tempfile
import re
import urllib.parse
import urllib.request
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .github_commit_service import FileChange, GitHubError, CommitOutcome, git_blob_sha


class GitLabError(GitHubError):
    """Provider-labelled error that remains compatible with shared workspace handling."""

    def __init__(self, status: int, message: str, *, retryable: bool = False, path: str = ""):
        RuntimeError.__init__(self, f"GitLab {status} {path}: {message}")
        self.status = status
        self.message = message
        self.retryable = retryable
        self.path = path


def is_gitlab_repo_url(url: str) -> bool:
    """Choose GitLab from the URL host, never from a group/project path segment."""
    raw = (url or "").strip()
    if raw.startswith("git@") and ":" in raw:
        host = raw[4:].split(":", 1)[0]
    else:
        parsed = urllib.parse.urlsplit(raw if "://" in raw else "https://" + raw)
        host = parsed.hostname or ""
    host = host.lower().rstrip(".")
    return host == "gitlab.com" or host.startswith("gitlab.")


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
                    raise GitLabError(413, "response exceeded bounded transport size")
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
        message_lower = str(message).lower()
        concurrency_conflict = any(
            marker in message_lower
            for marker in (
                "last_commit", "last commit", "changed since", "branch changed",
                "branch has changed", "already exists", "does not exist",
            )
        )
        if status == 409 or (status == 400 and concurrency_conflict):
            raise GitLabError(422, str(message), path=path)
        raise GitLabError(status, str(message), retryable=status >= 500 or status == 599, path=path)

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
            raise GitLabError(413, "blob exceeds workspace file size limit; use native execution", path=sha)
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
                raise GitLabError(404, f"branch '{branch}' does not exist")
            self.request("POST", "/repository/branches", {"branch": branch, "ref": create_from})
        current = self.head(branch)["commit"]
        if expected_head and current != expected_head:
            raise GitLabError(409, "branch head changed before commit", path=branch)
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
                    raise GitLabError(409, "missing file concurrency proof; use native Git execution", path=change.path)
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
            raise GitLabError(422, "ref updates are performed through branch commits, not detached SHA updates", path=branch)
        self.request("POST", "/repository/branches", {"branch": branch, "ref": sha})

    def delete_branch(self, branch: str):
        encoded = urllib.parse.quote(branch, safe="")
        self.request("DELETE", f"/repository/branches/{encoded}", ok=(200, 202, 204))


class GitLabNativeRepo(GitLabRepo):
    """Git transport implementation used when GitLab REST PAT scopes are too narrow.

    All reads and writes operate on Git objects/private refs. The configured
    checkout's worktree, index, and checked-out branch are never changed.
    """

    def __init__(self, client, base_url: str, project_path: str):
        super().__init__(base_url, project_path, client.token or "")
        from .git_identity import same_repository
        self.path = Path(client.path).resolve()
        if not (self.path / ".git").exists():
            raise GitLabError(409, "validated client checkout is unavailable for Git transport fallback")
        origin = self._run("remote", "get-url", "origin").strip()
        configured = client.repo or ""
        if not same_repository(origin, configured):
            raise GitLabError(409, "client checkout origin does not match the configured GitLab repository")
        self.remote_url = client.authenticated_repo
        if not self.remote_url:
            raise GitLabError(403, "Git transport fallback requires configured repository credentials")

    def _run(self, *args, env=None, input_bytes=None, check=True):
        process_env = os.environ.copy()
        process_env["GIT_TERMINAL_PROMPT"] = "0"
        if env:
            process_env.update(env)
        result = subprocess.run(["git", "-C", str(self.path), *args], input=input_bytes,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=process_env)
        if check and result.returncode:
            # Never include remote URL or credential-bearing process arguments.
            message = result.stderr.decode("utf-8", "replace")[-600:]
            message = re.sub(r"https?://[^\s@]+:[^\s@]+@", "https://[redacted]@", message)
            raise GitLabError(409 if "non-fast-forward" in message.lower() else 502,
                              message or "Git transport operation failed")
        return result.stdout.decode("utf-8", "replace")

    @staticmethod
    def _check_branch(branch):
        valid = subprocess.run(["git", "check-ref-format", "--branch", branch or ""],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if not branch or branch.startswith("-") or valid.returncode:
            raise GitLabError(400, "invalid branch name", path=branch or "")

    def _fetch(self, branch):
        self._check_branch(branch)
        ref = f"refs/mcp/gitlab/{uuid4().hex}/head"
        self._run("fetch", "--no-tags", self.remote_url, f"+refs/heads/{branch}:{ref}")
        return ref, self._run("rev-parse", ref).strip()

    def _drop_ref(self, ref):
        self._run("update-ref", "-d", ref, check=False)

    def default_branch(self):
        if self._default_branch is None:
            symbolic = self._run("ls-remote", "--symref", self.remote_url, "HEAD")
            match = re.search(r"ref: refs/heads/(.+)\tHEAD", symbolic)
            self._default_branch = match.group(1) if match else ""
        return self._default_branch or None

    def head(self, branch):
        ref, sha = self._fetch(branch)
        try:
            tree = self._run("rev-parse", f"{sha}^{{tree}}").strip()
            return {"commit": sha, "tree": tree, "message": self._run("show", "-s", "--format=%s", sha).strip()}
        finally:
            self._drop_ref(ref)

    def ref_exists(self, branch):
        ref = None
        try:
            ref, _ = self._fetch(branch)
            return True
        except GitLabError as exc:
            if exc.status in (409, 502) and "couldn't find remote ref" in exc.message.lower():
                return False
            raise
        finally:
            if ref:
                self._drop_ref(ref)

    def tree(self, tree_sha):
        output = self._run("ls-tree", "-r", "-z", "--full-tree", tree_sha)
        rows = {}
        for item in output.split("\0"):
            if not item:
                continue
            metadata, path = item.split("\t", 1)
            mode, kind, sha = metadata.split()
            if kind == "blob":
                size = self._run("cat-file", "-s", sha).strip()
                rows[path] = {"sha": sha, "mode": mode, "size": size}
        return rows

    def blob_text(self, sha):
        if not re.fullmatch(r"[0-9a-f]{40,64}", sha):
            raise GitLabError(400, "invalid blob id")
        result = subprocess.run(["git", "-C", str(self.path), "cat-file", "blob", sha],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode:
            raise GitLabError(404, "blob not found", path=sha)
        if len(result.stdout) > 400_000:
            raise GitLabError(413, "blob exceeds workspace file size limit; use native execution", path=sha)
        return result.stdout.decode("utf-8", "replace")

    def recent_commit_messages(self, branch, count=8):
        ref, sha = self._fetch(branch)
        try:
            rows = self._run("log", f"-{max(1, min(count, 100))}", "--format=%H%x09%s", sha)
            return [{"sha": line.split("\t", 1)[0], "message": line.split("\t", 1)[1] if "\t" in line else ""}
                    for line in rows.splitlines()]
        finally:
            self._drop_ref(ref)

    def commit_to_branch(self, branch, changes, message, *, expected_head=None, create_from=None):
        ref, current = self._fetch(branch)
        try:
            if expected_head and current != expected_head:
                raise GitLabError(409, "branch head changed before commit", path=branch)
            if not changes:
                return {"commit": current, "tree": current}
            with tempfile.TemporaryDirectory(prefix="odoo-mcp-git-index-") as tmp:
                index = str(Path(tmp) / "index")
                env = {"GIT_INDEX_FILE": index}
                self._run("read-tree", current, env=env)
                current_tree = self.tree(current)
                for change in changes:
                    if change.content is None:
                        self._run("update-index", "--force-remove", "--", change.path, env=env)
                        continue
                    content = change.content.encode("utf-8")
                    blob = subprocess.run(["git", "-C", str(self.path), "hash-object", "-w", "--stdin"],
                                          input=content, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    if blob.returncode:
                        raise GitLabError(502, "failed to create Git blob", path=change.path)
                    old = current_tree.get(change.path, {})
                    mode = old.get("mode", "100644")
                    self._run("update-index", "--add", "--cacheinfo", mode,
                              blob.stdout.decode().strip(), change.path, env=env)
                tree_sha = self._run("write-tree", env=env).strip()
                commit_env = {"GIT_AUTHOR_NAME": "Odoo MCP", "GIT_AUTHOR_EMAIL": "odoo-mcp@localhost",
                              "GIT_COMMITTER_NAME": "Odoo MCP", "GIT_COMMITTER_EMAIL": "odoo-mcp@localhost"}
                commit = subprocess.run(["git", "-C", str(self.path), "commit-tree", tree_sha, "-p", current],
                                        input=message.encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        env={**os.environ, **commit_env})
                if commit.returncode:
                    raise GitLabError(502, "failed to create Git commit")
                sha = commit.stdout.decode().strip()
            self._run("push", self.remote_url, f"{sha}:refs/heads/{branch}")
            return {"commit": sha, "tree": sha}
        finally:
            self._drop_ref(ref)

    def move_ref(self, branch, sha, *, create=False):
        raise GitLabError(422, "detached ref updates are not supported", path=branch)

    def delete_branch(self, branch):
        self._run("push", self.remote_url, f":refs/heads/{branch}")

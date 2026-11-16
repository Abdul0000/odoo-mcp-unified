"""Task-local clones, bounded file tools and durable command/agent submission.

The API never executes user commands. The separate worker runs them in Docker,
with only the task clone mounted, no host credentials and no network.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
from urllib.parse import urlsplit, urlunsplit

from fastapi import HTTPException

from ..config import settings, mask_repo_url
from ..execution_schemas import WorkspaceFiles
from .client_resolver_service import ClientResolverService
from .execution_store import ExecutionStore
from .path_service import resolve_allowed_path
from .repo_service import RepoService
from .approval_service import ApprovalService


def enabled():
    if not settings.execution_enabled:
        raise HTTPException(503, "Task execution is disabled; configure ODOO_AGENT_EXECUTION_ENABLED and start the execution worker")


def git(argv, cwd, *, timeout=60, env=None):
    """Trusted Git control operations, with bounded in-memory output."""
    clean_env = {"PATH": os.defpath, "HOME": str(Path.home()), "GIT_TERMINAL_PROMPT": "0"}
    if env:
        clean_env.update(env)
    with tempfile.TemporaryFile() as output:
        result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *argv], cwd=cwd,
                                env=clean_env, stdout=output, stderr=subprocess.STDOUT, timeout=timeout)
        output.seek(0)
        text = mask_repo_url(output.read(32001).decode("utf-8", errors="replace"))
    if result.returncode:
        raise HTTPException(409, f"Git operation failed: {text[:2000]}")
    return text[:32000]


def _plain_remote(url):
    if "://" not in url:
        return url
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, "", ""))


class ExecutionService:
    def __init__(self):
        enabled()
        self.store = ExecutionStore()

    def workspace(self, workspace_id):
        record = self.store.get(workspace_id, "workspace")
        if record["status"] != "ready":
            raise HTTPException(409, "Workspace is not ready")
        return record

    def root(self, workspace_id):
        return self.store.root / "workspaces" / workspace_id / "repo"

    def target(self, workspace_id, relative, *, directory=False):
        self.workspace(workspace_id)
        root = self.root(workspace_id).resolve()
        rel = Path(relative)
        if rel.is_absolute() or "\x00" in relative or any(p in {"..", ".git"} for p in rel.parts):
            raise HTTPException(400, "Use a task-relative path outside Git internals")
        target = (root / rel).resolve()
        if not target.is_relative_to(root):
            raise HTTPException(403, "Path escapes task workspace")
        # Never expose symlinked Git metadata through an innocent alias.
        if ".git" in target.relative_to(root).parts:
            raise HTTPException(403, "Git internals are not exposed by file tools")
        if directory and not target.is_dir():
            raise HTTPException(404, "Working directory not found")
        return target

    def open(self, request):
        client = ClientResolverService().resolve_client(request.client_name, project_path=request.project_path, require_complete=False)
        source_raw = request.project_path or (client.path if client else None) or settings.target_project_path
        if not source_raw:
            raise HTTPException(400, "Select a client or supply an existing project_path")
        source = resolve_allowed_path(source_raw)
        if not (source / ".git").exists():
            raise HTTPException(409, "Prepare this client's repository with client_check(validate=true) before opening a task")
        record, created = self.store.create("workspace", request.request_id, request.model_dump(), status="initializing")
        if not created:
            return self.status(record["id"]) if record["status"] == "ready" else self.public(record)
        path = self.root(record["id"])
        try:
            path.parent.mkdir(parents=True, mode=0o770)
            base = git(["rev-parse", "HEAD"], source).strip()
            remote = _plain_remote(git(["remote", "get-url", "origin"], source).strip())
            if client and client.repo and not RepoService()._remote_matches(remote, client.repo):
                raise HTTPException(409, "Source checkout remote differs from the selected client")
            git(["clone", "--no-hardlinks", "--no-checkout", "--", str(source), str(path)], source)
            branch = f"codex/task-{record['id'][:12]}"
            git(["checkout", "-b", branch, base], path)
            git(["remote", "set-url", "origin", remote], path)
            record = self.store.update(record["id"], "workspace", status="ready", client_name=client.name if client else None,
                                       source_path=str(source), branch=branch, base_sha=base,
                                       deploy_branch=client.branch if client else settings.target_git_branch)
            return self.status(record["id"])
        except Exception:
            self.store.update(record["id"], "workspace", status="failed", error="Workspace initialization failed; use a new request_id after correcting the source")
            raise

    @staticmethod
    def public(record):
        return {k: v for k, v in record.items() if k not in {"request", "source_path", "conversation", "pending_response"}}

    def status(self, workspace_id):
        record = self.workspace(workspace_id)
        dirty = git(["status", "--short"], self.root(workspace_id))
        return {**self.public(record), "workspace_id": record["id"], "changes": dirty,
                "deployment": "not_deployed", "next_call": {"tool": "execution_files", "arguments": {"workspace_id": workspace_id}}}

    def files(self, request: WorkspaceFiles):
        # File reads also wait for commands: a running container could otherwise
        # swap a path into a symlink between containment checking and opening.
        with self.store.workspace_lock(request.workspace_id):
            self.assert_idle(request.workspace_id)
            return self._files(request)

    def assert_idle(self, workspace_id):
        if any(record["request"]["workspace_id"] == workspace_id for record in self.store.pending("process")):
            raise HTTPException(409, "Workspace has a queued/running command; poll or cancel it before editing or publishing")

    def _files(self, request: WorkspaceFiles):
        target = self.target(request.workspace_id, request.path)
        if request.operation == "diff":
            return {"diff": git(["diff", "--no-ext-diff", "--no-textconv", "HEAD", "--", request.path], self.root(request.workspace_id)),
                    "note": "Untracked files appear in execution_workspace_status.changes; read them separately."}
        if request.operation == "read":
            if not target.is_file():
                raise HTTPException(404, "File not found")
            if target.stat().st_size > 2_000_000:
                raise HTTPException(413, "File exceeds the 2 MB file-tool limit")
            raw = target.read_bytes()
            return {"path": request.path, "content": raw[request.offset:request.offset + request.limit].decode("utf-8", errors="replace"),
                    "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw),
                    "next_offset": min(len(raw), request.offset + request.limit)}
        if request.operation == "write":
            if request.content is None:
                raise HTTPException(400, "content is required for write")
            if target.exists() and not target.is_file():
                raise HTTPException(400, "Target is not a file")
            if target.exists() and target.stat().st_size > 2_000_000:
                raise HTTPException(413, "Existing file exceeds the 2 MB limit")
            old_hash = hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else None
            if old_hash != request.expected_sha256:
                raise HTTPException(409, "Read the current file and supply its expected_sha256; omit it only for a new file")
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as f:
                temp = Path(f.name)
                f.write(request.content.encode())
            if target.exists():
                temp.chmod(target.stat().st_mode & 0o777)
            else:
                temp.chmod(0o660)
            temp.replace(target)
            return {"status": "written", "path": request.path, "sha256": hashlib.sha256(request.content.encode()).hexdigest(), "deployment": "not_deployed"}
        root = self.root(request.workspace_id).resolve()
        if request.operation == "search" and not request.query:
            raise HTTPException(400, "query is required for search")
        results, scanned, budget, truncated = [], 0, 0, False
        candidates = [target] if target.is_file() else self._walk(target)
        for p in candidates:
            if p.is_symlink() or not p.is_file():
                continue
            scanned += 1
            if scanned > 2000 or budget > 5_000_000 or len(results) >= 200:
                truncated = True
                break
            rel = str(p.relative_to(root))
            if request.operation == "list":
                results.append(rel)
            elif p.stat().st_size <= 500000:
                raw = p.read_bytes()
                budget += len(raw)
                if b"\0" in raw:
                    continue
                for line, text in enumerate(raw.decode("utf-8", errors="replace").splitlines(), 1):
                    if request.query in text:
                        results.append({"path": rel, "line": line, "text": text[:500]})
                        if len(results) >= 200:
                            truncated = True
                            break
        return {"results": results, "truncated": truncated, "files_scanned": scanned}

    @staticmethod
    def _walk(path):
        for parent, dirs, files in os.walk(path):
            dirs[:] = sorted(d for d in dirs if d not in {".git", "node_modules", ".venv", "__pycache__"} and not Path(parent, d).is_symlink())
            for name in sorted(files):
                yield Path(parent, name)

    def start_process(self, request):
        self.target(request.workspace_id, request.cwd, directory=True)
        if any("\0" in arg or len(arg) > 16000 for arg in request.argv):
            raise HTTPException(400, "Invalid command argument")
        with self.store.workspace_lock(request.workspace_id):
            existing = self.store.existing("process", request.request_id, request.model_dump())
            if existing:
                return self.poll(existing["id"])
            self.assert_idle(request.workspace_id)
            record, _ = self.store.create("process", request.request_id, request.model_dump())
        return self.poll(record["id"])

    def publish(self, request):
        with self.store.workspace_lock(request.workspace_id):
            existing = self.store.existing("publish", request.request_id, request.model_dump())
            if existing:
                return self.public(existing)
            self.assert_idle(request.workspace_id)
            return self._publish(request)

    def _publish(self, request):
        workspace = self.workspace(request.workspace_id)
        client = ClientResolverService().resolve_client(workspace.get("client_name"), project_path=workspace["source_path"], require_complete=False)
        branch = request.branch or workspace["branch"]
        if not RepoService()._safe_branch_name(branch):
            raise HTTPException(400, "Invalid publish branch")
        if branch not in {workspace["branch"], workspace.get("deploy_branch")}:
            raise HTTPException(409, "Publish only to the task branch or configured deployment branch")
        approvals = ApprovalService()
        protected = {"main", "master", "production", "prod", "live"} | settings.protected_branches
        approvals.ensure_allowed("git.push" if request.push else "git.commit", request.approval_id,
                                 request.model_dump(exclude={"approval_id"}), force=request.push and branch.lower() in protected)
        record, created = self.store.create("publish", request.request_id, request.model_dump(), status="running")
        if not created:
            return self.public(record)
        root = self.root(request.workspace_id)
        try:
            if git(["status", "--porcelain"], root).strip():
                git(["add", "-A", "--", "."], root)
                git(["-c", f"user.name={settings.git_author_name}", "-c", f"user.email={settings.git_author_email}", "commit", "-m", request.message], root)
            sha = git(["rev-parse", "HEAD"], root).strip()
            if request.push:
                remote = git(["remote", "get-url", "origin"], root).strip()
                expected = client.repo if client else git(["remote", "get-url", "origin"], workspace["source_path"]).strip()
                if not RepoService()._remote_matches(remote, expected):
                    raise HTTPException(409, "Task remote differs from configured source")
                target = client.authenticated_repo if client else remote
                git(["push", "--", target, f"HEAD:refs/heads/{branch}"], root, timeout=120)
                received = git(["ls-remote", "--heads", "--", target, f"refs/heads/{branch}"], root).split()
                if not received or received[0] != sha:
                    raise HTTPException(409, "Push returned without confirming the expected remote SHA")
            approvals.mark_consumed(request.approval_id)
            result = self.store.update(record["id"], "publish", status="succeeded", commit=sha, branch=branch,
                                       pushed=request.push, deployment="not_deployed",
                                       next_action="Use the existing Odoo deployment workflow with this expected SHA; push alone is not deployment.")
            return self.public(result)
        except Exception:
            approvals.mark_failed(request.approval_id)
            self.store.update(record["id"], "publish", status="needs_review", error="Publish interrupted or rejected; inspect local and remote SHA before submitting a new request_id")
            raise

    def poll(self, process_id, offset=0, max_bytes=16000):
        record = self.store.get(process_id, "process")
        log = self.store.root / "logs" / f"{process_id}.log"
        chunk = b""
        if log.exists():
            with log.open("rb") as f:
                f.seek(offset)
                chunk = f.read(max_bytes)
        return {**self.public(record), "process_id": record["id"], "output": chunk.decode("utf-8", errors="replace"),
                "next_offset": offset + len(chunk), "success_allowed": record["status"] == "succeeded",
                "next_call": {"tool": "execution_process_poll", "arguments": {"process_id": process_id, "offset": offset + len(chunk)}} if record["status"] in {"queued", "running"} else None}

    def cancel(self, record_id, kind):
        record = self.store.get(record_id, kind)
        if record["status"] in {"queued", "running"}:
            record = self.store.update(record_id, kind, cancel_requested=True,
                                       status="cancelled" if record["status"] == "queued" else None)
            if kind == "agent":
                for process in self.store.pending("process"):
                    if process.get("agent_task_id") == record_id:
                        self.cancel(process["id"], "process")
        return self.public(record)

    def start_agent(self, request):
        self.workspace(request.workspace_id)
        if not settings.agent_api_key or not settings.agent_model:
            return {"status": "configuration_required", "success_allowed": False,
                    "missing": [key for key, present in [("OPENAI_API_KEY", settings.agent_api_key), ("OPENAI_MODEL", settings.agent_model)] if not present],
                    "message": "Configure the worker environment. ChatGPT can already use execution_files and execution_process_start directly."}
        record, _ = self.store.create("agent", request.request_id, request.model_dump(), data={"turns": 0})
        return self.agent_status(record["id"])

    def agent_status(self, task_id):
        record = self.store.get(task_id, "agent")
        active = record["status"] in {"running", "queued"}
        return {**self.public(record), "task_id": record["id"], "success_allowed": False,
                "completion_required": active,
                "next_call": {"tool": "execution_agent_status", "arguments": {"task_id": task_id}} if active else None,
                "note": "Agent completion is not verification or deployment proof; inspect its tests and workspace diff."}

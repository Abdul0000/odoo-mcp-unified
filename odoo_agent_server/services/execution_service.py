"""Task-local clones, bounded file tools and durable command/agent submission.

The API never executes user commands. The separate worker runs them in Docker,
with only the task clone mounted, no host credentials and no network.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import signal
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
from .skill_service import SkillService


def enabled():
    if not settings.execution_enabled:
        raise HTTPException(503, "Task execution is disabled; configure ODOO_AGENT_EXECUTION_ENABLED and start the execution worker")


def git(argv, cwd, *, timeout=60, env=None):
    """Trusted Git control operations, with bounded in-memory output."""
    clean_env = {"PATH": os.defpath, "HOME": str(Path.home()), "GIT_TERMINAL_PROMPT": "0",
                 "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    if env:
        clean_env.update(env)
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(["git", "-c", f"safe.directory={Path(cwd).resolve()}",
                                    "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", *argv], cwd=cwd,
                                   env=clean_env, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            raise HTTPException(409, "Git operation timed out; subprocess tree stopped and source preserved") from exc
        output.seek(0)
        text = mask_repo_url(output.read(32001).decode("utf-8", errors="replace"))
    if process.returncode:
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
            if git(["status", "--porcelain"], source).strip():
                raise HTTPException(409, "Source has uncommitted changes; they were preserved. Commit or stash them before opening a fresh task.")
            path.parent.mkdir(parents=True, mode=0o770)
            base = git(["rev-parse", "HEAD"], source).strip()
            remote = _plain_remote(git(["remote", "get-url", "origin"], source).strip())
            if client and client.repo and not RepoService()._remote_matches(remote, client.repo):
                raise HTTPException(409, "Source checkout remote differs from the selected client")
            source_branch = git(["branch", "--show-current"], source).strip()
            if client and source_branch != client.branch:
                raise HTTPException(409, "Source checkout branch differs from the configured client branch; source preserved")
            # Local clones hard-link immutable Git objects; worktree files and the
            # index stay independent. Containers mount all Git metadata read-only.
            git(["clone", "--local", "--no-checkout", "--", str(source), str(path)], source)
            configured_branch = client.branch if client else source_branch
            branch = request.branch or configured_branch
            if not RepoService()._safe_branch_name(branch):
                raise HTTPException(409, "Select a valid branch; detached source checkouts need an explicit branch")
            git(["checkout", "-B", branch, base], path)
            git(["remote", "set-url", "origin", remote], path)
            remote_base = None
            if request.fresh:
                target = client.authenticated_repo if client else remote
                # Fetch only in the isolated task. The user's source branch and files never move.
                # Do not persist authenticated URLs in FETCH_HEAD or reflogs: commands
                # can read task Git metadata even though it is mounted read-only.
                prepared_ref = "refs/odoo-mcp/prepared"
                git(["-c", "core.logAllRefUpdates=false", "fetch", "--no-tags", "--no-write-fetch-head", "--",
                     target, f"refs/heads/{configured_branch}:{prepared_ref}"], path, timeout=120)
                remote_base = git(["rev-parse", prepared_ref], path).strip()
                git(["merge", "--ff-only", prepared_ref], path)
                base = git(["rev-parse", "HEAD"], path).strip()
                if base != remote_base:
                    raise HTTPException(409, "Source has unpublished commits; preserved. Publish/reconcile them before starting a fresh task")
            record = self.store.update(record["id"], "workspace", status="ready", client_name=client.name if client else None,
                                       source_path=str(source), branch=branch, base_sha=base,
                                       remote_base_sha=remote_base, source_branch=configured_branch, phase="inspect",
                                       deploy_branch=client.branch if client else configured_branch)
            return self.status(record["id"])
        except Exception:
            self.store.update(record["id"], "workspace", status="failed", error="Workspace initialization failed; use a new request_id after correcting the source")
            raise

    @staticmethod
    def public(record):
        return {k: v for k, v in record.items() if k not in {"request", "source_path", "conversation", "pending_response"}}

    def status(self, workspace_id):
        with self.store.workspace_lock(workspace_id):
            return self._status(workspace_id)

    def _status(self, workspace_id):
        record = self.workspace(workspace_id)
        dirty = git(["status", "--short"], self.root(workspace_id))
        pending = [p for p in self.store.pending("process") if p["request"]["workspace_id"] == workspace_id]
        busy = bool(pending)
        verification = self.verification(workspace_id) if not busy else {"ready": False, "reason": "command_pending"}
        phase = record.get("phase", "inspect")
        if phase in {"commit", "done"} and not busy and record.get("published_tree") != verification.get("tree"):
            phase = "edit"
        if verification.get("ready") and phase in {"inspect", "edit", "verify", "repair"}:
            phase = "verify"
        next_call = {"tool": "execution_files", "arguments": {"workspace_id": workspace_id}}
        if not record.get("checks"):
            next_call = {"tool": "execution_task_plan", "arguments": {"workspace_id": workspace_id}, "requires": ["checks"]}
        elif verification.get("next_check") and phase not in {"inspect", "repair"}:
            check = verification["next_check"]
            next_call = {"tool": "execution_process_start", "arguments": {"workspace_id": workspace_id, **check,
                         "check_name": check["name"], "request_id": f"check-{workspace_id[:8]}-{os.urandom(6).hex()}"}}
            next_call["arguments"].pop("name")
        elif verification.get("ready") and phase != "done":
            next_call = {"tool": "execution_publish", "arguments": {"workspace_id": workspace_id}, "requires": ["message", "request_id"]}
        if phase == "repair":
            next_call = {"tool": "execution_files", "arguments": {"workspace_id": workspace_id, "operation": "diff"}}
        if verification.get("needs_input") or phase in {"needs_review", "needs_input"}:
            next_call = None
        if busy:
            process = pending[0]
            next_call = {"tool": "execution_process_poll", "arguments": {"process_id": process["id"]}}
        elif phase == "done":
            next_call = None
        return {**self.public(record), "workspace_id": record["id"], "changes": dirty,
                "phase": phase, "verification": verification, "changes_truncated": len(dirty) >= 32000,
                "success_allowed": phase == "done",
                "input_required": phase in {"needs_review", "needs_input"},
                "completion_required": phase != "done", "deployment": "not_deployed", "next_call": next_call}

    def tree(self, workspace_id):
        """Exact prospective commit tree, including untracked files, modes and deletions.

        A private index avoids staging user changes. Git's normal ignore rules apply,
        exactly as they do to publish's git add -A. No truncated diff is hashed.
        """
        root = self.root(workspace_id)
        with tempfile.TemporaryDirectory(dir=self.store.root) as temp:
            index = Path(temp) / "index"
            shutil.copyfile(root / ".git" / "index", index)
            env = {"GIT_INDEX_FILE": str(index)}
            git(["add", "-A", "--", "."], root, env=env)
            return git(["write-tree"], root, env=env).strip()

    def plan(self, request):
        with self.store.workspace_lock(request.workspace_id):
            self.assert_idle(request.workspace_id)
            workspace = self.workspace(request.workspace_id)
            checks = [c.model_dump() for c in request.checks]
            if len({c["name"] for c in checks}) != len(checks):
                raise HTTPException(400, "Check names must be unique")
            for check in checks:
                self.target(request.workspace_id, check["cwd"], directory=True)
                self.validate_argv(check["argv"])
            skills = [SkillService().read(name) for name in dict.fromkeys(request.skills)]
            skill_refs = [{"name": s["name"], "sha256": hashlib.sha256(s["content"].encode()).hexdigest()} for s in skills]
            contract = {"checks": checks, "selected_skills": skill_refs, "max_repair_attempts": request.max_repair_attempts}
            if workspace.get("checks") and any(workspace.get(k) != v for k, v in contract.items()):
                raise HTTPException(409, "Task verification plan is immutable; start a new task for changed acceptance checks")
            self.store.update(request.workspace_id, "workspace", **contract)
            return {**self._status(request.workspace_id), "skills": skills if not workspace.get("checks") else []}

    def verification(self, workspace_id):
        workspace = self.workspace(workspace_id)
        tree = self.tree(workspace_id)
        results = workspace.get("check_results", {})
        missing = [c for c in workspace.get("checks", []) if not (
            results.get(c["name"], {}).get("status") == "succeeded" and results[c["name"]].get("tree") == tree)]
        exhausted = workspace.get("repair_attempts", 0) >= workspace.get("max_repair_attempts", 3)
        return {"ready": bool(workspace.get("checks")) and not missing and not exhausted,
                "tree": tree, "missing_checks": [c["name"] for c in missing],
                "next_check": missing[0] if missing and not exhausted else None,
                "repair_attempts": workspace.get("repair_attempts", 0), "needs_input": exhausted,
                "results": results}

    @staticmethod
    def validate_argv(argv):
        if any("\0" in arg or len(arg) > 16000 for arg in argv):
            raise HTTPException(400, "Invalid command argument")

    def finish_process(self, record, status, **details):
        """Persist worker evidence before making a completed command publishable."""
        ws = record["request"]["workspace_id"]
        with self.store.workspace_lock(ws):
            workspace = self.workspace(ws)
            check = record["request"].get("check_name")
            phase = "edit"
            if check:
                tree = self.tree(ws)
                if status == "succeeded" and tree != record.get("input_tree"):
                    status = "verification_invalidated"
                    details["error"] = "Source changed during verification; rerun against unchanged source"
                results = workspace.get("check_results", {})
                results[check] = {"process_id": record["id"], "status": status, "tree": tree}
                attempts = workspace.get("repair_attempts", 0) + (status != "succeeded")
                phase = "verify" if status == "succeeded" else "repair"
                if attempts >= workspace.get("max_repair_attempts", 3):
                    phase = "needs_input"
                self.store.update(ws, "workspace", check_results=results, repair_attempts=attempts, phase=phase)
            else:
                self.store.update(ws, "workspace", phase=phase)
            return self.store.update(record["id"], "process", status=status, **details)

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
        if request.operation == "batch":
            from .task_edit_service import apply_edits
            return apply_edits(self, request.workspace_id, request.edits)
        target = self.target(request.workspace_id, request.path)
        if request.operation == "diff":
            diff = git(["diff", "--no-ext-diff", "--no-textconv", "HEAD", "--", request.path], self.root(request.workspace_id))
            return {"diff": diff, "truncated": len(diff) >= 32000,
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
            self.store.update(request.workspace_id, "workspace", phase="edit")
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
        self.validate_argv(request.argv)
        with self.store.workspace_lock(request.workspace_id):
            existing = self.store.existing("process", request.request_id, request.model_dump())
            if existing:
                return self.poll(existing["id"])
            self.assert_idle(request.workspace_id)
            workspace = self.workspace(request.workspace_id)
            data = {}
            if request.check_name:
                check = next((c for c in workspace.get("checks", []) if c["name"] == request.check_name), None)
                if not check or any(check[k] != getattr(request, k) for k in ("argv", "cwd", "timeout_seconds")):
                    raise HTTPException(409, "Verification command must exactly match a named task-plan check")
                if workspace.get("repair_attempts", 0) >= workspace.get("max_repair_attempts", 3):
                    raise HTTPException(409, "Repair budget exhausted; inspect failures and start a new scoped task")
                data["input_tree"] = self.tree(request.workspace_id)
            record, _ = self.store.create("process", request.request_id, request.model_dump(), data=data)
            changes = {"phase": "verify" if request.check_name else "edit"}
            if request.check_name:
                results = workspace.get("check_results", {})
                results[request.check_name] = {"status": "queued", "process_id": record["id"], "tree": data["input_tree"]}
                changes["check_results"] = results
            self.store.update(request.workspace_id, "workspace", **changes)
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
        verification = self.verification(request.workspace_id)
        if not verification["ready"]:
            raise HTTPException(409, {"message": "Publish requires all planned checks to pass on the current tree",
                                      "verification": verification})
        approvals = ApprovalService()
        protected = {"main", "master", "production", "prod", "live"} | settings.protected_branches
        approvals.ensure_allowed("git.push" if request.push else "git.commit", request.approval_id,
                                 request.model_dump(exclude={"approval_id"}), force=request.push and branch.lower() in protected)
        record, created = self.store.create("publish", request.request_id, request.model_dump(), status="running")
        if not created:
            return self.public(record)
        root = self.root(request.workspace_id)
        try:
            if request.push:
                target = client.authenticated_repo if client else git(["remote", "get-url", "origin"], root).strip()
                advertised = git(["ls-remote", "--heads", "--", target, f"refs/heads/{branch}"], root).split()
                if branch == workspace.get("source_branch") and (
                    not workspace.get("remote_base_sha") or not advertised or advertised[0] != workspace["remote_base_sha"]
                ):
                    raise HTTPException(409, "Remote advanced since task preparation; preserve this task, sync/reapply in a fresh task and rerun checks")
            self.store.update(request.workspace_id, "workspace", phase="commit")
            if git(["status", "--porcelain"], root).strip():
                git(["add", "-A", "--", "."], root)
                git(["-c", f"user.name={settings.git_author_name}", "-c", f"user.email={settings.git_author_email}", "commit", "-m", request.message], root)
            sha = git(["rev-parse", "HEAD"], root).strip()
            if git(["rev-parse", "HEAD^{tree}"], root).strip() != verification["tree"]:
                raise HTTPException(409, "Commit tree differs from verified source")
            if request.push:
                self.store.update(request.workspace_id, "workspace", phase="push")
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
            self.store.update(request.workspace_id, "workspace", phase="done" if request.push else "commit",
                              published_tree=verification["tree"], published_commit=sha, published_branch=branch,
                              remote_base_sha=sha if request.push and branch == workspace.get("source_branch") else workspace.get("remote_base_sha"),
                              pushed=request.push)
            return self.public(result)
        except Exception:
            self.store.update(request.workspace_id, "workspace", phase="needs_review")
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

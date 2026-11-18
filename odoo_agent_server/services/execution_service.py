"""Task-local clones, bounded file tools and durable command/agent submission.

The API never executes user commands. The separate worker runs them in Docker,
with only the task clone mounted, no host credentials and no network.
"""
from __future__ import annotations

import hashlib
import ast
import os
import re
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore
from uuid import uuid4
from urllib.parse import urlsplit, urlunsplit

from fastapi import HTTPException

from ..config import settings, mask_repo_url
from ..execution_schemas import WorkspaceFiles, TaskCheck
from .client_resolver_service import ClientResolverService
from .execution_store import ExecutionStore
from .path_service import resolve_allowed_path
from .repo_service import RepoService
from .approval_service import ApprovalService
from .skill_service import SkillService

_INSTANCE = uuid4().hex
_IO_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="execution_git")
_IO_SLOTS = BoundedSemaphore(6)


def _submit_io(callback):
    if not _IO_SLOTS.acquire(blocking=False):
        raise HTTPException(429, "Git operation queue is full; finish/poll active work before retrying")
    def run():
        try:
            callback()
        finally:
            _IO_SLOTS.release()
    try:
        return _IO_POOL.submit(run)
    except Exception:
        _IO_SLOTS.release()
        raise


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
                                    "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                                    "-c", "maintenance.auto=false", "-c", "gc.auto=0", *argv], cwd=cwd,
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

    def open(self, request, *, background=False):
        client = ClientResolverService().resolve_client(request.client_name, project_path=request.project_path, require_complete=False)
        source_raw = request.project_path or (client.path if client else None) or settings.target_project_path
        if not source_raw:
            raise HTTPException(400, "Select a client or supply an existing project_path")
        source = resolve_allowed_path(source_raw)
        if not (source / ".git").exists():
            raise HTTPException(409, "Prepare this client's repository with client_check(validate=true) before opening a task")
        record, created = self.store.create("workspace", request.request_id, request.model_dump(), status="initializing", data={"owner_instance": _INSTANCE})
        if not created:
            return self.status(record["id"])
        if background:
            try:
                _submit_io(lambda: self._initialize(request, client, source, record))
            except Exception:
                self.store.update(record["id"], "workspace", status="failed", error="Initialization queue full; source preserved. Retry with a fresh request_id after active work finishes.")
                raise
            return self.status(record["id"])
        return self._initialize(request, client, source, record)

    def _initialize(self, request, client, source, record):
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
            if request.scope_paths:
                if any(Path(p).is_absolute() or any(part in {"..", ".git"} for part in Path(p).parts) or "\0" in p for p in request.scope_paths):
                    raise HTTPException(400, "Use repository-relative module directories for scope_paths")
                git(["sparse-checkout", "set", "--cone", "--", *request.scope_paths], path)
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
        except Exception as exc:
            detail = getattr(exc, "detail", "Workspace initialization failed; source preserved")
            self.store.update(record["id"], "workspace", status="failed", error=detail,
                              error_type=type(exc).__name__, recovery_action="correct_source_then_retry_new_request_id")
            raise

    @staticmethod
    def public(record):
        return {k: v for k, v in record.items() if k not in {"request", "source_path", "conversation", "pending_response"}}

    def status(self, workspace_id):
        record = self.store.get(workspace_id, "workspace")
        if record["status"] != "ready":
            pending = record["status"] in {"queued", "initializing"}
            if pending and record.get("owner_instance") != _INSTANCE:
                record = self.store.update(workspace_id, "workspace", status="interrupted", error="API restarted during initialization; source preserved. Inspect this task and open a fresh request_id.")
                pending = False
            return {**self.public(record), "workspace_id": workspace_id, "phase": record["status"],
                    "success_allowed": False, "completion_required": pending,
                    "state_class": "running" if pending else "blocked",
                    "next_call": {"tool": "execution_workspace_status", "arguments": {"workspace_id": workspace_id}} if pending else None}
        # Publishing owns the mutation lock; progress reads must not wait behind
        # a slow network push or attempt to inspect files mid-commit.
        if record.get("publishing") and record.get("publishing_owner") != _INSTANCE:
            record = self.store.update(workspace_id, "workspace", publishing=False, phase="needs_review",
                                       error="API restarted during publishing; inspect local and remote SHA before any new publish")
        if record.get("publishing"):
            return {**self.public(record), "workspace_id": workspace_id, "success_allowed": False,
                    "completion_required": True, "state_class": "running",
                    "next_call": {"tool": "execution_workspace_status", "arguments": {"workspace_id": workspace_id}}}
        try:
            with self.store.workspace_lock(workspace_id, blocking=False):
                return self._status(workspace_id)
        except BlockingIOError:
            record = self.workspace(workspace_id)
            return {**self.public(record), "workspace_id": workspace_id, "success_allowed": False,
                    "completion_required": True, "state_class": "running", "status": "running", "fresh_verification": False,
                    "next_call": {"tool": "execution_workspace_status", "arguments": {"workspace_id": workspace_id}}}

    def _status(self, workspace_id):
        record = self.workspace(workspace_id)
        dirty = git(["status", "--short"], self.root(workspace_id))
        pending = [p for p in self.store.pending("process") if p["request"]["workspace_id"] == workspace_id]
        busy = bool(pending)
        verification = self.verification(workspace_id) if not busy else {"ready": False, "reason": "command_pending"}
        phase = record.get("phase", "inspect")
        if phase in {"commit", "done"} and not busy:
            if record.get("published_tree") != verification.get("tree"):
                phase = "edit"
            elif not verification.get("ready"):
                phase = "verify"
        if (phase == "commit" and record.get("published_commit") and record.get("pushed") is False
                and record.get("published_tree") == verification.get("tree") and verification.get("ready")):
            phase = "done"
        if verification.get("ready") and phase in {"inspect", "edit", "verify", "repair"}:
            phase = "verify"
        next_call = {"tool": "execution_files", "arguments": {"workspace_id": workspace_id}}
        argument_request = None
        if not record.get("checks") or "required_layers" not in record:
            next_call = None
            argument_request = {"tool": "execution_task_plan", "arguments": {"workspace_id": workspace_id},
                                "required_generated_arguments": ["checks"],
                                "instruction": "Generate task-specific verification commands from source and acceptance criteria; call execution_task_plan. No user input is needed for technical test selection."}
            if record.get("checks"):
                argument_request["arguments"]["checks"] = record["checks"]
                argument_request["required_generated_arguments"] = ["checks", "required_layers"]
                argument_request["instruction"] = "Classify existing checks into verification layers and declare required_layers. Preserve their names/commands; rerun after upgrade."
        elif verification.get("next_check") and phase not in {"inspect", "repair"}:
            check = {k: v for k, v in verification["next_check"].items() if k in {"name", "argv", "cwd", "timeout_seconds"}}
            next_call = {"tool": "execution_process_start", "arguments": {"workspace_id": workspace_id, **check,
                         "check_name": check["name"], "request_id": f"check-{workspace_id[:8]}-{os.urandom(6).hex()}"}}
            next_call["arguments"].pop("name")
        elif verification.get("ready") and phase != "done":
            next_call = {"tool": "execution_publish", "arguments": {"workspace_id": workspace_id,
                         "message": record["request"]["task_description"][:2000],
                         "push": record.get("completion_target") != "commit",
                         "request_id": f"publish-{workspace_id[:8]}-{verification['tree'][:16]}-{(record.get('remote_base_sha') or '')[:8]}"}}
        if phase == "sync_required":
            next_call = {"tool": "execution_workspace_sync", "arguments": {"workspace_id": workspace_id, "request_id": f"sync-{workspace_id[:8]}-{os.urandom(6).hex()}"}}
        if phase == "repair":
            next_call = {"tool": "execution_files", "arguments": {"workspace_id": workspace_id, "operation": "diff"}}
        if verification.get("needs_input") or phase in {"needs_review", "needs_input", "configuration_required"}:
            next_call = None
        if busy:
            process = pending[0]
            next_call = {"tool": "execution_process_poll", "arguments": {"process_id": process["id"]}}
        elif phase == "done":
            next_call = None
        return {**self.public(record), "workspace_id": record["id"], "changes": dirty,
                "phase": phase, "verification": verification, "changes_truncated": len(dirty) >= 32000,
                "argument_request": argument_request,
                "success_allowed": phase == "done",
                "input_required": phase in {"needs_review", "needs_input", "configuration_required"},
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
            if not set(request.required_layers).issubset({c["layer"] for c in checks}):
                raise HTTPException(400, "Every required verification layer needs a planned check")
            if len({c["name"] for c in checks}) != len(checks):
                raise HTTPException(400, "Check names must be unique")
            for check in checks:
                self.target(request.workspace_id, check["cwd"], directory=True)
                self.validate_argv(check["argv"])
                self.validate_check(request.workspace_id, check)
            skills = [SkillService().read(name) for name in dict.fromkeys(request.skills)]
            skill_refs = [{"name": s["name"], "sha256": hashlib.sha256(s["content"].encode()).hexdigest()} for s in skills]
            previous_skills = {s["name"]: s for s in workspace.get("selected_skills", [])}
            changed_skills = {s["name"] for s in skill_refs if previous_skills.get(s["name"]) != s}
            skill_refs = list({**previous_skills, **{s["name"]: s for s in skill_refs}}.values())
            contract = {"checks": checks, "selected_skills": skill_refs, "max_repair_attempts": request.max_repair_attempts,
                        "required_layers": sorted(set(request.required_layers))}
            old = {c["name"]: TaskCheck.model_validate(c).model_dump() for c in workspace.get("checks", [])}
            new = {c["name"]: c for c in checks}
            legacy_plan = "required_layers" not in workspace
            def strengthens(name, check):
                candidate = new.get(name, {})
                semantic_fields = ("name", "argv", "cwd") if legacy_plan else ("name", "argv", "cwd", "layer")
                return (all(candidate.get(k) == check[k] for k in semantic_fields)
                        and set(check["output_dirs"]).issubset(candidate.get("output_dirs", []))
                        and candidate.get("timeout_seconds", 0) >= check["timeout_seconds"])
            if old and (any(not strengthens(name, check) for name, check in old.items())
                        or (not legacy_plan and not set(workspace["required_layers"]).issubset(contract["required_layers"]))
                        or workspace.get("max_repair_attempts", 3) != request.max_repair_attempts):
                raise HTTPException(409, "Plans may add checks/layers, but cannot remove or replace acceptance checks or reset the repair budget")
            results = workspace.get("check_results", {})
            for name, check in old.items():
                if new[name] != check:
                    results.pop(name, None)
            contract["check_results"] = results
            self.store.update(request.workspace_id, "workspace", **contract)
            return {**self._status(request.workspace_id), "skills": [s for s in skills if s["name"] in changed_skills]}

    def verification(self, workspace_id):
        workspace = self.workspace(workspace_id)
        tree = self.tree(workspace_id)
        results = workspace.get("check_results", {})
        runtime_id = self.runtime_id()
        missing = [c for c in workspace.get("checks", []) if not (
            results.get(c["name"], {}).get("status") == "succeeded" and results[c["name"]].get("tree") == tree
            and results[c["name"]].get("runtime_id") == runtime_id)]
        failures = workspace.get("failure_streaks", {})
        exhausted = any(value.get("count", 0) >= workspace.get("max_repair_attempts", 3) and value.get("tree") == tree for value in failures.values())
        passed_layers = sorted({c.get("layer", "unspecified") for c in workspace.get("checks", []) if c not in missing})
        required = workspace.get("required_layers", ["unit"])
        missing_layers = sorted(set(required) - set(passed_layers))
        return {"ready": bool(workspace.get("checks")) and "required_layers" in workspace and not missing and not missing_layers and not exhausted,
                "tree": tree, "missing_checks": [c["name"] for c in missing],
                "scope": "declared_checks_only", "passed_layers": passed_layers, "missing_layers": missing_layers,
                "orm_verified": "orm" in passed_layers, "deployment_verified": False,
                "next_check": missing[0] if missing and not exhausted else None,
                "repair_attempts": workspace.get("repair_attempts", 0), "needs_input": exhausted,
                "results": results}

    def runtime_id(self):
        capabilities = self.store._read(self.store.root / "capabilities.json") or {}
        return capabilities.get("image_id", settings.execution_image) if capabilities.get("image", settings.execution_image) == settings.execution_image else settings.execution_image

    def validate_check(self, workspace_id, check):
        argv = check["argv"]
        if Path(argv[0]).name in {"true", "echo"}:
            raise HTTPException(400, "A no-op command is not an acceptance check")
        if "-c" in argv and Path(argv[0]).name.startswith("python"):
            code = argv[argv.index("-c") + 1] if argv.index("-c") + 1 < len(argv) else ""
            try:
                nodes = ast.parse(code).body
                if nodes and all(isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
                                 and isinstance(n.value.func, ast.Name) and n.value.func.id == "print" for n in nodes):
                    raise HTTPException(400, "Printing output alone is not verification")
            except SyntaxError:
                pass  # A failing command is still useful evidence; the worker records it.
        capabilities = self.store._read(self.store.root / "capabilities.json") or {}
        if capabilities.get("image") != settings.execution_image:
            capabilities = {}
        if check["layer"] == "orm" and not capabilities.get("odoo"):
            raise HTTPException(409, {"error_code": "runtime_unavailable", "required_runtime": "odoo", "message": "Configure an Odoo test runtime/image before selecting ORM checks; no functional verification was performed."})
        if Path(argv[0]).name == "npm" and not capabilities.get("npm"):
            raise HTTPException(409, {"error_code": "runtime_unavailable", "required_runtime": "npm"})
        for directory in check["output_dirs"]:
            target = self.target(workspace_id, directory)
            if (not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", directory) or directory == "."
                    or (target.exists() and not target.is_dir())
                    or git(["ls-files", "--cached", "--others", "--exclude-standard", "--", directory], self.root(workspace_id)).strip()):
                raise HTTPException(400, "Writable outputs must be untracked directories outside source files")

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
                results[check] = {"process_id": record["id"], "status": status, "tree": tree,
                                  "runtime_id": record.get("runtime_id")}
                attempts = workspace.get("repair_attempts", 0) + (status != "succeeded")
                streaks = workspace.get("failure_streaks", {})
                previous = streaks.get(check, {})
                count = 0 if status in {"succeeded", "runtime_unavailable"} else (previous.get("count", 0) + 1 if previous.get("tree") == tree else 1)
                streaks[check] = {"tree": tree, "count": count}
                phase = "verify" if status == "succeeded" else "repair"
                if count >= workspace.get("max_repair_attempts", 3):
                    phase = "needs_input"
                if status == "runtime_unavailable":
                    phase = "configuration_required"
                self.store.update(ws, "workspace", check_results=results, repair_attempts=attempts, failure_streaks=streaks, phase=phase,
                                  runtime_error=details.get("error") if status == "runtime_unavailable" else None)
            else:
                self.store.update(ws, "workspace", phase=phase)
            return self.store.update(record["id"], "process", status=status, **details)

    def files(self, request: WorkspaceFiles):
        # File reads also wait for commands: a running container could otherwise
        # swap a path into a symlink between containment checking and opening.
        if self.workspace(request.workspace_id).get("publishing"):
            raise HTTPException(409, "Publishing is active; poll its operation before file access")
        with self.store.workspace_lock(request.workspace_id):
            self.assert_idle(request.workspace_id)
            return self._files(request)

    def assert_idle(self, workspace_id):
        if self.workspace(workspace_id).get("publishing"):
            raise HTTPException(409, "Publishing is active; poll its operation before editing or executing commands")
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
        if self.workspace(request.workspace_id).get("publishing"):
            raise HTTPException(409, "Publishing is active; poll its operation before submitting commands")
        self.target(request.workspace_id, request.cwd, directory=True)
        self.validate_argv(request.argv)
        with self.store.workspace_lock(request.workspace_id):
            existing = self.store.existing("process", request.request_id, request.model_dump())
            if existing:
                return self.poll(existing["id"])
            self.assert_idle(request.workspace_id)
            workspace = self.workspace(request.workspace_id)
            data = {"runtime_id": self.runtime_id()}
            if request.check_name:
                check = next((c for c in workspace.get("checks", []) if c["name"] == request.check_name), None)
                if not check or any(check[k] != getattr(request, k) for k in ("argv", "cwd", "timeout_seconds")):
                    raise HTTPException(409, "Verification command must exactly match a named task-plan check")
                self.validate_check(request.workspace_id, TaskCheck.model_validate(check).model_dump())
                verification = self.verification(request.workspace_id)
                if verification["needs_input"]:
                    raise HTTPException(409, "Repair budget exhausted; inspect failures and start a new scoped task")
                data["input_tree"] = verification["tree"]
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

    def sync_background(self, request):
        self.workspace(request.workspace_id)
        record, created = self.store.create("operation", f"sync:{request.request_id}", request.model_dump(), data={"owner_instance": _INSTANCE, "operation_type": "sync"})
        if created:
            def run():
                self.store.update(record["id"], "operation", status="running")
                try:
                    result = self.sync(request)
                    self.store.update(record["id"], "operation", status="succeeded", result=result)
                except HTTPException as exc:
                    self.store.update(record["id"], "operation", status="needs_review", error=exc.detail)
                except Exception:
                    self.store.update(record["id"], "operation", status="needs_review", error="Sync interrupted; source and snapshot preserved")
            try:
                _submit_io(run)
            except Exception:
                self.store.update(record["id"], "operation", status="needs_review", error="Git queue full; retry with a fresh request_id")
                raise
        return self.operation_status(record["id"])

    def sync(self, request):
        """Merge in a disposable clone. Promote only a complete, conflict-free tree."""
        wid = request.workspace_id
        with self.store.workspace_lock(wid):
            self.assert_idle(wid)
            ws = self.workspace(wid)
            root = self.root(wid)
            client = ClientResolverService().resolve_client(ws.get("client_name"), project_path=ws["source_path"], require_complete=False)
            target = client.authenticated_repo if client else git(["remote", "get-url", "origin"], root).strip()
            branch = ws["source_branch"]
            ref = "refs/odoo-mcp/sync-remote"
            git(["-c", "core.logAllRefUpdates=false", "fetch", "--no-tags", "--no-write-fetch-head", "--", target, f"+refs/heads/{branch}:{ref}"], root, timeout=120)
            latest = git(["rev-parse", ref], root).strip()
            if request.resolutions and request.expected_remote_sha != latest:
                raise HTTPException(409, {"error_code": "resolution_stale", "actual_head": latest, "message": "Remote moved; inspect conflicts again before resolving"})
            if latest == ws.get("remote_base_sha"):
                return {"status": "up_to_date", "workspace_id": wid, "remote_sha": latest}
            if ws.get("sync_attempts", 0) >= 2:
                raise HTTPException(409, "Branch keeps moving; two sync attempts reached. Coordinate concurrent writers before continuing")
            old_head = git(["rev-parse", "HEAD"], root).strip()
            tree = self.tree(wid)
            identity = {"GIT_AUTHOR_NAME": settings.git_author_name, "GIT_AUTHOR_EMAIL": settings.git_author_email,
                        "GIT_COMMITTER_NAME": settings.git_author_name, "GIT_COMMITTER_EMAIL": settings.git_author_email}
            snapshot = git(["commit-tree", tree, "-p", old_head, "-m", "MCP preserved pre-sync work"], root, env=identity).strip()
            git(["update-ref", f"refs/odoo-mcp/snapshots/{snapshot}", snapshot], root)
            with tempfile.TemporaryDirectory(dir=self.store.root) as directory:
                candidate = Path(directory) / "repo"
                git(["clone", "--local", "--no-checkout", "--", str(root), str(candidate)], root)
                git(["checkout", "--detach", snapshot], candidate)
                try:
                    git(["merge", "--no-commit", "--no-ff", latest], candidate, env=identity)
                except HTTPException:
                    raw = git(["diff", "--name-only", "--diff-filter=U", "-z"], candidate)
                    conflicts = [path for path in raw.split("\0") if path]
                    if not conflicts or len(raw) >= 32000 or len(conflicts) > 25:
                        raise
                    if set(request.resolutions) - set(conflicts):
                        raise HTTPException(400, "Supply resolutions only for current conflicting files")
                    if set(request.resolutions) != set(conflicts):
                        details = [{"path": path, "diff": git(["diff", "--", path], candidate)[:4000]} for path in conflicts]
                        return {"status": "merge_conflict", "workspace_id": wid, "remote_sha": latest, "snapshot_sha": snapshot,
                                "conflicts": details, "source_preserved": True,
                                "instruction": "Resolve both intents; call execution_workspace_sync with a new request_id, expected_remote_sha and resolutions for every conflicting path. Ask the user only for ambiguous business intent."}
                    for path, content in request.resolutions.items():
                        dest = candidate / path
                        if Path(path).is_absolute() or ".git" in Path(path).parts or not dest.parent.resolve().is_relative_to(candidate.resolve()):
                            raise HTTPException(400, "Unsafe resolution path")
                        if content is not None and (len(content.encode()) > 2_000_000 or re.search(r"(?m)^(<{7}|={7}|>{7})( |$)", content)):
                            raise HTTPException(400, "Resolution is too large or still contains conflict markers")
                        mode = dest.lstat().st_mode & 0o777 if dest.exists() and not dest.is_symlink() else 0o660
                        dest.unlink(missing_ok=True)
                        if content is not None:
                            dest.write_text(content)
                            dest.chmod(mode)
                        git(["add", "-A", "--", path], candidate)
                merged = git(["write-tree"], candidate).strip()
                # Local clone objects are hard-linked, but newly created trees are
                # not: import the merged tree through a temporary commit.
                merged_commit = git(["commit-tree", merged, "-p", snapshot, "-m", "MCP merged work"], candidate, env=identity).strip()
                git(["fetch", "--no-tags", "--no-write-fetch-head", "--", str(candidate), merged_commit], root)
                try:
                    git(["reset", "--soft", latest], root)
                    git(["read-tree", "--reset", "-u", merged], root)
                except Exception:
                    git(["reset", "--soft", old_head], root)
                    git(["read-tree", "--reset", "-u", tree], root)
                    raise
            self.store.update(wid, "workspace", base_sha=latest, remote_base_sha=latest, check_results={}, failure_streaks={},
                              phase="verify", error=None, published_tree=None, published_commit=None, pushed=False,
                              sync_attempts=ws.get("sync_attempts", 0) + 1, sync_snapshot=snapshot)
            return {"status": "synced", "workspace_id": wid, "remote_sha": latest, "snapshot_sha": snapshot,
                    "verification_required": True, "next_call": {"tool": "execution_workspace_status", "arguments": {"workspace_id": wid}}}

    def publish_background(self, request):
        self.workspace(request.workspace_id)
        key = f"publish:{request.request_id}:{request.approval_id or 'pending'}"
        record, created = self.store.create("operation", key, request.model_dump(), data={"owner_instance": _INSTANCE})
        if created:
            def run():
                self.store.update(record["id"], "operation", status="running")
                try:
                    result = self.publish(request)
                    self.store.update(record["id"], "operation", status="succeeded" if result.get("status") == "succeeded" else "needs_review", result=result)
                except HTTPException as exc:
                    if isinstance(exc.detail, dict) and exc.detail.get("approval_required"):
                        self.store.update(record["id"], "operation", status="approval_required", approval=exc.detail)
                    elif isinstance(exc.detail, dict) and exc.detail.get("error_code") == "remote_advanced":
                        self.store.update(record["id"], "operation", status="sync_required", error=exc.detail)
                    elif isinstance(exc.detail, dict) and exc.detail.get("verification"):
                        self.store.update(record["id"], "operation", status="verification_required", verification=exc.detail["verification"])
                    else:
                        self.store.update(record["id"], "operation", status="needs_review", error=mask_repo_url(str(exc.detail))[:1500])
                except Exception as exc:
                    self.store.update(record["id"], "operation", status="needs_review", error=f"Publish interrupted ({type(exc).__name__}); inspect SHA before retry")
            try:
                _submit_io(run)
            except Exception:
                self.store.update(record["id"], "operation", status="needs_review", error="Git operation queue full; retry later with a fresh request_id")
                raise
        return self.operation_status(record["id"])

    def operation_status(self, operation_id):
        record = self.store.get(operation_id, "operation")
        request = record["request"]
        if record["status"] in {"queued", "running"} and record.get("owner_instance") != _INSTANCE:
            record = self.store.update(operation_id, "operation", status="needs_review", error="API restarted; inspect task/local/remote SHA before creating a new request")
        active = record["status"] in {"queued", "running"}
        if record.get("operation_type") == "sync" and not active:
            result = record.get("result") or {}
            out = {"status": record["status"], "operation_id": operation_id, "result": result, "error": record.get("error"),
                   "success_allowed": False, "completion_required": True,
                   "next_call": {"tool": "execution_workspace_status", "arguments": {"workspace_id": request["workspace_id"]}}}
            if result.get("status") == "merge_conflict":
                out.update(status="merge_conflict", next_call=None, input_required=False,
                           argument_request={"tool": "execution_workspace_sync", "arguments": {"workspace_id": request["workspace_id"],
                               "request_id": "resolve-" + os.urandom(8).hex(), "expected_remote_sha": result["remote_sha"]},
                               "required_generated_arguments": ["resolutions"], "instruction": result["instruction"]})
            elif record["status"] != "succeeded":
                out.update(next_call=None, input_required=True)
            return out
        if record["status"] == "verification_required":
            return {"status": "verification_required", "operation_id": operation_id, "verification": record["verification"],
                    "success_allowed": False, "completion_required": True,
                    "next_call": {"tool": "execution_workspace_status", "arguments": {"workspace_id": request["workspace_id"]}}}
        if record["status"] == "approval_required":
            approval = record["approval"]
            return {**approval, "status": "approval_required", "operation_id": operation_id,
                    "success_allowed": False, "completion_required": True, "input_required": True,
                    "input_request": {"kind": "approval", "approval_id": approval["approval_id"],
                                      "requires_explicit_user_consent": True, "prompt": approval.get("message"),
                                      "resume_call": {"tool": "execution_publish", "arguments": {**request, "approval_id": approval["approval_id"]}}},
                    "next_call": None}
        if record["status"] == "sync_required":
            return {"status": "sync_required", "operation_id": operation_id, "error": record.get("error"),
                    "success_allowed": False, "completion_required": True, "input_required": False,
                    "next_call": {"tool": "execution_workspace_status", "arguments": {"workspace_id": request["workspace_id"]}}}
        result = record.get("result") or {}
        succeeded = bool(record["status"] == "succeeded" and result.get("commit")
                         and (not request.get("push", True) or result.get("pushed")))
        return {**result, "status": record["status"], "operation_id": operation_id,
                "workspace_id": request["workspace_id"], "error": record.get("error"),
                "success_allowed": succeeded,
                "completion_required": active, "state_class": "running" if active else "succeeded" if succeeded else "blocked",
                "next_call": {"tool": "execution_status", "arguments": {"operation_id": operation_id}} if active else None}

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
        self.store.update(request.workspace_id, "workspace", publishing=True, publishing_owner=_INSTANCE, phase="preparing_publish")
        try:
            if request.push:
                target = client.authenticated_repo if client else git(["remote", "get-url", "origin"], root).strip()
                advertised = git(["ls-remote", "--heads", "--", target, f"refs/heads/{branch}"], root).split()
                if branch == workspace.get("source_branch") and (
                    not workspace.get("remote_base_sha") or not advertised or advertised[0] != workspace["remote_base_sha"]
                ):
                    raise HTTPException(409, {"error_code": "remote_advanced",
                        "message": "Remote advanced since task preparation; call execution_workspace_sync for this task, then rerun checks",
                        "expected_head": workspace.get("remote_base_sha"), "actual_head": advertised[0] if advertised else None})
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
                try:
                    git(["push", "--", target, f"HEAD:refs/heads/{branch}"], root, timeout=120)
                except HTTPException as exc:
                    if any(word in str(exc.detail) for word in ("non-fast-forward", "fetch first")):
                        raise HTTPException(409, {"error_code": "remote_advanced", "message": "Remote moved during push; sync this workspace and rerun verification"}) from exc
                    raise
                received = git(["ls-remote", "--heads", "--", target, f"refs/heads/{branch}"], root).split()
                if not received or received[0] != sha:
                    raise HTTPException(409, "Push returned without confirming the expected remote SHA")
            approvals.mark_consumed(request.approval_id)
            result = self.store.update(record["id"], "publish", status="succeeded", commit=sha, branch=branch,
                                       pushed=request.push, deployment="not_deployed",
                                       next_action=("Push complete. Use the deployment workflow with this SHA only if deployment was requested." if request.push else "Local commit complete. Stop; no push or deployment was requested."))
            self.store.update(request.workspace_id, "workspace", phase="done", completion_target="push" if request.push else "commit",
                              published_tree=verification["tree"], published_commit=sha, published_branch=branch,
                              remote_base_sha=sha if request.push and branch == workspace.get("source_branch") else workspace.get("remote_base_sha"),
                              pushed=request.push, publishing=False, sync_attempts=0)
            return self.public(result)
        except Exception as exc:
            detail = getattr(exc, "detail", type(exc).__name__)
            sync_required = isinstance(detail, dict) and detail.get("error_code") == "remote_advanced"
            error = detail if isinstance(detail, dict) else mask_repo_url(str(detail))[:2000]
            self.store.update(request.workspace_id, "workspace", phase="sync_required" if sync_required else "needs_review", publishing=False,
                              error=error, error_type=type(exc).__name__, recovery_action="Inspect diff; open a fresh isolated task, reapply the preserved patch against current source, rerun checks, then publish. No user decision is required for ordinary synchronization.")
            approvals.mark_failed(request.approval_id)
            self.store.update(record["id"], "publish", status="sync_required" if sync_required else "needs_review", error=error, error_type=type(exc).__name__)
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

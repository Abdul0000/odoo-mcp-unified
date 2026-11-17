"""Validate exact execution proof before adopting an already-pushed commit."""

from pathlib import Path
from fastapi import HTTPException
from .client_resolver_service import ClientResolverService
from .execution_service import ExecutionService, git


def validate_published_workspace(payload):
    if not payload.execution_workspace_id:
        return None
    svc = ExecutionService()
    wid = payload.execution_workspace_id
    record = svc.workspace(wid)
    client = ClientResolverService().resolve_client(payload.client_name, require_complete=False)
    if (
        not client
        or record.get("client_name") != client.name
        or Path(record["source_path"]).resolve() != Path(client.path).resolve()
        or Path(payload.project_path).resolve() != Path(client.path).resolve()
        or record.get("published_branch") != client.branch
        or (payload.branch and payload.branch != client.branch)
    ):
        raise HTTPException(409, "Published workspace does not match deployment client, checkout and branch")
    status = svc.status(wid)
    if not (status.get("success_allowed") and status.get("verification", {}).get("ready") and record.get("pushed") and record.get("published_commit")):
        raise HTTPException(409, "Deployment requires current exact-tree checks and a verified published workspace")
    if git(["status", "--porcelain"], client.path).strip():
        raise HTTPException(409, "Client checkout has local work; it was preserved")
    sha = git(["rev-parse", "HEAD"], client.path).strip()
    if sha != record["published_commit"]:
        raise HTTPException(409, "Client checkout differs from the verified task commit")
    remote = git(["ls-remote", "--heads", "--", client.authenticated_repo, "refs/heads/" + client.branch], client.path, timeout=20).split()
    if not remote or remote[0] != sha:
        raise HTTPException(409, "Remote advanced or cannot confirm the task commit; deployment blocked")
    root = payload.module_root_rel or payload.module_name
    if root.startswith("/") or ".." in Path(root).parts or not (Path(client.path) / root).resolve().is_relative_to(Path(client.path).resolve()):
        raise HTTPException(400, "Module root must remain inside client checkout")
    touched = git(["diff-tree", "--root", "--no-commit-id", "--name-only", "-r", sha, "--", root], client.path).splitlines()
    if not touched:
        raise HTTPException(409, "Published commit did not change the selected module")
    return {"sha": sha, "workspace_id": wid, "paths": touched}

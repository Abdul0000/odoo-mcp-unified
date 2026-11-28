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
    task_id = getattr(payload, "review_task_id", None)
    review_sha = None
    if task_id:
        from .task_review_service import review_branch, reviewed_deploy
        from .task_service import TaskService

        ledger = TaskService()
        task = ledger.read(task_id)
        review = task.get("review") or {}
        if (not client or not ledger.current(task) or not reviewed_deploy(task)
                or task["client_name"] != client.name or task.get("module_name") != payload.module_name
                or task["refs"].get("workspace_id") != wid or review.get("state") != "merged"
                or review.get("head_sha") != record.get("published_commit") or not review.get("merged_sha")):
            raise HTTPException(409, "Reviewed deployment needs the linked merged task evidence")
        review_sha = review["merged_sha"]
        branch = review_branch(task_id)
    else:
        branch = client.branch if client else None
    if (
        not client
        or record.get("client_name") != client.name
        or Path(record["source_path"]).resolve() != Path(client.path).resolve()
        or Path(payload.project_path).resolve() != Path(client.path).resolve()
        or record.get("published_branch") != branch
        or (payload.branch and payload.branch != client.branch)
    ):
        raise HTTPException(409, "Published workspace does not match deployment client, checkout and branch")
    status = svc.status(wid)
    if not (status.get("success_allowed") and status.get("verification", {}).get("ready") and record.get("pushed") and record.get("published_commit")):
        raise HTTPException(409, "Deployment requires current exact-tree checks and a verified published workspace")
    if git(["status", "--porcelain"], client.path).strip():
        raise HTTPException(409, "Client checkout has local work; it was preserved")
    sha = git(["rev-parse", "HEAD"], client.path).strip()
    if sha != (review_sha or record["published_commit"]):
        raise HTTPException(409, "Client checkout differs from the verified task commit")
    remote = git(["ls-remote", "--heads", "--", client.authenticated_repo, "refs/heads/" + client.branch], client.path, timeout=20).split()
    if not remote or remote[0] != sha:
        raise HTTPException(409, "Remote advanced or cannot confirm the task commit; deployment blocked")
    base_sha = None
    if review_sha:
        base_sha = record.get("remote_base_sha")
        if (not base_sha or git(["rev-parse", sha + "^1"], client.path).strip() != base_sha
                or git(["rev-parse", sha + "^{tree}"], client.path).strip() != record.get("published_tree")
                or status["verification"].get("tree") != record.get("published_tree")):
            raise HTTPException(409, "Merged tree or base differs from the checked review; reverify on the new base")
    root = payload.module_root_rel or payload.module_name
    if root.startswith("/") or ".." in Path(root).parts or not (Path(client.path) / root).resolve().is_relative_to(Path(client.path).resolve()):
        raise HTTPException(400, "Module root must remain inside client checkout")
    module = (Path(client.path) / root).resolve()
    if (Path(root).name != payload.module_name or module.name != payload.module_name
            or not any((module / name).is_file() for name in ("__manifest__.py", "__openerp__.py"))):
        raise HTTPException(409, "Deployment source root must be the manifest root of the selected technical module")
    command = (["diff", "--name-only", base_sha, sha] if base_sha
               else ["diff-tree", "--root", "--no-commit-id", "--name-only", "-r", sha])
    touched = git([*command, "--", root], client.path).splitlines()
    if not touched:
        raise HTTPException(409, "Published commit did not change the selected module")
    return {"sha": sha, "workspace_id": wid, "paths": touched, "base_sha": base_sha,
            "passed_layers": status["verification"].get("passed_layers", [])}

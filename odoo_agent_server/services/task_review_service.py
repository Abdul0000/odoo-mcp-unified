"""Open or reconcile a review of an exact task branch without merging it."""

import re
from urllib.parse import urlencode

from fastapi import HTTPException

from .github_commit_service import GitHubError, GitHubRepo, parse_repo
from .gitlab_commit_service import GitLabRepo, is_gitlab_repo_url, parse_gitlab_repo

SHA = re.compile(r"[a-f0-9]{40}")


def review_branch(task_id):
    return "odoo-mcp/task-" + task_id


def reviewed_deploy(task):
    return task["target"] == "deploy" and bool(task.get("sdlc"))


def review_api(client):
    if not client.token:
        raise HTTPException(409, "The selected client needs repository review API credentials")
    if is_gitlab_repo_url(client.repo):
        return "gitlab", GitLabRepo(*parse_gitlab_repo(client.repo), client.token)
    try:
        return "github", GitHubRepo(*parse_repo(client.repo), client.token, max_attempts=1)
    except ValueError:
        raise HTTPException(409, "Reviewed tasks require a configured GitHub or GitLab repository") from None


def reconcile_review(client, task, head, *, create=False):
    """Find the one task review or open it after the task push is verified."""
    if not isinstance(head, str) or not SHA.fullmatch(head):
        raise HTTPException(409, "Review requires a verified source commit")
    provider, api = review_api(client)
    source = review_branch(task["task_id"])
    marker = f"<!-- odoo-mcp-task:{task['task_id']} -->"
    title = (task["description"].strip().splitlines() or ["Odoo MCP task"])[0][:100]
    links = task["sdlc"]
    body = (f"{marker}\n\nIntent: {links['intent']}\nDesign: {links['design']}\n"
            f"MCP task: {task['task_id']}\n\nVerified source commit: {head}")
    if links.get("incident_id"):
        body += f"\nReadiness incident: {links['incident_id']}"
    try:
        if provider == "github":
            query = urlencode({"head": f"{api.owner}:{source}", "base": client.branch, "state": "all", "per_page": 30})
            matches = api.request("GET", "/pulls?" + query)
            if not matches and create:
                if api.head(source)["commit"] != head:
                    raise HTTPException(409, "Review branch moved after verification")
                row = api.request("POST", "/pulls", {"head": source, "base": client.branch, "title": title, "body": body})
            else:
                row = one_review(matches, "body", marker)
            if row is None:
                return None
            row = api.request("GET", f"/pulls/{int(row['number'])}")
            repo = f"{api.owner}/{api.repo}".casefold()
            if (row["head"]["ref"] != source or row["base"]["ref"] != client.branch
                    or any((row[side]["repo"] or {}).get("full_name", "").casefold() != repo for side in ("head", "base"))):
                raise HTTPException(409, "Review repository or branches differ from the task")
            actual_head, merged, merge_sha = row["head"]["sha"], row["merged"], row.get("merge_commit_sha")
            closed = row["state"] == "closed" and not merged
            if not linked_body(row.get("body"), task, marker) or actual_head != head:
                raise HTTPException(409, "Review no longer identifies the verified task commit")
            if merged:
                verify_github_approval(api, row, head)
            link = f"https://github.com/{api.owner}/{api.repo}/pull/{int(row['number'])}"
        else:
            query = urlencode({"source_branch": source, "target_branch": client.branch, "scope": "all", "per_page": 30})
            matches = api.request("GET", "/merge_requests?" + query)
            if not matches and create:
                if api.head(source)["commit"] != head:
                    raise HTTPException(409, "Review branch moved after verification")
                row = api.request("POST", "/merge_requests", {"source_branch": source, "target_branch": client.branch,
                                  "title": title, "description": body, "remove_source_branch": False})
            else:
                row = one_review(matches, "description", marker)
            if row is None:
                return None
            iid = int(row["iid"])
            row = api.request("GET", f"/merge_requests/{iid}")
            if (row["source_branch"] != source or row["target_branch"] != client.branch
                    or row.get("source_project_id") != row.get("target_project_id")
                    or not row.get("target_project_id")):
                raise HTTPException(409, "Review repository or branches differ from the task")
            actual_head, merged = row["sha"], row["state"] == "merged"
            merge_sha = row.get("merge_commit_sha") or row.get("squash_commit_sha") or head
            closed = row["state"] in {"closed", "locked"}
            if not linked_body(row.get("description"), task, marker) or actual_head != head:
                raise HTTPException(409, "Review no longer identifies the verified task commit")
            if merged:
                approvals = api.request("GET", f"/merge_requests/{iid}/approvals")
                if not any(item["user"]["id"] != row["author"]["id"] and item["user"].get("bot") is not True
                           for item in approvals.get("approved_by", [])):
                    raise HTTPException(409, "Merged request lacks independent approval")
            link = f"{api.base_url}/{api.project_path}/-/merge_requests/{iid}"
        if merged and (not isinstance(merge_sha, str) or not SHA.fullmatch(merge_sha)):
            raise HTTPException(409, "Provider did not confirm the merged commit")
        return {"state": "merged" if merged else "closed" if closed else "pending",
                "url": link, "head_sha": head, "merged_sha": merge_sha if merged else None}
    except GitHubError as exc:
        raise HTTPException(503 if exc.retryable else 409, f"Repository review API returned HTTP {exc.status}") from None
    except (KeyError, TypeError, ValueError, AttributeError):
        raise HTTPException(502, "Repository review API returned incomplete evidence") from None


def linked_body(body, task, marker):
    lines = set((body or "").splitlines())
    return {marker, f"Intent: {task['sdlc']['intent']}", f"Design: {task['sdlc']['design']}",
            f"MCP task: {task['task_id']}"} <= lines


def one_review(rows, field, marker):
    if not rows:
        return None
    if len(rows) != 1 or marker not in (rows[0].get(field) or ""):
        raise HTTPException(409, "Review branch has an ambiguous or unrelated existing review")
    return rows[0]


def verify_github_approval(api, row, head):
    decisions = {}
    for page in range(1, 6):
        reviews = api.request("GET", f"/pulls/{int(row['number'])}/reviews?per_page=100&page={page}")
        for review in reviews:
            user = review.get("user") or {}
            if (user.get("type") == "User" and user["id"] != row["user"]["id"]
                    and review.get("author_association") in {"OWNER", "MEMBER", "COLLABORATOR"}
                    and review["state"] in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}):
                decisions[user["id"]] = review
        if len(reviews) < 100:
            break
    else:
        raise HTTPException(409, "Review history exceeds the safe verification limit")
    states = {review["state"] for review in decisions.values() if review.get("commit_id") == head}
    if "APPROVED" not in states or "CHANGES_REQUESTED" in states:
        raise HTTPException(409, "Merged pull request lacks independent approval of the exact head")

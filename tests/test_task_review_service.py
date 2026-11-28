from types import SimpleNamespace

from fastapi import HTTPException
import pytest

from odoo_agent_server.services import task_review_service as service

TASK_ID = "1" * 32
HEAD = "2" * 40
MERGE = "3" * 40


@pytest.mark.parametrize("provider", ["github", "gitlab"])
def test_open_and_reconcile_provider_review_once(monkeypatch, provider):
    branch = service.review_branch(TASK_ID)
    client = SimpleNamespace(branch="staging")
    task = {"task_id": TASK_ID, "description": "Improve staging totals",
            "sdlc": {"intent": "https://example.test/issues/1", "design": "https://example.test/docs/1"}}
    state = {"row": None, "writes": 0, "approve": False, "source": HEAD}

    def request(method, route, payload=None):
        if method == "POST":
            state["writes"] += 1
            assert (payload.get("head") or payload.get("source_branch")) == branch
            note = payload.get("body") or payload["description"]
            if provider == "github":
                state["row"] = {"number": 1, "body": note, "state": "open", "merged": False, "user": {"id": 1},
                                "head": {"ref": branch, "sha": HEAD, "repo": {"full_name": "org/repo"}},
                                "base": {"ref": "staging", "repo": {"full_name": "org/repo"}}}
            else:
                state["row"] = {"iid": 1, "description": note, "state": "opened", "sha": HEAD,
                                "source_branch": branch, "target_branch": "staging", "author": {"id": 1},
                                "source_project_id": 2, "target_project_id": 2}
            return state["row"]
        if route.startswith(("/pulls?", "/merge_requests?")):
            return [state["row"]] if state["row"] else []
        if "/reviews?" in route:
            return [{"user": {"type": "User", "id": 2}, "commit_id": HEAD, "state": "APPROVED",
                     "author_association": "MEMBER"}] if state["approve"] else []
        if route.endswith("/approvals"):
            return {"approved_by": [{"user": {"id": 2}}] if state["approve"] else []}
        return state["row"]

    api = SimpleNamespace(owner="org", repo="repo", base_url="https://gitlab.example.test", project_path="org/repo",
                          request=request, head=lambda branch: {"commit": state["source"]})
    monkeypatch.setattr(service, "review_api", lambda client: (provider, api))
    assert service.reconcile_review(client, task, HEAD) is None
    state["source"] = MERGE
    with pytest.raises(HTTPException, match="moved after verification"):
        service.reconcile_review(client, task, HEAD, create=True)
    state["source"] = HEAD
    assert service.reconcile_review(client, task, HEAD, create=True)["state"] == "pending"
    assert service.reconcile_review(client, task, HEAD, create=True)["state"] == "pending"
    assert state["writes"] == 1
    state["row"].update(state="merged", merged=True, merge_commit_sha=MERGE)
    with pytest.raises(HTTPException, match="independent approval"):
        service.reconcile_review(client, task, HEAD)
    state["approve"] = True
    assert service.reconcile_review(client, task, HEAD)["merged_sha"] == MERGE
    field = "body" if provider == "github" else "description"
    state["row"][field] = state["row"][field].replace("Design: ", "Old design: ")
    with pytest.raises(HTTPException, match="no longer identifies"):
        service.reconcile_review(client, task, HEAD)

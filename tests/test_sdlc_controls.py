import json
from types import SimpleNamespace

import pytest

from deploy.readiness_monitor import advance, record
from deploy.release_gate import WORKFLOWS, verified_release
from odoo_agent_server.config import settings
from odoo_agent_server.services.sdlc_incidents import readiness_incidents
from tests.evals.live import CASES, run, score


@pytest.fixture
def release_evidence():
    commit = "a" * 40
    head = "b" * 40
    pr = {"merged": True, "merge_commit_sha": commit, "base": {"ref": "master"}, "head": {"sha": head},
          "user": {"login": "author"},
          "body": "Intent: https://example.test/issues/1\nDesign: https://example.test/docs/1"}
    reviews = [{"user": {"login": "reviewer", "type": "User"}, "state": "APPROVED",
                "author_association": "COLLABORATOR", "commit_id": head}]
    runs = {name: [{"id": 7, "head_sha": commit, "head_branch": "master", "status": "completed",
                    "event": "push", "conclusion": "success"}] for name in WORKFLOWS}

    def get(route):
        if route == "pulls/1":
            return pr
        if route.startswith("pulls/1/reviews?"):
            return reviews
        return {"workflow_runs": runs[route.split("/")[2]]}

    return commit, pr, reviews, runs, get


def test_release_binds_review_and_ci_to_commit(release_evidence):
    commit, _, _, _, get = release_evidence
    proof = verified_release(get, commit, "master", 1)
    assert proof["reviewers"] == ["reviewer"]
    assert proof["checks"] == {"ci.yml": 7, "agent-evals.yml": 7}
    assert proof["sdlc"]["intent"] == "https://example.test/issues/1"
    with pytest.raises(ValueError, match="did not merge"):
        verified_release(get, "9" * 40, "master", 1)


@pytest.mark.parametrize(("defect", "message"), [
    ("self", "independent"), ("stale", "independent"), ("dismissed", "independent"),
    ("running", "passing run"), ("other_commit", "passing run"), ("missing_design", "design link"),
])
def test_release_fails_closed(release_evidence, defect, message):
    commit, pr, reviews, runs, get = release_evidence
    if defect == "self":
        reviews[0]["user"]["login"] = "author"
    elif defect == "stale":
        reviews[0]["commit_id"] = "c" * 40
    elif defect == "dismissed":
        reviews.append(reviews[0] | {"state": "DISMISSED"})
    elif defect == "running":
        runs["agent-evals.yml"].append(runs["agent-evals.yml"][0] | {"id": 8, "status": "in_progress"})
    elif defect == "other_commit":
        runs["ci.yml"][0]["head_sha"] = "c" * 40
    elif defect == "missing_design":
        pr["body"] = "Intent: https://example.test/issues/1"
    with pytest.raises(ValueError, match=message):
        verified_release(get, commit, "master", 1)


def test_monitor_deduplicates_repeated_failure_and_marks_recovery():
    state = {}
    advance(state, ready=True, commit="a" * 40, now="0")
    for hour in range(1, 8):
        advance(state, ready=False, commit="b" * 40, now=str(hour))
        assert len(state["incidents"]) == int(hour >= 3)
    advance(state, ready=True, commit="c" * 40, now="8")
    assert state["incidents"][0]["recovered_at"] == "8"
    assert state["incidents"][0]["last_ready_commit"] == "a" * 40
    for hour in range(9, 12):
        advance(state, ready=False, commit=None, now=str(hour))
    assert len({row["id"] for row in state["incidents"]}) == 2


def test_monitor_persists_queue_for_api(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path))
    assert readiness_incidents() == {"observed": False, "incidents": []}
    for _ in range(3):
        record(tmp_path, sample=lambda: (False, "b" * 40))
    queue = readiness_incidents()
    assert queue["observed"] is True
    assert queue["incidents"][0]["failed_probes"] == 3
    assert queue["incidents"][0]["observed_commit"] == "b" * 40


def test_live_eval_sends_real_schemas_and_checks_synthetic_decisions():
    requests = []
    expected = iter(CASES)

    def post(url, *, json: dict):
        requests.append((url, json))
        _, _, choices, _ = next(expected)
        name, args = next(iter(choices.items()))
        output = [{"type": "function_call", "name": name, "arguments": dumps(args)}]
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"output": output})

    dumps = json.dumps
    results = run(SimpleNamespace(post=post), "configured-model")
    assert all(row["passed"] for row in results)
    assert all(payload["store"] is False for _, payload in requests)
    assert "odoo_search_read" in {tool["name"] for tool in requests[0][1]["tools"]}
    assert not score([{"type": "function_call", "name": "approvals_approve", "arguments": "{}"}], CASES[-1][2])

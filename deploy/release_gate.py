"""Record and enforce an operator's approval of an exact reviewed connector release."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.request import Request, urlopen

SHA = re.compile(r"[a-f0-9]{40}")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
WORKFLOWS = ("ci.yml", "agent-evals.yml")


def trace_links(body):
    links = {}
    for field in ("Intent", "Design"):
        match = re.search(rf"^{field}: (https://[A-Za-z0-9.-]+(?:/[A-Za-z0-9._~/%+-]+)+)\s*$", body, re.M)
        if match is None:
            raise ValueError(f"Pull request needs an authoritative {field.lower()} link")
        links[field.lower()] = match.group(1)
    task = re.search(r"^MCP task: ([a-f0-9]{32})\s*$", body, re.M)
    if task:
        links["task_id"] = task.group(1)
    return links


def verified_release(get, commit, branch, number):
    pr = get(f"pulls/{number}")
    if not (pr["merged"] and pr["merge_commit_sha"] == commit and pr["base"]["ref"] == branch):
        raise ValueError("Pull request did not merge the requested release into the deployment branch")
    links = trace_links(pr.get("body") or "")
    reviews = {}
    for page in range(1, 11):
        batch = get(f"pulls/{number}/reviews?per_page=100&page={page}")
        for review in batch:
            user = review.get("user") or {}
            if (user.get("type") == "User" and user.get("login") != pr["user"]["login"]
                    and review.get("author_association") in {"OWNER", "MEMBER", "COLLABORATOR"}
                    and review["state"] in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}):
                reviews[user["login"]] = review
        if len(batch) < 100:
            break
    else:
        raise ValueError("Review history exceeds verification limit")
    relevant = {user: row for user, row in reviews.items() if row.get("commit_id") == pr["head"]["sha"]}
    if any(row["state"] == "CHANGES_REQUESTED" for row in relevant.values()):
        raise ValueError("Changes are still requested on the reviewed head")
    approved = sorted(user for user, row in relevant.items() if row["state"] == "APPROVED")
    if not approved:
        raise ValueError("The final PR head needs an independent approval from a repository collaborator")
    checks = {}
    for workflow in WORKFLOWS:
        runs = get(f"actions/workflows/{workflow}/runs?head_sha={commit}&branch={branch}&per_page=30")["workflow_runs"]
        matching = [run for run in runs if run["head_sha"] == commit and run["head_branch"] == branch
                    and run["event"] in {"push", "workflow_dispatch", "schedule"}]
        latest = max(matching, key=lambda row: row["id"], default=None)
        if latest is None or latest["status"] != "completed" or latest["conclusion"] != "success":
            raise ValueError(f"{workflow} needs a passing run on the exact release commit")
        checks[workflow] = latest["id"]
    return {"pull_request": int(number), "reviewers": approved, "checks": checks, "sdlc": links}


def secure_read(path):
    # Only trust metadata controlled by the machine operator, including its directory.
    for entry in (path.parent, path):
        st = entry.lstat()
        if entry.is_symlink() or st.st_uid != 0 or st.st_mode & 0o022:
            raise ValueError("Release approval must be root-owned and protected from other writers")
    if not path.is_file():
        raise ValueError("Release approval must be a regular file")
    return json.loads(path.read_text(encoding="utf-8"))


def read_release(directory, repository, branch, commit):
    row = secure_read(directory / f"{commit}.json")
    if (row.get("repository"), row.get("branch"), row.get("commit")) != (repository, branch, commit):
        raise ValueError("Release approval does not match this repository, branch and commit")
    if not row.get("approved_by") or not row.get("reviewers") or set(row.get("checks", {})) != set(WORKFLOWS):
        raise ValueError("Release approval is missing required evidence")
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("approve", "check"))
    parser.add_argument("repository")
    parser.add_argument("branch")
    parser.add_argument("commit")
    parser.add_argument("--pull-request", type=int)
    parser.add_argument("--directory", type=Path, default=Path("/etc/odoo-mcp/release-approvals"))
    args = parser.parse_args()
    if (not SHA.fullmatch(args.commit) or not REPOSITORY.fullmatch(args.repository)
            or not re.fullmatch(r"[A-Za-z0-9._/-]+", args.branch)):
        parser.error("Invalid repository, branch or full commit SHA")
    try:
        if args.action == "approve":
            if not args.pull_request or args.pull_request < 1 or os.geteuid() != 0:
                raise ValueError("Approval requires a root operator and a pull request number")
            identity = os.environ.get("SUDO_USER") or "root"
            token = os.environ.get("GH_TOKEN")

            def get(endpoint):
                headers = {"Accept": "application/vnd.github+json", "User-Agent": "odoo-mcp-release-gate"}
                if token:
                    headers["Authorization"] = "Bearer " + token
                url = f"https://api.github.com/repos/{args.repository}/{endpoint}"
                with urlopen(Request(url, headers=headers), timeout=15) as response:
                    return json.load(response)

            evidence = verified_release(get, args.commit, args.branch, args.pull_request)
            args.directory.mkdir(mode=0o700, exist_ok=True)
            directory = args.directory.lstat()
            if args.directory.is_symlink() or directory.st_uid != 0 or directory.st_mode & 0o022:
                raise ValueError("Approval directory must be root-owned and protected from other writers")
            with tempfile.NamedTemporaryFile(mode="w", dir=args.directory, delete=False) as stream:
                json.dump(evidence | {"repository": args.repository, "branch": args.branch, "commit": args.commit,
                                     "approved_by": identity, "approved_at": datetime.now(timezone.utc).isoformat()}, stream)
                temp = Path(stream.name)
            temp.replace(args.directory / f"{args.commit}.json")
        row = read_release(args.directory, args.repository, args.branch, args.commit)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, f"Release gate blocked: {exc}\n")
    print(json.dumps(row, sort_keys=True))


if __name__ == "__main__":
    main()

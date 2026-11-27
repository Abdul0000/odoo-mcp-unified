"""Native commits never rewrite verified work to catch up with another writer."""
from __future__ import annotations

import subprocess

import pytest

from odoo_agent_server.config import settings
from odoo_agent_server.schemas import CommandResult
from odoo_agent_server.services.git_service import GitService


def git(repo, *args, check=True):
    return subprocess.run(["git", *args], cwd=repo, check=check, capture_output=True, text=True, timeout=20)


@pytest.fixture
def repos(tmp_path, monkeypatch):
    monkeypatch.setenv("ODOO_CLIENTS", "[]")
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "target_git_repo", None)
    monkeypatch.setattr(settings, "target_git_branch", None)
    local = tmp_path / "local"
    local.mkdir()
    git(local, "init", "-b", "staging")
    for key, value in (("user.name", "Test"), ("user.email", "test@example.com")):
        git(local, "config", key, value)
    (local / "hello.py").write_text("VALUE = 'base'\n")
    (local / "README.md").write_text("base\n")
    git(local, "add", ".")
    git(local, "commit", "-m", "base")
    bare = tmp_path / "origin.git"
    git(tmp_path, "clone", "--bare", str(local), str(bare))
    git(local, "remote", "add", "origin", str(bare))
    other = tmp_path / "other"
    git(tmp_path, "clone", str(bare), str(other))
    git(other, "config", "user.name", "Other")
    git(other, "config", "user.email", "other@example.com")
    return local, other, bare


def advance(other, path, content):
    (other / path).write_text(content)
    git(other, "add", "--", path)
    git(other, "commit", "-m", "upstream update")
    git(other, "push")


def snapshot(repo):
    return {
        "head": git(repo, "rev-parse", "HEAD").stdout,
        "branch": git(repo, "symbolic-ref", "HEAD", check=False).stdout,
        "index": git(repo, "ls-files", "--stage", "-z").stdout,
        "staged": git(repo, "diff", "--cached", "--binary").stdout,
        "stash": git(repo, "rev-parse", "--verify", "refs/stash", check=False).stdout,
        "files": {str(p.relative_to(repo)): p.read_bytes() for p in repo.rglob("*")
                  if p.is_file() and ".git" not in p.relative_to(repo).parts},
    }


@pytest.mark.parametrize("remote_change", ["unrelated", "overlap", "untracked_collision"])
def test_remote_advance_preserves_commits_index_worktree_and_stash(repos, remote_change):
    local, other, bare = repos
    (local / "saved.py").write_text("SAVED = True\n")
    git(local, "stash", "push", "--include-untracked", "-m", "operator saved work")
    (local / "hello.py").write_text("VALUE = 'local commit'\n")
    git(local, "add", "hello.py")
    git(local, "commit", "-m", "unpublished local commit")
    (local / "README.md").write_text("staged work\n")
    git(local, "add", "README.md")
    (local / "README.md").write_text("staged work plus unstaged work\n")
    (local / "new.py").write_text("LOCAL = True\n")
    path = {"unrelated": "other.py", "overlap": "hello.py", "untracked_collision": "new.py"}[remote_change]
    advance(other, path, "UPSTREAM = True\n")
    remote_head = git(bare, "rev-parse", "staging").stdout
    before = snapshot(local)
    for _ in range(2):
        result = GitService().commit(str(local), "must sync first", [])
        assert result.exit_code != 0 and "SYNC_REQUIRED" in result.stderr
        assert snapshot(local) == before
        assert not (local / ".git/rebase-merge").exists()
        assert not (local / ".git/rebase-apply").exists()
        assert git(bare, "rev-parse", "staging").stdout == remote_head


def test_existing_rebase_conflict_is_preserved_for_its_owner(repos):
    local, other, _ = repos
    (local / "hello.py").write_text("VALUE = 'local'\n")
    git(local, "add", "hello.py")
    git(local, "commit", "-m", "local change")
    advance(other, "hello.py", "VALUE = 'upstream'\n")
    git(local, "fetch", "origin")
    assert git(local, "rebase", "origin/staging", check=False).returncode != 0
    before = snapshot(local)
    state = (local / ".git/rebase-merge/head-name").read_text()
    result = GitService().commit(str(local), "must not resolve automatically", [])
    assert "GIT_OPERATION_IN_PROGRESS" in result.stderr
    assert snapshot(local) == before
    assert (local / ".git/rebase-merge/head-name").read_text() == state


def test_fetch_timeout_does_not_stage_or_rewrite_local_work(repos, monkeypatch):
    local, _, _ = repos
    (local / "hello.py").write_text("VALUE = 'pending'\n")
    (local / "new.py").write_text("NEW = True\n")
    before = snapshot(local)
    service = GitService()
    run = service._run

    def timeout_fetch(command, cwd, timeout_seconds):
        if command[:2] == ["git", "fetch"]:
            return CommandResult(command=command, cwd=str(cwd), exit_code=124, timed_out=True)
        return run(command, cwd, timeout_seconds)

    monkeypatch.setattr(service, "_run", timeout_fetch)
    result = service.commit(str(local), "must preserve", [])
    assert result.exit_code == 124 and result.timed_out
    assert snapshot(local) == before


@pytest.mark.parametrize("branch_kind", ["existing", "new", "no_remote"])
def test_current_or_unpublished_branch_can_commit_without_rewriting_history(repos, branch_kind):
    local, _, _ = repos
    if branch_kind == "new":
        git(local, "checkout", "-b", "new-feature")
    elif branch_kind == "no_remote":
        git(local, "remote", "remove", "origin")
    before = git(local, "rev-parse", "HEAD").stdout
    (local / "hello.py").write_text("VALUE = 'updated'\n")
    result = GitService().commit(str(local), "verified local edit", ["hello.py"])
    assert result.exit_code == 0, result.stderr
    assert git(local, "rev-parse", "HEAD^").stdout == before
    assert not git(local, "status", "--porcelain").stdout


def test_push_permission_rejection_is_not_misreported_as_remote_advance(repos, monkeypatch):
    local, _, _ = repos
    service = GitService()
    run = service._run
    denied = "[remote rejected] staging -> staging (permission denied)"

    def deny_push(command, cwd, timeout_seconds):
        if command[:2] == ["git", "push"]:
            return CommandResult(command=command, cwd=str(cwd), exit_code=1, stderr=denied)
        return run(command, cwd, timeout_seconds)

    monkeypatch.setattr(service, "_run", deny_push)
    result = service.push(str(local), branch="staging")
    assert result.exit_code == 1 and result.stderr == denied
    assert "SYNC_REQUIRED" not in result.stderr


def test_real_rejected_push_preserves_both_histories(repos):
    local, other, bare = repos
    (local / "hello.py").write_text("VALUE = 'local'\n")
    git(local, "add", "hello.py")
    git(local, "commit", "-m", "local commit")
    advance(other, "hello.py", "VALUE = 'upstream'\n")
    remote_head = git(bare, "rev-parse", "staging").stdout
    before = snapshot(local)
    result = GitService().push(str(local), branch="staging")
    assert result.exit_code != 0 and "SYNC_REQUIRED" in result.stderr
    assert snapshot(local) == before
    assert git(bare, "rev-parse", "staging").stdout == remote_head


def test_commit_paths_cannot_turn_into_git_add_options(repos):
    local, _, _ = repos
    (local / "-A").write_text("requested file\n")
    (local / "unrelated.py").write_text("UNRELATED = True\n")
    result = GitService().commit(str(local), "one requested file", ["-A"])
    assert result.exit_code == 0, result.stderr
    assert git(local, "show", "HEAD:-A").stdout == "requested file\n"
    assert git(local, "ls-files", "--", "unrelated.py").stdout == ""
    assert (local / "unrelated.py").read_text() == "UNRELATED = True\n"


def test_scoped_commit_preserves_existing_index_and_matches_verified_tree(repos):
    local, _, _ = repos
    (local / "hello.py").write_text("VALUE = 'updated'\n")
    (local / "unrelated.py").write_text("UNRELATED = True\n")
    git(local, "add", "--", "unrelated.py")
    service = GitService()
    before = snapshot(local)
    tree = service.prospective_commit_tree(str(local), ["hello.py"])
    result = service.commit(str(local), "scoped edit", ["hello.py"])
    assert result.exit_code != 0 and "Unrelated staged paths" in result.stderr
    assert snapshot(local) == before

    git(local, "reset", "HEAD", "--", "unrelated.py")
    result = service.commit(str(local), "scoped edit", ["hello.py"])
    assert result.exit_code == 0, result.stderr
    sha = git(local, "rev-parse", "HEAD").stdout.strip()
    assert service.commit_tree(str(local), sha) == tree
    assert git(local, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").stdout.strip() == "hello.py"


def test_verified_push_uses_pinned_commit_when_local_head_has_advanced(repos):
    local, _, bare = repos
    (local / "hello.py").write_text("VALUE = 'verified'\n")
    git(local, "add", "hello.py")
    git(local, "commit", "-m", "verified edit")
    verified_sha = git(local, "rev-parse", "HEAD").stdout.strip()
    (local / "hello.py").write_text("VALUE = 'later'\n")
    git(local, "add", "hello.py")
    git(local, "commit", "-m", "later local edit")
    later_sha = git(local, "rev-parse", "HEAD").stdout.strip()

    result = GitService().push(str(local), branch="staging", verified_sha=verified_sha)

    assert result.exit_code == 0, result.stderr
    assert git(bare, "rev-parse", "staging").stdout.strip() == verified_sha
    assert git(local, "rev-parse", "HEAD").stdout.strip() == later_sha

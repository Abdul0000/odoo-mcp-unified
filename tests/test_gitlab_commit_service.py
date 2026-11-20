import unittest

from odoo_agent_server.services.gitlab_commit_service import parse_gitlab_repo


class ParseGitLabRepo(unittest.TestCase):
    def test_https_nested_group(self):
        self.assertEqual(
            parse_gitlab_repo("https://gitlab.example.com/group/subgroup/project.git"),
            ("https://gitlab.example.com", "group/subgroup/project"),
        )

    def test_ssh_url(self):
        self.assertEqual(
            parse_gitlab_repo("git@gitlab.example.com:group/subgroup/project.git"),
            ("https://gitlab.example.com", "group/subgroup/project"),
        )

    def test_rejects_github_host(self):
        with self.assertRaisesRegex(ValueError, "GitHub"):
            parse_gitlab_repo("https://github.com/group/project.git")

    def test_requires_group_and_project(self):
        with self.assertRaisesRegex(ValueError, "group and project"):
            parse_gitlab_repo("https://gitlab.example.com/project")


if __name__ == "__main__":
    unittest.main()


def test_gitlab_update_and_delete_have_file_concurrency_preconditions():
    from odoo_agent_server.services.gitlab_commit_service import GitLabRepo
    from odoo_agent_server.services.github_commit_service import FileChange
    calls = []
    def transport(method, url, headers, body):
        import json
        calls.append((method, url, json.loads(body) if body else None))
        if '/branches/' in url:
            return 200, {}, {'commit': {'id': 'a' * 40}}
        if '/repository/tree?' in url:
            return 200, {}, [{'type': 'blob', 'path': p, 'id': p, 'mode': '100755'} for p in ['a.py', 'b.py']]
        if '/repository/files/' in url:
            assert 'ref=' + 'a' * 40 in url
            return 200, {}, {'last_commit_id': 'b' * 40}
        return 201, {}, {'id': 'c' * 40}
    repo = GitLabRepo('https://gitlab.invalid', 'group/repo', 'secret', transport=transport)
    repo.commit_to_branch('staging', [FileChange('a.py', 'new'), FileChange('b.py', None)], 'fix', expected_head='a' * 40)
    actions = calls[-1][2]['actions']
    assert [a['last_commit_id'] for a in actions] == ['b' * 40, 'b' * 40]
    assert repo.tree('a' * 40)['a.py']['mode'] == '100755'
    assert len([c for c in calls if '/repository/tree?' in c[1]]) == 1


def test_gitlab_missing_concurrency_proof_fails_closed():
    import pytest
    from odoo_agent_server.services.gitlab_commit_service import GitLabRepo
    from odoo_agent_server.services.github_commit_service import FileChange, GitHubError
    repo = GitLabRepo('https://gitlab.invalid', 'group/repo', '')
    repo.ref_exists = lambda branch: True
    repo.head = lambda branch: {'commit': 'a' * 40}
    repo.tree = lambda sha: {'a.py': {'sha': 'blob'}}
    repo.request = lambda method, path: {}
    with pytest.raises(GitHubError, match='concurrency proof'):
        repo.commit_to_branch('staging', [FileChange('a.py', 'new')], 'fix')


def test_directory_lookup_does_not_fetch_recursive_repository_tree():
    from odoo_agent_server.services.gitlab_commit_service import GitLabRepo
    calls = []
    def transport(method, url, headers, body):
        calls.append(url)
        assert 'recursive=false' in url and 'path=module' in url
        return 200, {}, [{'type': 'blob', 'path': 'module/a.py', 'id': 'blob'}]
    repo = GitLabRepo('https://gitlab.invalid', 'group/repo', '', transport=transport)
    assert repo.tree_at('a' * 40, 'module')['a.py']['sha'] == 'blob'
    assert len(calls) == 1


def test_gitlab_native_adapter_reads_and_pushes_without_touching_checkout(tmp_path):
    import subprocess
    from types import SimpleNamespace
    from odoo_agent_server.services.gitlab_commit_service import GitLabNativeRepo
    from odoo_agent_server.services.github_commit_service import FileChange

    def git(*args, cwd=None):
        return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()

    bare = tmp_path / "origin.git"
    checkout = tmp_path / "checkout"
    git("init", "--bare", str(bare))
    git("clone", str(bare), str(checkout))
    git("config", "user.name", "Test", cwd=checkout)
    git("config", "user.email", "test@example.invalid", cwd=checkout)
    (checkout / "a.py").write_text("old = True\n")
    git("add", "a.py", cwd=checkout)
    git("commit", "-m", "initial", cwd=checkout)
    git("branch", "-M", "17-nov-2026-stagging", cwd=checkout)
    git("push", "origin", "17-nov-2026-stagging", cwd=checkout)
    initial_head = git("rev-parse", "HEAD", cwd=checkout)
    dirty_index = git("write-tree", cwd=checkout)

    remote_url = bare.as_uri()
    client = SimpleNamespace(path=str(checkout), repo=remote_url, token=None,
                             authenticated_repo=remote_url)
    repo = GitLabNativeRepo(client, "https://gitlab.invalid", "group/project")
    assert repo.head("17-nov-2026-stagging")["commit"] == initial_head
    tree = repo.tree(initial_head)
    assert repo.blob_text(tree["a.py"]["sha"]) == "old = True\n"
    pushed = repo.commit_to_branch("17-nov-2026-stagging", [FileChange("a.py", "new = True\n")],
                                   "fix", expected_head=initial_head)

    assert git("rev-parse", "refs/heads/17-nov-2026-stagging", cwd=bare) == pushed["commit"]
    assert git("rev-parse", "HEAD", cwd=checkout) == initial_head
    assert git("write-tree", cwd=checkout) == dirty_index


def test_workspace_scope_fallback_only_handles_insufficient_scope():
    from types import SimpleNamespace
    from unittest.mock import patch
    from odoo_agent_server.services.gitlab_commit_service import GitLabError
    from odoo_agent_server.services.workspace_factory import GitLabScopeFallbackRepo

    client = SimpleNamespace()
    native = SimpleNamespace(head=lambda branch: {"commit": "native"})
    rest = SimpleNamespace(head=lambda branch: (_ for _ in ()).throw(
        GitLabError(403, "insufficient_scope: requires api read_api")))
    with patch("odoo_agent_server.services.workspace_factory.GitLabNativeRepo", return_value=native):
        assert GitLabScopeFallbackRepo(rest, client, "https://gitlab.invalid", "group/project").head("staging")["commit"] == "native"

    rest = SimpleNamespace(head=lambda branch: (_ for _ in ()).throw(GitLabError(403, "access denied")))
    with patch("odoo_agent_server.services.workspace_factory.GitLabNativeRepo") as native_factory:
        try:
            GitLabScopeFallbackRepo(rest, client, "https://gitlab.invalid", "group/project").head("staging")
        except GitLabError as exc:
            assert "access denied" in exc.message
        else:
            raise AssertionError("non-scope GitLab 403 must not be swallowed")
        native_factory.assert_not_called()

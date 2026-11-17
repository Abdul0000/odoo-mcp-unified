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

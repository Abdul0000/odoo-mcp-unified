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

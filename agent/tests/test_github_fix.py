"""Unit tests for github_fix helpers (no agent import)."""

import json
import unittest
from unittest.mock import MagicMock, patch

from github_fix import (
    BRANCH_PATTERN,
    _GIT_WITH_TOKEN_CRED,
    clone_relative_path,
    create_github_pull_request,
    public_remote,
    validate_branch,
    validate_owner_repo,
    _gh_env,
)


class TestGithubFixValidation(unittest.TestCase):
    def test_clone_path(self):
        self.assertEqual(clone_relative_path("devrev", "wisp"), "repos/devrev/wisp")

    def test_branch_pattern(self):
        self.assertTrue(BRANCH_PATTERN.match("cost-fix/sqs-url-cache"))
        self.assertFalse(BRANCH_PATTERN.match("main"))
        self.assertFalse(BRANCH_PATTERN.match("cost-fix/"))

    def test_validate_branch(self):
        validate_branch("cost-fix/ok-slug")
        with self.assertRaises(ValueError):
            validate_branch("feature/foo")

    def test_validate_owner_repo(self):
        validate_owner_repo("devrev", "marina", "devrev")
        with self.assertRaises(ValueError):
            validate_owner_repo("other", "marina", "devrev")

    def test_public_remote_has_no_token(self):
        url = public_remote("devrev", "wisp")
        self.assertEqual(url, "https://github.com/devrev/wisp.git")
        self.assertNotIn("x-access-token", url)

    def test_gh_env_sets_token_not_in_remote(self):
        env = _gh_env("secret-token-value")
        self.assertEqual(env["GH_TOKEN"], "secret-token-value")
        self.assertNotIn("secret-token-value", public_remote("devrev", "wisp"))

    def test_git_cred_helper_does_not_use_gh_cli(self):
        joined = " ".join(_GIT_WITH_TOKEN_CRED)
        self.assertIn("GH_TOKEN", joined)
        self.assertNotIn("gh auth", joined)

    @patch("github_fix.urllib.request.urlopen")
    def test_create_github_pull_request_returns_html_url(self, urlopen: MagicMock) -> None:
        resp = MagicMock()
        resp.read.return_value = json.dumps(
            {"html_url": "https://github.com/devrev/wisp/pull/1"}
        ).encode()
        urlopen.return_value.__enter__.return_value = resp
        url = create_github_pull_request(
            "devrev",
            "wisp",
            "fix: test",
            "body",
            "main",
            "cost-fix/test",
            "token",
        )
        self.assertEqual(url, "https://github.com/devrev/wisp/pull/1")


if __name__ == "__main__":
    unittest.main()

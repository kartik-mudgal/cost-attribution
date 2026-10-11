"""Unit tests for github_fix helpers (no agent import)."""

import base64
import json
import unittest
from unittest.mock import MagicMock, patch

from github_fix import (
    BRANCH_PATTERN,
    _git_auth_args,
    _git_env,
    clone_relative_path,
    create_github_pull_request,
    public_remote,
    validate_branch,
    validate_owner_repo,
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

    def test_git_auth_args_uses_basic_x_access_token(self):
        args = _git_auth_args("secret-token-value")
        expected = base64.b64encode(b"x-access-token:secret-token-value").decode()
        self.assertEqual(args, ["-c", f"http.extraHeader=Authorization: Basic {expected}"])
        self.assertNotIn("secret-token-value", public_remote("devrev", "wisp"))
        self.assertNotIn("Bearer", args[1])

    def test_git_env_disables_terminal_prompt(self):
        env = _git_env()
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")

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

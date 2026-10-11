"""Unit tests for github_fix helpers (no agent import)."""

import json
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from github_fix import (
    BRANCH_PATTERN,
    _ensure_origin,
    _git_config_args,
    _require_token,
    clone_relative_path,
    create_github_pull_request,
    public_remote,
    validate_branch,
    validate_owner_repo,
    verify_github_repo_access,
    _gh_env,
)
from pathlib import Path


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

    def test_git_config_uses_basic_extra_header_not_shell_helper(self):
        args = _git_config_args("ghp_test_token_value")
        joined = " ".join(args)
        self.assertIn("safe.directory=*", joined)
        self.assertIn("http.https://github.com/.extraHeader=Authorization: Basic", joined)
        self.assertNotIn("ghp_test_token_value", joined)
        self.assertNotIn("credential.helper=!f()", joined)

    def test_require_token_rejects_empty(self):
        with self.assertRaises(RuntimeError):
            _require_token("  ")

    @patch("github_fix.urllib.request.urlopen")
    def test_verify_github_repo_access_403(self, urlopen: MagicMock) -> None:
        err = urllib.error.HTTPError(
            "https://api.github.com/repos/devrev/argus",
            403,
            "Forbidden",
            {},
            None,
        )
        err.read = MagicMock(return_value=b'{"message":"Resource not accessible"}')
        urlopen.side_effect = err
        with self.assertRaises(RuntimeError) as ctx:
            verify_github_repo_access("devrev", "argus", "tok")
        self.assertIn("403", str(ctx.exception))
        self.assertIn("PAT rejected", str(ctx.exception))

    @patch("github_fix._run_git")
    def test_ensure_origin_adds_when_missing(self, run_git: MagicMock) -> None:
        missing = MagicMock(returncode=2, stdout="", stderr="")
        run_git.side_effect = [missing, MagicMock(returncode=0)]
        _ensure_origin(Path("/tmp/repo"), "https://github.com/devrev/wisp.git", "tok")
        run_git.assert_any_call(["remote", "get-url", "origin"], Path("/tmp/repo"), "tok", check=False)
        run_git.assert_any_call(
            ["remote", "add", "origin", "https://github.com/devrev/wisp.git"],
            Path("/tmp/repo"),
            "tok",
        )

    @patch("github_fix._run_git")
    def test_ensure_origin_set_url_when_present(self, run_git: MagicMock) -> None:
        run_git.side_effect = [MagicMock(returncode=0, stdout="https://old\n"), MagicMock(returncode=0)]
        _ensure_origin(Path("/tmp/repo"), "https://github.com/devrev/wisp.git", "tok")
        run_git.assert_any_call(
            ["remote", "set-url", "origin", "https://github.com/devrev/wisp.git"],
            Path("/tmp/repo"),
            "tok",
        )

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

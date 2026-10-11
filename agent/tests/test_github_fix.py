"""Unit tests for github_fix helpers (no agent import)."""

import json
import subprocess
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import MagicMock, patch

from github_fix import (
    BRANCH_PATTERN,
    _git_env,
    _redact,
    _run_git,
    authed_remote,
    clone_relative_path,
    clone_repository,
    create_github_pull_request,
    public_remote,
    validate_branch,
    validate_owner_repo,
)

TOKEN = "ghp_secretTokenValue123"


class TestRunGit(unittest.TestCase):
    def test_failure_carries_git_message_without_the_credential(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError) as ctx:
                _run_git(["remote", "set-url", "origin", "https://example.invalid/x.git"], Path(tmp), TOKEN)
        message = str(ctx.exception)
        self.assertIn("git remote failed (exit 128)", message)
        self.assertIn("not a git repository", message)
        self.assertNotIn(TOKEN, message)

    def test_check_false_returns_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = _run_git(["rev-parse", "HEAD"], Path(tmp), TOKEN, check=False)
        self.assertNotEqual(proc.returncode, 0)

    def test_timeout_does_not_leak_the_command(self):
        with patch("github_fix.subprocess.run", side_effect=subprocess.TimeoutExpired(["git", TOKEN], 1)):
            with self.assertRaises(RuntimeError) as ctx:
                _run_git(["clone", "x"], Path("."), TOKEN)
        self.assertNotIn(TOKEN, str(ctx.exception))

    def test_redact_hides_token_and_url_encoded_form(self):
        quoted = urllib.parse.quote(TOKEN, safe="")
        self.assertEqual(_redact(f"a {TOKEN} b {quoted}", TOKEN), "a *** b ***")


class TestCloneRepository(unittest.TestCase):
    def test_failed_clone_leaves_no_directory(self):
        def fake_run(args, cwd, token, check=True):
            if args[0] == "clone":
                Path(args[-1]).mkdir(parents=True)
                raise RuntimeError("git clone failed (exit 128): remote: invalid credentials")
            raise AssertionError(f"unexpected git {args[0]}")

        with tempfile.TemporaryDirectory() as tmp, patch("github_fix._run_git", side_effect=fake_run):
            with self.assertRaises(RuntimeError):
                clone_repository(Path(tmp), "devrev", "marina", "main", TOKEN, "devrev")
            self.assertFalse((Path(tmp) / "repos/devrev/marina").exists())

    def test_broken_clone_is_replaced(self):
        calls = []

        def fake_run(args, cwd, token, check=True):
            calls.append(args[0])
            # First origin rewrite on the broken clone fails; later scrub after reclone succeeds.
            if args[0] == "remote" and calls.count("remote") == 1:
                raise RuntimeError("git remote failed (exit 128): fatal: detected dubious ownership")
            if args[0] == "clone":
                (Path(args[-1]) / ".git").mkdir(parents=True)
            return subprocess.CompletedProcess(args, 0, stdout="abc123\n", stderr="")

        with tempfile.TemporaryDirectory() as tmp, patch("github_fix._run_git", side_effect=fake_run):
            (Path(tmp) / "repos/devrev/marina/.git").mkdir(parents=True)
            (Path(tmp) / "repos/devrev/marina/stale.txt").write_text("x")
            out = clone_repository(Path(tmp), "devrev", "marina", "main", TOKEN, "devrev")
            self.assertFalse((Path(tmp) / "repos/devrev/marina/stale.txt").exists())
        self.assertEqual(calls, ["remote", "clone", "remote", "rev-parse"])
        self.assertEqual(out["action"], "cloned")

    def test_clone_uses_authed_url_then_scrubs(self):
        seen = []

        def fake_run(args, cwd, token, check=True):
            seen.append(list(args))
            if args[0] == "clone":
                (Path(args[-1]) / ".git").mkdir(parents=True)
            return subprocess.CompletedProcess(args, 0, stdout="abc123\n", stderr="")

        with tempfile.TemporaryDirectory() as tmp, patch("github_fix._run_git", side_effect=fake_run):
            clone_repository(Path(tmp), "devrev", "marina", "main", TOKEN, "devrev")

        clone_args = next(a for a in seen if a[0] == "clone")
        self.assertIn(authed_remote("devrev", "marina", TOKEN), clone_args)
        scrub = next(
            a
            for a in seen
            if a[:3] == ["remote", "set-url", "origin"] and a[3] == public_remote("devrev", "marina")
        )
        self.assertTrue(scrub)


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

    def test_authed_remote_embeds_token(self):
        url = authed_remote("devrev", "wisp", "secret-token-value")
        self.assertEqual(
            url,
            "https://x-access-token:secret-token-value@github.com/devrev/wisp.git",
        )
        self.assertNotEqual(url, public_remote("devrev", "wisp"))

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

"""Clone a GitHub repo into the session workspace and open a fix PR."""

import json
import logging
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger("github_fix")

CLONE_DIR = "repos"
BRANCH_PATTERN = re.compile(r"^cost-fix/[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}$")
OWNER_REPO_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,99}$")
GIT_TIMEOUT_S = 300
GITHUB_API = "https://api.github.com"

# Session storage may be owned by another uid; without this git exits 128 with
# "detected dubious ownership" on every command inside the clone.
_GIT_SAFE_DIR = ["-c", "safe.directory=*"]


def validate_owner_repo(owner: str, repo: str, allowed_owner: str) -> None:
    if owner != allowed_owner:
        raise ValueError(f"only {allowed_owner!r} org repositories are supported")
    if not OWNER_REPO_PATTERN.match(owner) or not OWNER_REPO_PATTERN.match(repo):
        raise ValueError("invalid owner or repo name")


def validate_branch(branch: str) -> None:
    if not BRANCH_PATTERN.match(branch):
        raise ValueError("branch must match cost-fix/<slug> (letters, digits, ., _, -)")


def clone_relative_path(owner: str, repo: str) -> str:
    return f"{CLONE_DIR}/{owner}/{repo}"


def public_remote(owner: str, repo: str) -> str:
    return f"https://github.com/{owner}/{repo}.git"


def authed_remote(owner: str, repo: str, token: str) -> str:
    """HTTPS remote with PAT for clone/fetch/push only; scrub back to public_remote after."""
    pat = urllib.parse.quote(token.strip(), safe="")
    return f"https://x-access-token:{pat}@github.com/{owner}/{repo}.git"


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _ensure_git() -> None:
    if not shutil.which("git"):
        raise RuntimeError("git is not on PATH; install git on the AgentCore runtime")


def _redact(text: str, token: str) -> str:
    if not text or not token:
        return text
    secrets = {token.strip(), urllib.parse.quote(token.strip(), safe="")}
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def _run_git(args: list[str], cwd: Path, token: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run git. Errors carry git's own message, redacted; never echo argv (may contain PAT)."""
    step = args[0]
    start = time.monotonic()
    try:
        proc = subprocess.run(
            ["git", *_GIT_SAFE_DIR, *args],
            cwd=cwd,
            env=_git_env(),
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        logger.error("git %s timed out after %ss cwd=%s", step, GIT_TIMEOUT_S, cwd)
        raise RuntimeError(f"git {step} timed out after {GIT_TIMEOUT_S}s") from None
    elapsed = time.monotonic() - start
    stderr = _redact((proc.stderr or "").strip(), token)
    if proc.returncode == 0:
        logger.info("git %s ok in %.1fs cwd=%s", step, elapsed, cwd)
        return proc
    logger.warning("git %s exit=%s in %.1fs cwd=%s stderr=%s", step, proc.returncode, elapsed, cwd, stderr[-500:])
    if check:
        raise RuntimeError(f"git {step} failed (exit {proc.returncode}): {stderr[-500:] or 'no output'}")
    return proc


def _set_origin(repo_dir: Path, url: str, token: str) -> None:
    _run_git(["remote", "set-url", "origin", url], repo_dir, token)


def create_github_pull_request(
    owner: str,
    repo: str,
    title: str,
    body: str,
    base_branch: str,
    head_branch: str,
    token: str,
) -> str:
    """Open a pull request via GitHub REST API; returns html_url."""
    payload = json.dumps(
        {
            "title": title,
            "body": body,
            "base": base_branch,
            "head": head_branch,
        }
    ).encode()
    req = urllib.request.Request(
        f"{GITHUB_API}/repos/{owner}/{repo}/pulls",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token.strip()}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = _redact(e.read().decode()[:500], token)
        logger.warning(
            "create pull request %s/%s %s -> %s: HTTP %s %s",
            owner,
            repo,
            head_branch,
            base_branch,
            e.code,
            detail,
        )
        raise RuntimeError(f"GitHub API {e.code} creating pull request: {detail}") from e
    url = data.get("html_url")
    if not url:
        raise RuntimeError("GitHub API did not return pull request URL")
    return str(url)


def clone_repository(
    workspace: Path,
    owner: str,
    repo: str,
    base_branch: str,
    token: str,
    allowed_owner: str,
) -> dict[str, Any]:
    """Shallow-clone or refresh a repo under workspace/repos/<owner>/<repo>."""
    _ensure_git()
    validate_owner_repo(owner, repo, allowed_owner)
    base_branch = base_branch.strip() or "main"
    rel = clone_relative_path(owner, repo)
    dest = (workspace / rel).resolve()
    root = workspace.resolve()
    if root not in dest.parents:
        raise ValueError("clone path must stay inside the workspace")

    public = public_remote(owner, repo)
    authed = authed_remote(owner, repo, token)
    logger.info("clone_repository start %s/%s branch=%s dest=%s", owner, repo, base_branch, dest)
    action = ""
    if dest.is_dir() and (dest / ".git").is_dir():
        try:
            _set_origin(dest, authed, token)
            try:
                _run_git(["fetch", "origin", base_branch, "--depth", "50"], dest, token)
                _run_git(["checkout", base_branch], dest, token)
                _run_git(["reset", "--hard", f"origin/{base_branch}"], dest, token)
                action = "refreshed"
            finally:
                _set_origin(dest, public, token)
        except RuntimeError as e:
            # Refresh discards local edits anyway; a broken clone (e.g. left by an earlier
            # failed run) is replaced with a fresh one.
            logger.warning("refresh of %s failed, recloning: %s", rel, e)
            shutil.rmtree(dest, ignore_errors=True)
    elif dest.exists():
        logger.warning("%s exists but is not a git clone, removing it", rel)
        shutil.rmtree(dest, ignore_errors=True)

    if not action:
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            _run_git(
                [
                    "clone",
                    "--branch",
                    base_branch,
                    "--single-branch",
                    "--depth",
                    "50",
                    authed,
                    str(dest),
                ],
                dest.parent,
                token,
            )
            _set_origin(dest, public, token)
        except RuntimeError:
            shutil.rmtree(dest, ignore_errors=True)
            raise
        action = "cloned"

    head = _run_git(["rev-parse", "HEAD"], dest, token).stdout.strip()
    logger.info("clone_repository done %s/%s action=%s head=%s", owner, repo, action, head)
    return {
        "workspace_path": rel,
        "base_branch": base_branch,
        "head_sha": head,
        "action": action,
    }


def create_fix_pull_request(
    workspace: Path,
    owner: str,
    repo: str,
    branch: str,
    base_branch: str,
    title: str,
    body: str,
    commit_message: str,
    token: str,
    allowed_owner: str,
    paths: list[str] | None,
) -> dict[str, Any]:
    """Commit workspace edits in the clone, push branch, and open a GitHub PR."""
    _ensure_git()
    validate_owner_repo(owner, repo, allowed_owner)
    validate_branch(branch)
    base_branch = base_branch.strip() or "main"
    rel = clone_relative_path(owner, repo)
    repo_dir = (workspace / rel).resolve()
    if not (repo_dir / ".git").is_dir():
        raise FileNotFoundError(f"clone missing at {rel}; call github_clone_repo first")

    public = public_remote(owner, repo)
    authed = authed_remote(owner, repo, token)
    _run_git(["config", "user.email", "cost-investigator-agent@users.noreply.github.com"], repo_dir, token)
    _run_git(["config", "user.name", "cost-investigator-agent"], repo_dir, token)

    existing = _run_git(["branch", "--list", branch], repo_dir, token, check=False)
    if branch in existing.stdout:
        _run_git(["checkout", branch], repo_dir, token)
    else:
        _run_git(["checkout", "-b", branch], repo_dir, token)

    if paths:
        for path in paths:
            if ".." in path or path.startswith("/"):
                raise ValueError(f"invalid path {path!r}")
            if not (repo_dir / path).exists():
                raise FileNotFoundError(f"no file to commit at {path!r} under {rel}")
        for path in paths:
            _run_git(["add", "--", path], repo_dir, token)
    else:
        _run_git(["add", "-A"], repo_dir, token)

    diff = _run_git(["diff", "--cached", "--quiet"], repo_dir, token, check=False)
    if diff.returncode == 0:
        raise ValueError("no staged changes; edit files under the clone path before creating a PR")

    _run_git(["commit", "-m", commit_message], repo_dir, token)
    commit_sha = _run_git(["rev-parse", "HEAD"], repo_dir, token).stdout.strip()

    try:
        _set_origin(repo_dir, authed, token)
        _run_git(["push", "-u", "origin", branch], repo_dir, token)
    finally:
        _set_origin(repo_dir, public, token)

    logger.info("opening pull request %s/%s %s -> %s", owner, repo, branch, base_branch)
    pr_url = create_github_pull_request(
        owner,
        repo,
        title,
        body,
        base_branch,
        branch,
        token,
    )
    return {
        "workspace_path": rel,
        "branch": branch,
        "commit_sha": commit_sha,
        "pull_request_url": pr_url,
    }

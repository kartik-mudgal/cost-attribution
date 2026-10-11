"""Clone a GitHub repo into the session workspace and open a fix PR."""

import re
import subprocess
from pathlib import Path
from typing import Any, Callable

CLONE_DIR = "repos"
BRANCH_PATTERN = re.compile(r"^cost-fix/[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}$")
OWNER_REPO_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,99}$")
GIT_TIMEOUT_S = 300


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


def _git_env() -> dict[str, str]:
    import os

    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _run_git(args: list[str], cwd: Path, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=_git_env(),
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_S,
        check=check,
    )


def _authenticated_remote(owner: str, repo: str, token: str) -> str:
    return f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"


def clone_repository(
    workspace: Path,
    owner: str,
    repo: str,
    base_branch: str,
    token: str,
    allowed_owner: str,
) -> dict[str, Any]:
    """Shallow-clone or refresh a repo under workspace/repos/<owner>/<repo>."""
    validate_owner_repo(owner, repo, allowed_owner)
    base_branch = base_branch.strip() or "main"
    rel = clone_relative_path(owner, repo)
    dest = (workspace / rel).resolve()
    root = workspace.resolve()
    if root not in dest.parents:
        raise ValueError("clone path must stay inside the workspace")

    remote = _authenticated_remote(owner, repo, token)
    if dest.is_dir() and (dest / ".git").is_dir():
        _run_git(["remote", "set-url", "origin", remote], dest)
        _run_git(["fetch", "origin", base_branch, "--depth", "50"], dest)
        _run_git(["checkout", base_branch], dest)
        _run_git(["reset", "--hard", f"origin/{base_branch}"], dest)
        action = "refreshed"
    else:
        if dest.exists() and not (dest / ".git").is_dir():
            raise ValueError(f"{rel} exists and is not a git clone; remove it first")
        dest.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            [
                "git",
                "clone",
                "--branch",
                base_branch,
                "--single-branch",
                "--depth",
                "50",
                remote,
                str(dest),
            ],
            env=_git_env(),
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or "git clone failed").strip()[:500])
        action = "cloned"

    head = _run_git(["rev-parse", "HEAD"], dest).stdout.strip()
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
    create_pr: Callable[[str, str, str, str, str, str], dict[str, Any]],
) -> dict[str, Any]:
    """Commit workspace edits in the clone, push branch, and open a GitHub PR."""
    validate_owner_repo(owner, repo, allowed_owner)
    validate_branch(branch)
    base_branch = base_branch.strip() or "main"
    rel = clone_relative_path(owner, repo)
    repo_dir = (workspace / rel).resolve()
    if not (repo_dir / ".git").is_dir():
        raise FileNotFoundError(f"clone missing at {rel}; call github_clone_repo first")

    _run_git(["remote", "set-url", "origin", _authenticated_remote(owner, repo, token)], repo_dir)
    _run_git(["config", "user.email", "cost-investigator-agent@users.noreply.github.com"], repo_dir)
    _run_git(["config", "user.name", "cost-investigator-agent"], repo_dir)

    # Branch from current HEAD (expected to match base after clone/refresh).
    existing = _run_git(["branch", "--list", branch], repo_dir, check=False)
    if branch in existing.stdout:
        _run_git(["checkout", branch], repo_dir)
    else:
        _run_git(["checkout", "-b", branch], repo_dir)

    if paths:
        for path in paths:
            if ".." in path or path.startswith("/"):
                raise ValueError(f"invalid path {path!r}")
            if not (repo_dir / path).exists():
                raise FileNotFoundError(f"no file to commit at {path!r} under {rel}")
        for path in paths:
            _run_git(["add", "--", path], repo_dir)
    else:
        _run_git(["add", "-A"], repo_dir)

    diff = _run_git(["diff", "--cached", "--quiet"], repo_dir, check=False)
    if diff.returncode == 0:
        raise ValueError("no staged changes; edit files under the clone path before creating a PR")

    _run_git(["commit", "-m", commit_message], repo_dir)
    commit_sha = _run_git(["rev-parse", "HEAD"], repo_dir).stdout.strip()

    push = subprocess.run(
        ["git", "push", "-u", "origin", branch],
        cwd=repo_dir,
        env=_git_env(),
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_S,
    )
    if push.returncode != 0:
        raise RuntimeError((push.stderr or push.stdout or "git push failed").strip()[:500])

    pr = create_pr(owner, repo, title, body, branch, base_branch)
    return {
        "workspace_path": rel,
        "branch": branch,
        "commit_sha": commit_sha,
        "pull_request_url": pr.get("html_url"),
        "pull_request_number": pr.get("number"),
    }

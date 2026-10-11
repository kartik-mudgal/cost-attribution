"""Clone a GitHub repo into the session workspace and open a fix PR."""

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

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


def public_remote(owner: str, repo: str) -> str:
    return f"https://github.com/{owner}/{repo}.git"


_GIT_WITH_GH_CRED = [
    "-c",
    "credential.helper=",
    "-c",
    "credential.helper=!gh auth git-credential",
]


def _gh_env(token: str) -> dict[str, str]:
    import os

    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GH_TOKEN"] = token.strip()
    return env


def _ensure_gh() -> None:
    if not shutil.which("gh"):
        raise RuntimeError("gh CLI is not on PATH; install GitHub CLI on the AgentCore runtime")


def _redact(text: str, token: str) -> str:
    if not text or not token:
        return text
    return text.replace(token, "***")


def _run_git(args: list[str], cwd: Path, token: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *_GIT_WITH_GH_CRED, *args],
        cwd=cwd,
        env=_gh_env(token),
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_S,
        check=check,
    )


def _run_gh(args: list[str], cwd: Path | None, token: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["gh", *args],
        cwd=cwd,
        env=_gh_env(token),
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_S,
    )


def _fail(proc: subprocess.CompletedProcess[str], token: str, label: str) -> None:
    detail = _redact((proc.stderr or proc.stdout or label).strip(), token)
    raise RuntimeError(detail[:500])


def clone_repository(
    workspace: Path,
    owner: str,
    repo: str,
    base_branch: str,
    token: str,
    allowed_owner: str,
) -> dict[str, Any]:
    """Shallow-clone or refresh a repo under workspace/repos/<owner>/<repo>."""
    _ensure_gh()
    validate_owner_repo(owner, repo, allowed_owner)
    base_branch = base_branch.strip() or "main"
    rel = clone_relative_path(owner, repo)
    dest = (workspace / rel).resolve()
    root = workspace.resolve()
    if root not in dest.parents:
        raise ValueError("clone path must stay inside the workspace")

    remote = public_remote(owner, repo)
    if dest.is_dir() and (dest / ".git").is_dir():
        _run_git(["remote", "set-url", "origin", remote], dest, token)
        _run_git(["fetch", "origin", base_branch, "--depth", "50"], dest, token)
        _run_git(["checkout", base_branch], dest, token)
        _run_git(["reset", "--hard", f"origin/{base_branch}"], dest, token)
        action = "refreshed"
    else:
        if dest.exists() and not (dest / ".git").is_dir():
            raise ValueError(f"{rel} exists and is not a git clone; remove it first")
        dest.parent.mkdir(parents=True, exist_ok=True)
        proc = _run_gh(
            [
                "repo",
                "clone",
                f"{owner}/{repo}",
                str(dest),
                "--",
                "--branch",
                base_branch,
                "--single-branch",
                "--depth",
                "50",
            ],
            None,
            token,
        )
        if proc.returncode != 0:
            _fail(proc, token, "gh repo clone failed")
        _run_git(["remote", "set-url", "origin", remote], dest, token)
        action = "cloned"

    head = _run_git(["rev-parse", "HEAD"], dest, token).stdout.strip()
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
    """Commit workspace edits in the clone, push branch, and open a GitHub PR via gh."""
    _ensure_gh()
    validate_owner_repo(owner, repo, allowed_owner)
    validate_branch(branch)
    base_branch = base_branch.strip() or "main"
    rel = clone_relative_path(owner, repo)
    repo_dir = (workspace / rel).resolve()
    if not (repo_dir / ".git").is_dir():
        raise FileNotFoundError(f"clone missing at {rel}; call github_clone_repo first")

    _run_git(["remote", "set-url", "origin", public_remote(owner, repo)], repo_dir, token)
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

    push = subprocess.run(
        ["git", *_GIT_WITH_GH_CRED, "push", "-u", "origin", branch],
        cwd=repo_dir,
        env=_gh_env(token),
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_S,
    )
    if push.returncode != 0:
        _fail(push, token, "git push failed")

    pr_proc = _run_gh(
        [
            "pr",
            "create",
            "--repo",
            f"{owner}/{repo}",
            "--title",
            title,
            "--body",
            body,
            "--base",
            base_branch,
            "--head",
            branch,
        ],
        repo_dir,
        token,
    )
    if pr_proc.returncode != 0:
        _fail(pr_proc, token, "gh pr create failed")

    pr_url = pr_proc.stdout.strip()
    return {
        "workspace_path": rel,
        "branch": branch,
        "commit_sha": commit_sha,
        "pull_request_url": pr_url,
    }

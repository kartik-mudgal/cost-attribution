---
name: github-fix-pr
description: >
  Open a GitHub pull request with a cost-fix code change after investigation. Clone the
  application repo into the workspace, apply a minimal fix, and use github_create_fix_pr.
  Load github-code-lookup first. Only when the user wants a fix or explicitly approves a PR.
---

# Propose a cost fix via pull request

Use this skill when:

- Investigation found a **specific code change** that removes waste or corrects misconfiguration, and
- The user asked to **implement the fix**, **open a PR**, or **draft a change** for review.

Do **not** open a PR for speculative fixes, low-confidence findings, or marina-only deploy changes.

## Preconditions

1. **Evidence in hand:** CUR query IDs, resource name, and (if possible) metrics or code path cited.
2. **Repo identified:** Usually `devrev/<service>` from code search; not `marina` unless the fix is Helm-only.
3. **User intent:** If the user only wanted analysis, describe the fix in the answer and ask before cloning.

AWS stays read-only. GitHub writes are limited to this workflow.

## Workflow

### 1. Clone

```
github_clone_repo(owner="devrev", repo="<service>", base_branch="main")
```

Clone path: `repos/devrev/<service>/` (workspace-relative).

Use workspace paths like `repos/devrev/wisp/pkg/queue/client.go`.

**Prefer `edit_workspace_file`** (search-and-replace with enough context that `old_string`
matches once). That keeps tool payloads small and fast on AgentCore. Use `read_workspace_file`
only for the section you need—not necessarily the whole file. Reserve `write_workspace_file` for
new notes under `notes/`, not for large cloned source files.

Re-read surrounding code before editing; keep the diff **minimal** (one concern per PR).

### 2. Implement the fix

- Match existing style, imports, and error handling.
- No unrelated refactors, drive-by formatting, or new dependencies unless required.
- Prefer fixes that are easy to verify (e.g. reuse queue URL, enable long polling, batch writes).

Document what you changed in workspace notes (`notes/fix-<service>.md`).

### 3. Open the PR

Branch name **must** match `cost-fix/<short-slug>` (e.g. `cost-fix/sqs-cache-queue-url`).

```
github_create_fix_pr(
  owner="devrev",
  repo="<service>",
  branch="cost-fix/<slug>",
  title="fix(<area>): <one-line savings>",
  body="<markdown: problem, evidence, change, estimated monthly saving, risks>",
  commit_message="fix(<area>): <same as title without prefix if preferred>",
  base_branch="main",
  paths="path/one,path/two"   # omit to stage all changes under the clone
)
```

On success, return **`pull_request_url`** in the answer. On error JSON, explain and do not invent a URL.

### 4. PR body template

```markdown
## Cost issue
<resource> — <usage line> — ~$X/mo run-rate (CUR query `…`)

## Cause
<one paragraph, link to code before fix>

## Change
<what this PR does>

## Evidence
- Athena: `<query_execution_id>` …
- Metrics: … (if used)

## Estimated impact
~$Y/mo if <assumption> (confidence: high/medium)

## Notes
- [ ] Human review required before merge
```

## Marina (Helm) fixes

If the fix belongs in **`devrev/marina`** (values, resources), clone `marina` instead and use the same
PR tools. Prefer application-repo fixes when the bug is in client code (e.g. GetQueueUrl per send).

## Limits

- Only **`devrev`** org repos (enforced by tools).
- Runtime must have **`git`** on PATH. Clone/push authenticate via `GH_TOKEN` (credential helper;
  token never in remote URLs). PRs use the GitHub REST API. Shallow history (`--depth 50`).
- Large repos: edit only files you need; use `paths` to stage a tight commit.
- PAT must allow **contents** and **pull requests** on the target repo.
- Never log or print the token.

## Confidence gate

| Confidence | Action |
| --- | --- |
| High (code + CUR) | OK to open PR when user asked for a fix |
| Medium | Propose diff in the answer; ask before `github_create_fix_pr` |
| Low | Do not open a PR |

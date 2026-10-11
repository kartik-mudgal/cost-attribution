---
name: devrev-issue
description: >
  File a DevRev issue for a cost finding or recommended fix using devrev_create_issue.
  Routing (part, team, space, tag, stage) is fixed; only title and body vary. Load when the
  user asks to create/track an issue or after investigation when filing to the cost queue.
---

# File a DevRev issue for a cost finding

Use when the user wants a **DevRev issue** for a finding, or your investigation recommends
tracking work in DevRev (cost queue). Do not file issues for low-confidence speculation.

## Preconditions

1. **Finding is documented:** verdict, resource, CUR query IDs, estimated savings, confidence.
2. **User intent:** user said to create/file an issue, or agreed after you proposed filing.
3. Load this skill, then call **`devrev_create_issue`** once per distinct finding (not one mega-issue).

AWS and GitHub stay read-only unless another skill applies. This is the only DevRev write path.

## Fixed routing (do not change)

These are set by the tool from environment defaults (cost attribution queue):

| Field | Purpose |
| --- | --- |
| `applies_to_part` | Cost / platform feature part |
| `owned_by` | Owning service account / team |
| `space` | Issue space |
| `tags` | Cost attribution tag |
| `stage` | Initial stage |
| `priority_v2` | Default priority (3) |
| `type` | `issue` |

You only supply **`title`** and **`body`**.

## Title

- Short, actionable: `[Cost] <service> — <resource> — <one-line fix or symptom>`
- Example: `[Cost] SQS hubble-parquet-ingest-queue — cache GetQueueUrl (~$54/mo)`

## Body (markdown)

Use this structure:

```markdown
## Summary
<verdict and one paragraph>

## Resource
- **AWS:** <resource id / name>
- **Service / owner:** <marina or repo name if known>

## Evidence
- CUR: query `<query_execution_id>` — <figures>
- Metrics: <day and metric names if used>
- Code: `<repo>/<path>` — <what you found>
- Deploy: <marina commit / tag if correlated>

## Recommendation
<fix steps; link PR if github-fix-pr was used>

## Savings (estimate)
- **Monthly:** ~$X (assumption: …)
- **Confidence:** high / medium / low

## Links
- PR: <url if any>
- Artifact: <presigned report url if any>
```

Paste **real** query IDs and numbers from the investigation only.

## Tool

```
devrev_create_issue(title="...", body="...")
```

On success, include **`url`** and **`display_id`** in the answer. On `error` in the JSON response,
explain and do not invent an issue link.

## When to file vs PR

| Situation | Action |
| --- | --- |
| Fix needs code | Prefer `github-fix-pr` first; reference PR in issue body |
| Tracking / ownership only | `devrev_create_issue` |
| User wants both | PR then issue (or issue with “PR pending”) |

## Limits

- Token is loaded from Secrets Manager; never print or request it.
- PAT is short-lived; if creation fails with 401, ask the operator to refresh the secret.
- One issue per finding keeps triage clear.

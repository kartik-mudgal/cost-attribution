---
name: marina-deployments
description: >
  Attribute cost or metric changes to Kubernetes deploys recorded in devrev/marina: image tag
  changes per service and environment, with commit SHA and PR links. Load after you know the
  owning marina service name and the cost/metric change window (especially for anomalies and
  deep dives). Use with github-code-lookup.
---

# Marina deploy history (image tag changes)

Deployments are **Git commits on `devrev/marina`** that change an application **`image.tag`**
in Helm values for a given **service** and **environment**. Argo CD syncs that revision to the
cluster; treat the **commit timestamp** on `main` as deploy time unless the user supplies Argo
sync time.

Load **github-code-lookup** before any GitHub call. Never invent commit SHAs, tags, or deploy
times — only report what tools return.

## Map AWS account → marina environment

Match the CUR row's `line_item_usage_account_id` (or the account in the system prompt) to the
env segment in marina paths:

| AWS account (examples) | Marina env folder | Notes |
| --- | --- | --- |
| `020040093233` | `dev` | Cost investigator default dev account |
| `990737956189` | `qa` | |
| (prod accounts) | `prod` | Use only if CUR data is for that account |

Also exists: `dev-ingest`, `qa-ingest`, `prod-ingest`, `infra-prod` for infra charts. If the
owning service lives under one of those, use that segment instead of `dev`/`qa`/`prod`.

## Marina layout (where tags live)

```
<marina-service>/
├── values.yaml                    # project-wide (repository, shared keys)
├── <env>/values.yaml              # usual place for image.tag for all regions in env
└── <env>/<region>/values.yaml     # regional override (can override image.tag)
```

- **Marina service name** = top-level folder (e.g. `codex`, `wisp`). Often matches the
  Kubernetes namespace and ECR image name; confirm with a code search if unsure.
- **Values key** = usually the same as the folder name, nested under it:

```yaml
codex:
  image:
    tag: v1322bdc
```

- **Tag format:** `v` + short git SHA of the **application** image (e.g. `v1322bdc`). That SHA
  is the commit deployed in the **application source repo** (often `devrev/<marina-service>`;
  confirm with `github_search_code` or anchor `devrev.ai/source-repository` if needed).

**Chart.yaml** dependency version bumps are template changes, not an app deploy. Prefer
**`image.tag`** changes for attributing runtime behavior.

## When to use this skill

Use after you have:

1. **When** cost moved — at least the **day**; for anomalies, the **hour** from hourly CUR.
2. **Which resource** — queue, table, etc., and the **owning marina service** (from Terraform,
   marina Helm references, `github_search_code` on queue/table name, or CUR `resource_tags`).
3. **Metric context** (optional) — same day from `cloudwatch_metrics`; daily granularity only.

Do **not** use for quick cost-only questions with no change investigation.

## Investigation workflow

### 1. Pick the values file path

Default path to scan:

`{marina-service}/{env}/values.yaml`

If the service is region-specific in dev (e.g. only `us-east-1`), also check:

`{marina-service}/{env}/us-east-1/values.yaml`

(and other regions if CUR `product_region_code` points elsewhere).

### 2. List commits that touched that file in the window

Use **`github_list_commits`** on `owner=devrev`, `repo=marina`, `path=<values path>`.

- **`since`:** ISO 8601 start — use **24 hours before** the cost spike hour (or start of spike
  day if you only have a day).
- **`until`:** ISO 8601 end — **6 hours after** the spike hour (or end of spike day).
- Request enough commits (`per_page` up to 100) to cover the window; paginate with a second call
  using an earlier `until` if the first page stops before the window starts.

Save notes to the workspace, e.g. `notes/deploys-<service>-<date>.md`.

### 3. Detect deploy events (tag changes)

Walk commits **newest first** within the window. For each commit SHA:

1. **`github_get_file`**(`devrev`, `marina`, path, `ref=<sha>`).
2. Parse `image.tag` under the service key (and under nested keys like `migrations.image.tag`
   only if your resource is tied to that subchart).
3. When **`tag` differs** from the tag at the **next older** commit in the list, record a
   **deploy event**:

| Field | Source |
| --- | --- |
| `deployed_at` | Commit `date` from `github_list_commits` |
| `marina_commit` | Commit `sha` |
| `marina_pr` | Parse `#12345` from commit `message` if present |
| `tag_before` / `tag_after` | Parsed from consecutive file reads |
| `values_path` | Path you scanned |
| `bulk_update` | `true` if message matches `update ECR Docker images` and many services changed |

If **no tag change** appears in the window, say so and widen the window by 24h once before
concluding no marina deploy correlates.

### 4. Link tag → application commit

For each `tag_after` (e.g. `v1322bdc`):

- Application ref = tag without leading `v` (may need full SHA via GitHub if short SHA is
  ambiguous — prefer the marina commit message or compare API if lookup fails).
- **`github_get_file`** on the **source repo** at that ref to sample what shipped (optional).
- **`github_search_commits`** on the source repo with `author-date` in the same window for
  human context if the tag alone is not enough.

Report **marina commit** as “what was deployed to the cluster” and **application commit/tag**
as “what code revision ran.”

### 5. Correlate to cost and metrics

| Evidence | Interpretation |
| --- | --- |
| Deploy **0–6h before** hourly cost spike | **Candidate** deploy (medium confidence) |
| Deploy same day, metrics up next day | Consistent with daily metrics; cite both |
| Deploy after spike | Unlikely cause — deprioritize |
| Bulk ECR bot commit | Only attribute if **this service's** values file changed in that commit |
| Multiple deploys in window | List all; pick closest before spike unless code rules one out |

**Confidence**

- **High:** Deploy before spike **and** application diff explains the usage line (e.g. polling
  change) **or** metrics move in the expected direction.
- **Medium:** Time-aligned deploy and tag change, cause not verified in app repo.
- **Low:** Deploy in window but no plausible link to the cost line.

Always separate **confirmed** cause from **deploy correlation** in the finding text.

## Finding template (paste into answers)

```markdown
### Deploy correlation — <marina-service> (<env>)
- **Cost / metric window:** <dates and hour if known> (CUR query IDs: …)
- **Deploy:** <deployed_at> UTC — marina [`<sha>`](https://github.com/devrev/marina/commit/<sha>)
  — `tag_before` → `tag_after` in `<values_path>` (PR #… if known)
- **Application revision:** `devrev/<repo>` @ `<tag_after>` / commit `<app-sha>`
- **Alignment:** deploy was <N>h before CUR spike hour <H>
- **Confidence:** medium — tag change time-aligned; verify <what to check in app diff>
```

## Tool reference

| Tool | Use |
| --- | --- |
| `github_list_commits` | Commits touching `{service}/{env}/values.yaml` in the time window |
| `github_get_file` | Read values at a commit SHA; read app repo at image tag |
| `github_search_code` | Find marina path or which service references a queue/table |
| `github_search_commits` | Extra context in application repo around the window |

## Limits

- Git history reflects **intent** on `main`; cluster lag is usually minutes.
- Regional overrides require scanning the correct regional `values.yaml`.
- One shared resource (queue/table) may have **several** deploy candidates — list them all.
- Without hourly CUR, do not claim hour-level alignment; use day-level language only.

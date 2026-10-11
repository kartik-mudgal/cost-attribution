"""Create DevRev issues via the internal works.create gateway."""

import json
import urllib.error
import urllib.request
from typing import Any, Callable

DEFAULT_API_BASE = "https://app.devrev.ai/api/gateway/internal"
DEFAULT_APPLIES_TO_PART = "don:core:dvrv-us-1:devo/0:feature/2971"
DEFAULT_OWNED_BY = ["don:identity:dvrv-us-1:devo/0:svcacc/273"]
DEFAULT_SPACE = "don:identity:dvrv-us-1:devo/0:space/17H9CvPAF"
DEFAULT_TAG_ID = "don:core:dvrv-us-1:devo/0:tag/191028"
DEFAULT_STAGE_ID = "don:core:dvrv-us-1:devo/0:custom_stage/16"
DEFAULT_PRIORITY = 3


def auth_header_value(token: str) -> str:
    token = token.strip()
    if not token:
        raise ValueError("empty DevRev token")
    if token.lower().startswith("bearer "):
        return token
    return token


def build_create_issue_payload(
    title: str,
    body: str,
    *,
    applies_to_part: str = DEFAULT_APPLIES_TO_PART,
    owned_by: list[str] | None = None,
    space: str = DEFAULT_SPACE,
    tag_id: str = DEFAULT_TAG_ID,
    stage_id: str = DEFAULT_STAGE_ID,
    priority: int = DEFAULT_PRIORITY,
) -> dict[str, Any]:
    title = title.strip()
    body = body.strip()
    if not title:
        raise ValueError("title is required")
    if not body:
        raise ValueError("body is required")
    if len(title) > 500:
        raise ValueError("title must be at most 500 characters")
    return {
        "custom_fields": {},
        "body": body,
        "title": title,
        "type": "issue",
        "applies_to_part": applies_to_part,
        "owned_by": owned_by if owned_by is not None else list(DEFAULT_OWNED_BY),
        "priority_v2": priority,
        "space": space,
        "tags": [{"id": tag_id}],
        "stage": {"id": stage_id},
        "custom_schema_spec": {
            "tenant_fragment": True,
            "validate_required_fields": True,
        },
    }


def create_issue(
    title: str,
    body: str,
    token: str,
    *,
    api_base: str = DEFAULT_API_BASE,
    applies_to_part: str = DEFAULT_APPLIES_TO_PART,
    owned_by: list[str] | None = None,
    space: str = DEFAULT_SPACE,
    tag_id: str = DEFAULT_TAG_ID,
    stage_id: str = DEFAULT_STAGE_ID,
    priority: int = DEFAULT_PRIORITY,
    request_fn: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Call works.create and return a small summary for the agent."""
    payload = build_create_issue_payload(
        title,
        body,
        applies_to_part=applies_to_part,
        owned_by=owned_by,
        space=space,
        tag_id=tag_id,
        stage_id=stage_id,
        priority=priority,
    )
    if request_fn is None:
        request_fn = _post_json
    raw = request_fn(
        f"{api_base.rstrip('/')}/works.create",
        payload,
        auth_header_value(token),
    )
    work = raw.get("work") if isinstance(raw, dict) else None
    if not isinstance(work, dict):
        return {"raw": raw}
    display_id = work.get("display_id") or work.get("displayId")
    work_id = work.get("id")
    url = work.get("url") or work.get("web_url")
    if not url and display_id:
        url = f"https://app.devrev.ai/{display_id}"
    return {
        "display_id": display_id,
        "id": work_id,
        "url": url,
        "title": work.get("title") or title,
    }


def _post_json(url: str, payload: dict[str, Any], authorization: str) -> dict[str, Any]:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": authorization,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-DevRev-Include-Oasis-Format": "true",
            "User-Agent": "cost-investigator-agent",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:800]
        raise RuntimeError(f"DevRev API {e.code}: {detail}") from e

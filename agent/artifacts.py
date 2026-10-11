"""Upload a workspace file to the dedicated artifact bucket and sign a download URL.

The runtime role signs the URL. It stops working after ARTIFACT_URL_TTL_S, and sooner
if the temporary credentials that signed it expire first.
"""
import re
from pathlib import Path

DEFAULT_BUCKET = "cost-agent-user-artifacts-270149200236"
DEFAULT_PREFIX = "generated"
DEFAULT_URL_TTL_S = 86400
MAX_PRESIGN_TTL_S = 7 * 24 * 60 * 60
DEFAULT_MAX_BYTES = 20 * 1024 * 1024

_SESSION_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_FILENAME_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")

CONTENT_TYPES = {
    ".md": "text/markdown; charset=utf-8",
    ".markdown": "text/markdown; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".json": "application/json",
    ".txt": "text/plain; charset=utf-8",
    ".png": "image/png",
    ".pdf": "application/pdf",
    ".svg": "image/svg+xml",
}


def safe_session_id(session_id: str | None) -> str:
    """Return an S3-safe session path segment."""
    cleaned = _SESSION_UNSAFE.sub("-", (session_id or "").strip())
    while ".." in cleaned:
        cleaned = cleaned.replace("..", ".")
    cleaned = cleaned.strip(".-")
    return (cleaned[:128] or "unknown-session")


def download_filename(name: str) -> str:
    """Return a header-safe attachment filename."""
    safe = _FILENAME_UNSAFE.sub("_", Path(name).name).strip("._")
    return (safe[:200] or "artifact")


def content_type_for(name: str) -> str:
    return CONTENT_TYPES.get(Path(name).suffix.lower(), "application/octet-stream")


def object_key(session_id: str | None, relative_name: str, prefix: str = DEFAULT_PREFIX) -> str:
    """Build the object key. relative_name must already be inside the workspace."""
    path = Path(relative_name)
    if path.is_absolute():
        raise ValueError("artifact path must be relative to the workspace")
    parts: list[str] = []
    for part in path.parts:
        if part in ("", ".", "..") or "/" in part or "\\" in part:
            raise ValueError("artifact path must stay inside the workspace")
        parts.append(part)
    if not parts:
        raise ValueError("artifact path is empty")
    key_prefix = prefix.strip("/")
    session = safe_session_id(session_id)
    key = f"{session}/{'/'.join(parts)}" if not key_prefix else f"{key_prefix}/{session}/{'/'.join(parts)}"
    if len(key.encode()) > 1024:
        raise ValueError("artifact key is too long")
    return key


def workspace_file(workspace: Path, name: str) -> Path:
    """Resolve name inside workspace or raise ValueError."""
    root = workspace.resolve()
    path = (workspace / name).resolve()
    if path != root and root not in path.parents:
        raise ValueError("path must stay inside the workspace")
    if not path.is_file():
        raise FileNotFoundError(name)
    return path


def publish_workspace_file(
    *,
    workspace: Path,
    name: str,
    session_id: str | None,
    s3_client,
    bucket: str = DEFAULT_BUCKET,
    prefix: str = DEFAULT_PREFIX,
    ttl_s: int = DEFAULT_URL_TTL_S,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict:
    """Upload one workspace file and return its presigned GET URL."""
    if not bucket:
        raise ValueError("ARTIFACT_BUCKET is not set")
    path = workspace_file(workspace, name)
    size = path.stat().st_size
    if size > max_bytes:
        raise ValueError(f"artifact is {size} bytes; limit is {max_bytes}")
    relative = path.relative_to(workspace.resolve()).as_posix()
    key = object_key(session_id, relative, prefix=prefix)
    content_type = content_type_for(path.name)
    filename = download_filename(path.name)
    ttl = max(60, min(int(ttl_s), MAX_PRESIGN_TTL_S))
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=path.read_bytes(),
        ContentType=content_type,
        ContentDisposition=f'attachment; filename="{filename}"',
    )
    url = s3_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=ttl,
    )
    return {
        "bucket": bucket,
        "key": key,
        "download_url": url,
        "expires_in_seconds": ttl,
        "content_type": content_type,
        "bytes": size,
        "filename": filename,
    }


def format_expiry(seconds: int | str | None) -> str:
    """Human-readable lifetime for download messages."""
    try:
        ttl = int(seconds)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "unknown time"
    if ttl >= 86400 and ttl % 86400 == 0:
        days = ttl // 86400
        return "1 day" if days == 1 else f"{days} days"
    if ttl >= 3600 and ttl % 3600 == 0:
        hours = ttl // 3600
        return "1 hour" if hours == 1 else f"{hours} hours"
    if ttl >= 60 and ttl % 60 == 0:
        minutes = ttl // 60
        return "1 minute" if minutes == 1 else f"{minutes} minutes"
    return "1 second" if ttl == 1 else f"{ttl} seconds"


def format_download_message(items: list[dict]) -> str:
    """Assistant text listing presigned URLs from this run."""
    if not items:
        return ""
    lines = ["Artifact downloads:"]
    for item in items:
        lines.append(
            f"- {item.get('key', item.get('filename', 'artifact'))}: {item['download_url']} "
            f"(expires in {format_expiry(item.get('expires_in_seconds'))})"
        )
    return "\n".join(lines)

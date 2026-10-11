"""Small in-place edits to workspace files (avoid re-uploading whole files via tools)."""

from pathlib import Path


def resolve_workspace_path(workspace: Path, name: str) -> Path:
    path = (workspace / name).resolve()
    root = workspace.resolve()
    if root not in path.parents and path != root:
        raise ValueError("path must stay inside the workspace")
    return path


def apply_workspace_edit(
    workspace: Path,
    name: str,
    old_string: str,
    new_string: str,
    *,
    replace_all: bool = False,
) -> dict[str, int | str]:
    """Replace old_string with new_string in a workspace file.

    For a new file, pass old_string as empty and new_string as the full content.
    """
    path = resolve_workspace_path(workspace, name)
    if not old_string and not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_string)
        return {"action": "created", "replacements": 1, "bytes": len(new_string.encode())}

    if not path.is_file():
        raise FileNotFoundError(f"{name} does not exist; read it or clone the repo first")

    content = path.read_text()
    count = content.count(old_string)
    if count == 0:
        raise ValueError("old_string was not found in the file (check whitespace and context)")
    if not replace_all and count > 1:
        raise ValueError(
            f"old_string appears {count} times; include more context or set replace_all=true"
        )

    if replace_all:
        updated = content.replace(old_string, new_string)
        replacements = count
    else:
        updated = content.replace(old_string, new_string, 1)
        replacements = 1

    path.write_text(updated)
    return {"action": "updated", "replacements": replacements, "bytes": len(updated.encode())}

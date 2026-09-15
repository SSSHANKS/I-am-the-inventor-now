from __future__ import annotations

import re
import tomllib
from copy import deepcopy
from typing import Any


def normalise_python_metadata(
    content: str, architecture: dict[str, Any], manifest: dict[str, Any]
) -> str:
    """Correct conventional metadata fields using only approved file layout facts."""
    try:
        metadata = tomllib.loads(content)
    except tomllib.TOMLDecodeError:
        return content
    project = metadata.get("project", {})
    planned = {item["path"] for item in manifest["files"]}
    readme = project.get("readme") if isinstance(project, dict) else None
    referenced = readme.get("file") if isinstance(readme, dict) else readme
    if isinstance(referenced, str) and referenced not in planned:
        content = _edit_field(content, ("project",), "readme", None)
    profile = architecture["project_profile"]
    if "setuptools" not in str(profile.get("build_system", "")).casefold():
        return content
    layout = str(profile.get("layout", "")).casefold()
    if layout not in {"src", "flat"}:
        return content
    root = "src" if layout == "src" else "."
    # Only adjust an existing discovery table; explicit package lists and custom
    # mappings need the normal validator/repair path rather than a guessed rewrite.
    content = _edit_field(
        content, ("tool", "setuptools", "packages", "find"), "where", f'["{root}"]'
    )
    return content


def _edit_field(content: str, table: tuple[str, ...], key: str, value: str | None) -> str:
    """Edit simple table syntax only, proving all other parsed values are preserved."""
    original = tomllib.loads(content)
    expected = deepcopy(original)
    cursor = expected
    for part in table:
        cursor = cursor.get(part)
        if not isinstance(cursor, dict):
            return content
    if value is None:
        cursor.pop(key, None)
    else:
        cursor[key] = tomllib.loads(f"value = {value}")["value"]
    header = re.search(
        r"(?m)^\[" + re.escape(".".join(table)) + r"\][ \t]*(?:#[^\n]*)?\r?$",
        content,
    )
    if header is None:
        return content
    next_header = re.search(r"(?m)^\s*\[", content[header.end():])
    end = header.end() + next_header.start() if next_header else len(content)
    body = content[header.end():end]
    pattern = r"(?m)^[ \t]*" + re.escape(key) + r"[ \t]*=[^\n]*(?:\n|$)"
    replacement = f"{key} = {value}\n" if value is not None else ""
    body, count = re.subn(pattern, lambda _: replacement, body, count=1)
    if not count and value is not None:
        body = "\n" + replacement + body
    candidate = content[:header.end()] + body + content[end:]
    try:
        return candidate if tomllib.loads(candidate) == expected else content
    except tomllib.TOMLDecodeError:
        return content

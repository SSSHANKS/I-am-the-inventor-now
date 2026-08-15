"""Index configuration and build files listed as code.

JSON keeps its dedicated handler. Everything else that ingest puts on the code list
because it is a known config name (Dockerfile, Makefile, pyproject.toml, …) lands here
so those files contribute catalogue entries instead of `files_skipped`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from packages.modules.indexing.models import evidence
from packages.modules.skills.reading import Reader

_TOML_KEY = re.compile(r"^\s*([A-Za-z_][\w-]*)\s*=")
_TOML_TABLE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
_MAKE_TARGET = re.compile(r"^([A-Za-z_][\w-]*)\s*:")
_DOCKER_INSTRUCTION = re.compile(
    r"^\s*(FROM|RUN|CMD|ENTRYPOINT|ENV|ARG|COPY|ADD|WORKDIR|EXPOSE|USER|VOLUME)\b",
    re.IGNORECASE,
)


def index_config_file(relative_path: str, result: dict[str, Any], source_reader: Reader) -> None:
    content = source_reader.read_file(relative_path)
    lines = content.splitlines()
    name = Path(relative_path).name
    suffix = Path(relative_path).suffix.lower()
    kind = _config_kind(name, suffix)

    result["files_indexed"].append(relative_path)
    result["configs"].append(
        {
            "file": relative_path,
            "kind": kind,
            "line_count": len(lines),
            "top_level_type": "text",
            "top_level_keys": _top_level_keys(kind, lines),
            "evidence": evidence(relative_path, lines, 1, min(len(lines), 1) or 1),
        }
    )
    result["analysis_targets"].append(
        {
            "file": relative_path,
            "target": relative_path,
            "target_type": "config",
            "reason": f"{kind} configuration file listed as code",
            "evidence": evidence(relative_path, lines, 1, min(len(lines), 1) or 1),
        }
    )


def _config_kind(name: str, suffix: str) -> str:
    lowered = name.lower()
    if lowered == "dockerfile":
        return "dockerfile"
    if lowered == "makefile":
        return "makefile"
    if lowered == "requirements.txt":
        return "requirements"
    if suffix == ".toml":
        return "toml"
    if suffix == ".txt":
        return "text_config"
    return suffix.lstrip(".") or "config"


def _top_level_keys(kind: str, lines: list[str]) -> list[str]:
    keys: list[str] = []
    if kind == "toml":
        for line in lines:
            table = _TOML_TABLE.match(line)
            if table:
                keys.append(table.group(1).strip())
                continue
            match = _TOML_KEY.match(line)
            if match:
                keys.append(match.group(1))
    elif kind == "makefile":
        for line in lines:
            match = _MAKE_TARGET.match(line)
            if match and not match.group(1).startswith("."):
                keys.append(match.group(1))
    elif kind == "dockerfile":
        for line in lines:
            match = _DOCKER_INSTRUCTION.match(line)
            if match:
                keys.append(match.group(1).upper())
    elif kind == "requirements":
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                package = re.split(r"[<>=\s;]", stripped, maxsplit=1)[0]
                if package:
                    keys.append(package)
    # Preserve order, drop duplicates.
    seen: set[str] = set()
    ordered: list[str] = []
    for key in keys:
        if key not in seen:
            seen.add(key)
            ordered.append(key)
    return ordered

"""Index Jupyter notebooks by analysing each code cell.

Evidence points at real line ranges inside the `.ipynb` file so later stages can
pre-read the same bytes the indexer saw. Cell-local parses are remapped onto those
spans; exact intra-cell line fidelity is secondary to having a citable location.
"""

from __future__ import annotations

import json
import re
from typing import Any

from packages.modules.indexing.models import evidence
from packages.modules.indexing.python_file import index_python_source
from packages.modules.indexing.source_file import (
    PROFILE_BY_SUFFIX,
    LanguageProfile,
    index_text_as_language,
)
from packages.modules.skills.reading import Reader

_PYTHON_KERNEL = re.compile(r"python", re.IGNORECASE)


def index_notebook_file(relative_path: str, result: dict[str, Any], source_reader: Reader) -> None:
    raw = source_reader.read_file(relative_path)
    raw_lines = raw.splitlines()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Notebook is not valid JSON: {exc}") from exc

    if relative_path not in result["files_indexed"]:
        result["files_indexed"].append(relative_path)

    result["configs"].append(
        {
            "file": relative_path,
            "kind": "ipynb",
            "line_count": len(raw_lines),
            "top_level_type": type(payload).__name__,
            "top_level_keys": list(payload.keys()) if isinstance(payload, dict) else [],
            "evidence": evidence(relative_path, raw_lines, 1),
        }
    )

    default_profile = _default_profile(payload)
    cells = payload.get("cells") if isinstance(payload, dict) else None
    if not isinstance(cells, list):
        return

    before = _snapshot_counts(result)
    for cell in cells:
        if not isinstance(cell, dict) or cell.get("cell_type") != "code":
            continue
        source = cell.get("source", [])
        if isinstance(source, str):
            cell_lines = source.splitlines()
            cell_text = source
        elif isinstance(source, list):
            cell_lines = [line.rstrip("\n") for line in source if isinstance(line, str)]
            cell_text = "".join(source)
        else:
            continue
        if not cell_text.strip():
            continue

        span = _locate_cell_span(raw_lines, cell_lines)
        if span is None:
            continue
        span_start, span_end = span

        language = None
        metadata = cell.get("metadata")
        if isinstance(metadata, dict):
            vscode = metadata.get("vscode")
            language = metadata.get("language")
            if language is None and isinstance(vscode, dict):
                language = vscode.get("languageId")

        profile = _profile_for_cell(language, default_profile)
        cell_result: dict[str, Any] = {
            "files_indexed": [],
            "imports": [],
            "classes": [],
            "functions": [],
            "entrypoints": [],
            "calls": [],
            "analysis_targets": [],
            "configs": [],
            "errors": [],
        }

        if profile is None:
            # Python: real AST over the cell source, then remap onto the notebook span.
            index_python_source(relative_path, cell_result, cell_text, cell_lines)
        else:
            index_text_as_language(
                relative_path,
                cell_result,
                cell_text,
                cell_lines,
                profile,
                record_file=False,
            )

        _remap_and_merge(result, cell_result, relative_path, raw_lines, span_start, span_end)

    # Always keep a file-level analysis target so empty-code notebooks still surface.
    if _snapshot_counts(result) == before:
        result["analysis_targets"].append(
            {
                "file": relative_path,
                "target": relative_path,
                "target_type": "file",
                "reason": "notebook listed as code",
                "evidence": evidence(relative_path, raw_lines, 1, min(len(raw_lines), 1)),
            }
        )


def _default_profile(payload: dict[str, Any]) -> LanguageProfile | None:
    """None means Python (AST). Otherwise a regex profile."""
    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    if not isinstance(metadata, dict):
        return None
    kernelspec = metadata.get("kernelspec") if isinstance(metadata.get("kernelspec"), dict) else {}
    language_info = (
        metadata.get("language_info") if isinstance(metadata.get("language_info"), dict) else {}
    )
    name = str(kernelspec.get("language") or language_info.get("name") or "python")
    return _profile_for_cell(name, None)


def _profile_for_cell(
    language: str | None, default: LanguageProfile | None
) -> LanguageProfile | None:
    """Return a regex profile, or None to signal Python AST indexing."""
    if language is None:
        return default
    normalized = language.strip().lower()
    if not normalized or _PYTHON_KERNEL.search(normalized):
        return None
    if normalized in {"javascript", "js", "node"}:
        return PROFILE_BY_SUFFIX[".js"]
    if normalized in {"typescript", "ts"}:
        return PROFILE_BY_SUFFIX[".ts"]
    if normalized == "java":
        return PROFILE_BY_SUFFIX[".java"]
    if normalized in {"c", "cpp", "c++", "cxx"}:
        return PROFILE_BY_SUFFIX[".cpp"]
    return default


def _locate_cell_span(raw_lines: list[str], cell_lines: list[str]) -> tuple[int, int] | None:
    """Find the inclusive 1-based line span of a cell's source inside the raw notebook."""
    needles = [line.strip() for line in cell_lines if line.strip()]
    if not needles:
        return None

    first = needles[0]
    # JSON encodes the source line as a quoted string; search for the bare text.
    start = None
    for index, line in enumerate(raw_lines, start=1):
        if first in line:
            start = index
            break
    if start is None:
        return None

    last = needles[-1]
    end = start
    for index, line in enumerate(raw_lines[start - 1 :], start=start):
        if last in line:
            end = index
    return start, max(start, end)


def _remap_and_merge(
    result: dict[str, Any],
    cell_result: dict[str, Any],
    relative_path: str,
    raw_lines: list[str],
    span_start: int,
    span_end: int,
) -> None:
    cell_evidence = evidence(relative_path, raw_lines, span_start, span_end)
    for collection in ("imports", "classes", "functions", "entrypoints", "calls", "analysis_targets"):
        for item in cell_result.get(collection) or []:
            remapped = dict(item)
            remapped["file"] = relative_path
            remapped["line_start"] = span_start
            remapped["line_end"] = span_end
            remapped["evidence"] = cell_evidence
            result[collection].append(remapped)


def _snapshot_counts(result: dict[str, Any]) -> tuple[int, int, int]:
    return (
        len(result.get("classes") or []),
        len(result.get("functions") or []),
        len(result.get("analysis_targets") or []),
    )

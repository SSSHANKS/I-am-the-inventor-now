"""Compact index views for prompts.

A full index is far too large to paste into a prompt, so each collection is trimmed
to the fields an agent actually reasons over and capped at `limit` entries.
"""

from typing import Any

DEFAULT_AGENT_CONTEXT_LIMIT = 80

CODE_FIELDS: dict[str, list[str]] = {
    "entrypoints": ["file", "kind", "line_start", "line_end", "evidence"],
    "analysis_targets": ["file", "target", "target_type", "reason", "evidence"],
    "classes": [
        "file",
        "name",
        "qualified_name",
        "line_start",
        "line_end",
        "methods",
        "evidence",
    ],
    "functions": [
        "file",
        "qualified_name",
        "owner",
        "args",
        "line_start",
        "line_end",
        "evidence",
    ],
    "imports": [
        "file",
        "kind",
        "module",
        "name",
        "alias",
        "line_start",
        "line_end",
        "evidence",
    ],
    "configs": [
        "file",
        "kind",
        "line_count",
        "top_level_type",
        "top_level_keys",
        "evidence",
    ],
}

DOC_FIELDS: dict[str, list[str]] = {
    "headings": ["file", "level", "title", "line_start", "line_end", "evidence"],
    "sections": ["file", "title", "level", "line_start", "line_end", "evidence"],
    "commands": ["file", "command", "line_start", "line_end", "evidence"],
    "code_blocks": ["file", "language", "line_start", "line_end", "evidence"],
    "links": ["file", "text", "url", "line_start", "line_end", "evidence"],
    "list_items": ["file", "ordinal", "text", "line_start", "line_end", "evidence"],
    "references": ["file", "kind", "value", "line_start", "line_end", "evidence"],
}

PASSTHROUGH = ("repo_url", "branch", "commit_hash", "files_indexed", "files_skipped", "errors")


def build_source_code_index_context(
    source_code_index: dict[str, Any],
    limit: int = DEFAULT_AGENT_CONTEXT_LIMIT,
) -> dict[str, Any]:
    return _build(source_code_index, CODE_FIELDS, limit)


def build_source_doc_index_context(
    source_doc_index: dict[str, Any],
    limit: int = DEFAULT_AGENT_CONTEXT_LIMIT,
) -> dict[str, Any]:
    return _build(source_doc_index, DOC_FIELDS, limit)


def _build(
    index: dict[str, Any],
    field_map: dict[str, list[str]],
    limit: int,
) -> dict[str, Any]:
    context: dict[str, Any] = {key: index.get(key) for key in PASSTHROUGH}
    for collection, fields in field_map.items():
        context[collection] = [
            {field: item[field] for field in fields if field in item}
            for item in (index.get(collection) or [])[:limit]
        ]
    return context

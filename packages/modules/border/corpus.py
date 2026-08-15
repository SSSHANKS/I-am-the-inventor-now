"""Dirty-side excerpt corpus used by content-leak scanning.

Excerpts never cross the boundary - they exist only so Border (and Dirty's advisory
pass) can tell whether finished prose copied a source line word-for-word.
"""

from __future__ import annotations

import json
from typing import Any


def evidence_excerpts(*reports: str | dict[str, Any] | None) -> tuple[str, ...]:
    """Every verbatim source line the dirty-side reports carry.

    Only excerpts with a real source location count. A `missing` finding has no file to
    quote, so the agent's own English is stored as its excerpt; feeding that back in
    flags the specification for restating our own words.
    """
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            excerpt = node.get("excerpt")
            if isinstance(excerpt, str) and excerpt.strip() and node.get("file"):
                found.append(excerpt)
            for key, value in node.items():
                if key != "excerpt":
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for report in reports:
        if isinstance(report, str):
            try:
                report = json.loads(report)
            except (TypeError, ValueError):
                continue
        walk(report)
    return tuple(found)

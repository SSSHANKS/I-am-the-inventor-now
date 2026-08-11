"""Index shapes and the shared evidence helper.

An index is a compact map of a snapshot: where things are, and a short excerpt
proving it. Indexes are DIRTY-SIDE artifacts - the excerpts are verbatim original
text, which is allowed here and forbidden in anything that crosses to the clean team
(CLAUDE.md section 2).
"""

from dataclasses import asdict, dataclass, field
from typing import Any


class IndexingError(Exception):
    """Raised when a snapshot cannot be indexed."""


@dataclass
class SourceCodeIndex:
    repo_url: str | None = None
    branch: str | None = None
    commit_hash: str | None = None
    files_indexed: list[str] = field(default_factory=list)
    files_skipped: list[dict[str, Any]] = field(default_factory=list)
    imports: list[dict[str, Any]] = field(default_factory=list)
    classes: list[dict[str, Any]] = field(default_factory=list)
    functions: list[dict[str, Any]] = field(default_factory=list)
    entrypoints: list[dict[str, Any]] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    configs: list[dict[str, Any]] = field(default_factory=list)
    analysis_targets: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SourceDocIndex:
    repo_url: str | None = None
    branch: str | None = None
    commit_hash: str | None = None
    files_indexed: list[dict[str, Any]] = field(default_factory=list)
    files_skipped: list[dict[str, Any]] = field(default_factory=list)
    sections: list[dict[str, Any]] = field(default_factory=list)
    headings: list[dict[str, Any]] = field(default_factory=list)
    commands: list[dict[str, Any]] = field(default_factory=list)
    code_blocks: list[dict[str, Any]] = field(default_factory=list)
    links: list[dict[str, Any]] = field(default_factory=list)
    list_items: list[dict[str, Any]] = field(default_factory=list)
    references: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def evidence(
    relative_path: str,
    lines: list[str],
    line_start: int,
    line_end: int | None = None,
) -> dict[str, Any]:
    """Locate a claim: file, 1-based inclusive line range, and the text itself.

    One helper for code, config and documentation indexing - the legacy tree carried
    three near-identical copies that disagreed on how to clamp out-of-range lines.
    """
    if line_end is None:
        line_end = line_start

    if not lines:
        return {"file": relative_path, "line_start": 0, "line_end": 0, "excerpt": ""}

    start = max(1, min(line_start, len(lines)))
    end = max(start, min(line_end, len(lines)))
    return {
        "file": relative_path,
        "line_start": start,
        "line_end": end,
        "excerpt": "\n".join(lines[start - 1 : end]),
    }

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from packages.modules.clean.workspace import WorkspaceError

REQUIREMENT_PATTERN = re.compile(r"\b(?:FR|BR|EH|AC|TC)-\d{3,}\b")
_HEADING_PATTERN = re.compile(r"^(?P<marks>#{1,6})\s+")

MAX_SCOPED_SPECIFICATION_BYTES = 128 * 1024
MAX_SCOPED_PLAN_BYTES = 256 * 1024


@dataclass(frozen=True)
class ScopedCleanContext:
    specification: str
    plan: dict[str, Any]
    requirement_ids: tuple[str, ...]
    symbol_ids: tuple[str, ...]

    @property
    def specification_bytes(self) -> int:
        return len(self.specification.encode("utf-8"))

    @property
    def plan_bytes(self) -> int:
        return len(_render_json(self.plan).encode("utf-8"))

    def audit_record(self) -> dict[str, Any]:
        return {
            "requirement_ids": list(self.requirement_ids),
            "symbol_ids": list(self.symbol_ids),
            "specification_bytes": self.specification_bytes,
            "plan_bytes": self.plan_bytes,
        }


def build_scoped_context(
    specification: str,
    plan: dict[str, Any],
    paths: Iterable[str],
) -> ScopedCleanContext:
    """Build deterministic specification and plan context for selected file tasks."""
    selected_paths = set(paths)
    selected_files = [
        item for item in plan["files"] if item["path"] in selected_paths
    ]
    requirement_ids = tuple(
        sorted(
            {
                requirement_id
                for item in selected_files
                for requirement_id in item["requirement_ids"]
            }
        )
    )
    symbol_ids = tuple(
        sorted(
            {
                symbol_id
                for item in selected_files
                for field in ("provides", "requires")
                for symbol_id in item[field]
            }
        )
    )
    scoped_specification = select_specification_context(
        specification,
        set(requirement_ids),
    )
    scoped_plan = {
        "schema_version": plan["schema_version"],
        "summary": plan["summary"],
        "project_kind": plan["project_kind"],
        "runtime": plan["runtime"],
        "dependencies": plan["dependencies"],
        "packages": plan["packages"],
        "entry_points": [
            item for item in plan["entry_points"] if item["path"] in selected_paths
        ],
        "symbol_contracts": [
            item for item in plan["symbol_contracts"] if item["symbol_id"] in symbol_ids
        ],
        "files": selected_files,
        "validation_strategy": plan["validation_strategy"],
        "open_questions": _relevant_open_questions(
            plan["open_questions"],
            set(requirement_ids),
        ),
    }
    context = ScopedCleanContext(
        specification=scoped_specification,
        plan=scoped_plan,
        requirement_ids=requirement_ids,
        symbol_ids=symbol_ids,
    )
    if context.specification_bytes > MAX_SCOPED_SPECIFICATION_BYTES:
        raise WorkspaceError(
            "Scoped specification context exceeds the 128 KiB prompt limit"
        )
    if context.plan_bytes > MAX_SCOPED_PLAN_BYTES:
        raise WorkspaceError("Scoped Clean plan exceeds the 256 KiB prompt limit")
    return context


def select_specification_context(specification: str, requirement_ids: set[str]) -> str:
    """Select Markdown blocks containing the requested requirement identifiers."""
    if not requirement_ids:
        return "# Scoped specification context\n\nNo requirement IDs are assigned to this task.\n"

    lines = specification.splitlines(keepends=True)
    selected: set[int] = set()
    heading_stack: dict[int, int] = {}
    for index, line in enumerate(lines):
        heading = _HEADING_PATTERN.match(line)
        if heading:
            level = len(heading.group("marks"))
            heading_stack = {
                existing_level: heading_index
                for existing_level, heading_index in heading_stack.items()
                if existing_level < level
            }
            heading_stack[level] = index
        if not (set(REQUIREMENT_PATTERN.findall(line)) & requirement_ids):
            continue
        selected.update(heading_stack.values())
        selected.update(_requirement_block(lines, index))

    if not selected:
        raise WorkspaceError(
            "Scoped specification context could not find assigned requirement IDs: "
            + ", ".join(sorted(requirement_ids))
        )
    body = _render_selected_lines(lines, selected)
    identifiers = ", ".join(sorted(requirement_ids))
    return f"# Scoped specification context\n\nRequirement IDs: {identifiers}\n\n{body}"


def _requirement_block(lines: list[str], anchor: int) -> set[int]:
    selected = {anchor}
    anchor_heading = _HEADING_PATTERN.match(lines[anchor])
    anchor_level = len(anchor_heading.group("marks")) if anchor_heading else None
    for index in range(anchor + 1, len(lines)):
        line = lines[index]
        heading = _HEADING_PATTERN.match(line)
        if heading and (
            anchor_level is None or len(heading.group("marks")) <= anchor_level
        ):
            break
        if REQUIREMENT_PATTERN.search(line):
            break
        selected.add(index)
    return selected


def _render_selected_lines(lines: list[str], selected: set[int]) -> str:
    rendered: list[str] = []
    previous: int | None = None
    for index in sorted(selected):
        if previous is not None and index != previous + 1:
            rendered.append("\n[unrelated specification content omitted]\n\n")
        rendered.append(lines[index])
        previous = index
    return "".join(rendered).strip() + "\n"


def _relevant_open_questions(
    questions: list[str],
    requirement_ids: set[str],
) -> list[str]:
    return [
        question
        for question in questions
        if not REQUIREMENT_PATTERN.search(question)
        or bool(set(REQUIREMENT_PATTERN.findall(question)) & requirement_ids)
    ]


def _render_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)

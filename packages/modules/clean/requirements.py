from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

REQUIREMENT_ID_PATTERN = re.compile(r"\b(?:FR|BR|EH|AC|TC)-\d{3,}\b")
_HEADING_PATTERN = re.compile(r"^(?P<marks>#{1,6})\s+(?P<title>.+?)\s*$")
_LIST_ITEM_PATTERN = re.compile(r"^(?P<prefix>\s{0,3}[-*+]\s+)(?P<body>\S.*)$")


class RequirementCatalogueError(ValueError):
    """The approved specification cannot support deterministic traceability."""


@dataclass(frozen=True)
class RequirementLabel:
    requirement_id: str
    category: str
    source_line: int
    section: str

    def to_dict(self) -> dict[str, str | int]:
        return {
            "requirement_id": self.requirement_id,
            "category": self.category,
            "source_line": self.source_line,
            "section": self.section,
        }


@dataclass(frozen=True)
class PreparedSpecification:
    text: str
    requirement_ids: tuple[str, ...]
    generated_labels: tuple[RequirementLabel, ...]
    source_sha256: str

    def audit_record(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "source_specification_sha256": self.source_sha256,
            "requirement_ids": list(self.requirement_ids),
            "requirement_statements": [
                {
                    "requirement_id": requirement_id,
                    "statement": statement,
                }
                for requirement_id, statement in requirement_statements(self.text).items()
            ],
            "generated_labels": [label.to_dict() for label in self.generated_labels],
        }


def requirement_statements(specification: str) -> dict[str, str]:
    """Return the authoritative source line for every labeled requirement."""
    statements: dict[str, str] = {}
    for line in specification.splitlines():
        identifiers = REQUIREMENT_ID_PATTERN.findall(line)
        if not identifiers:
            continue
        statement = line.strip()
        match = _LIST_ITEM_PATTERN.match(statement)
        if match is not None:
            statement = match.group("body")
        for requirement_id in identifiers:
            statements.setdefault(requirement_id, statement)
    return statements


@dataclass(frozen=True)
class _LineContext:
    category: str | None
    section: str


def prepare_specification(specification: str) -> PreparedSpecification:
    """Add stable traceability labels to unlabeled requirement statements.

    The approved text is not rewritten semantically. Labels are inserted only before
    list items in recognized requirement sections. If a category contains no list,
    paragraph starts in that section are labeled as a conservative fallback.
    """
    if not isinstance(specification, str) or not specification.strip():
        raise RequirementCatalogueError("Approved specification must be non-empty text")

    lines = specification.splitlines(keepends=True)
    contexts = _line_contexts(lines)
    categories_with_primary_lists = {
        context.category
        for line, context in zip(lines, contexts, strict=True)
        if context.category is not None
        and _LIST_ITEM_PATTERN.match(line.rstrip("\r\n"))
        and not _is_precondition_postcondition_elaboration(context.section)
    }
    used_ids = set(REQUIREMENT_ID_PATTERN.findall(specification))
    counters = _initial_counters(used_ids)
    generated: list[RequirementLabel] = []
    rendered: list[str] = []

    for index, (line, context) in enumerate(zip(lines, contexts, strict=True)):
        category = context.category
        bare_line = line.rstrip("\r\n")
        ending = line[len(bare_line) :]
        list_match = _LIST_ITEM_PATTERN.match(bare_line)
        should_label_list = (
            category is not None
            and list_match is not None
            and (
                not _is_precondition_postcondition_elaboration(context.section)
                or category not in categories_with_primary_lists
            )
        )
        should_label_paragraph = (
            category is not None
            and category not in categories_with_primary_lists
            and _is_paragraph_start(lines, index)
        )
        if (
            (should_label_list or should_label_paragraph)
            and not REQUIREMENT_ID_PATTERN.search(bare_line)
        ):
            requirement_id = _next_id(category, counters, used_ids)
            if list_match is not None:
                bare_line = (
                    f"{list_match.group('prefix')}{requirement_id}: "
                    f"{list_match.group('body')}"
                )
            else:
                bare_line = f"{requirement_id}: {bare_line}"
            generated.append(
                RequirementLabel(
                    requirement_id=requirement_id,
                    category=category,
                    source_line=index + 1,
                    section=context.section,
                )
            )
        rendered.append(bare_line + ending)

    prepared_text = "".join(rendered)
    requirement_ids = tuple(sorted(set(REQUIREMENT_ID_PATTERN.findall(prepared_text))))
    if not requirement_ids:
        raise RequirementCatalogueError(
            "Approved specification has no formal requirement IDs or identifiable "
            "requirement statements under standard Markdown sections"
        )
    return PreparedSpecification(
        text=prepared_text,
        requirement_ids=requirement_ids,
        generated_labels=tuple(generated),
        source_sha256=hashlib.sha256(specification.encode("utf-8")).hexdigest(),
    )


def _line_contexts(lines: list[str]) -> list[_LineContext]:
    headings: list[tuple[int, str, str | None]] = []
    contexts: list[_LineContext] = []
    for line in lines:
        match = _HEADING_PATTERN.match(line.rstrip("\r\n"))
        if match:
            level = len(match.group("marks"))
            title = match.group("title").strip()
            headings = [heading for heading in headings if heading[0] < level]
            inherited = headings[-1][2] if headings else None
            category = (
                None
                if _is_non_requirement_heading(title)
                else _category_for_heading(title) or inherited
            )
            headings.append((level, title, category))
        category = headings[-1][2] if headings else None
        section = " / ".join(heading[1] for heading in headings)
        contexts.append(_LineContext(category=category, section=section))
    return contexts


def _category_for_heading(title: str) -> str | None:
    normalized = re.sub(r"[^a-z]+", " ", title.casefold()).strip()
    if "functional requirement" in normalized:
        return "FR"
    if "behavioral requirement" in normalized or "behavioural requirement" in normalized:
        return "BR"
    if "error handling" in normalized or "error requirement" in normalized:
        return "EH"
    if "acceptance criteria" in normalized or "acceptance criterion" in normalized:
        return "AC"
    if (
        "test candidate" in normalized
        or "test case" in normalized
        or "testing requirement" in normalized
    ):
        return "TC"
    return None


def _is_non_requirement_heading(title: str) -> bool:
    """Keep supporting catalogues and unresolved gaps out of the requirement IR.

    These headings often contain category phrases such as ``Error Handling`` while
    describing evidence *about* that category.  They also occur beneath requirement
    headings, so they must explicitly reset rather than inherit the parent category.
    """
    normalized = re.sub(r"[^a-z]+", " ", title.casefold()).strip()
    return (
        "evidence reference" in normalized
        or "evidence catalogue" in normalized
        or "evidence catalog" in normalized
        or normalized.startswith("gap ")
        or "open question" in normalized
        or normalized in {"gaps", "gaps and open questions"}
    )


def _is_precondition_postcondition_elaboration(section: str) -> bool:
    normalized = re.sub(r"[^a-z]+", " ", section.casefold()).strip()
    return "precondition" in normalized and "postcondition" in normalized


def _initial_counters(requirement_ids: set[str]) -> dict[str, int]:
    counters = {category: 0 for category in ("FR", "BR", "EH", "AC", "TC")}
    for requirement_id in requirement_ids:
        category, number = requirement_id.split("-", 1)
        counters[category] = max(counters[category], int(number))
    return counters


def _next_id(category: str, counters: dict[str, int], used_ids: set[str]) -> str:
    while True:
        counters[category] += 1
        requirement_id = f"{category}-{counters[category]:03d}"
        if requirement_id not in used_ids:
            used_ids.add(requirement_id)
            return requirement_id


def _is_paragraph_start(lines: list[str], index: int) -> bool:
    stripped = lines[index].strip()
    if not stripped or stripped.startswith(("#", "```", "~~~")):
        return False
    if _LIST_ITEM_PATTERN.match(lines[index].rstrip("\r\n")):
        return False
    if index == 0:
        return True
    previous = lines[index - 1].strip()
    return not previous or previous.startswith("#")

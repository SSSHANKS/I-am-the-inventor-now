import re
from pathlib import Path
from typing import Any

from packages.modules.indexing.models import evidence
from packages.modules.skills.reading import Reader


def index_document_file(relative_path: str, result: dict[str, Any], reader: Reader) -> None:
    content = reader.read_file(relative_path)
    lines = content.splitlines()

    result["files_indexed"].append(
        {
            "file": relative_path,
            "line_count": len(lines),
            "kind": _doc_kind(relative_path),
            "evidence": evidence(relative_path, lines, 1, min(len(lines), 1)),
        }
    )
    result["headings"].extend(_extract_doc_headings(relative_path, lines))
    result["sections"].extend(_extract_doc_sections(relative_path, lines))
    result["code_blocks"].extend(_extract_code_blocks(relative_path, lines))
    result["commands"].extend(_extract_commands(relative_path, lines))
    result["links"].extend(_extract_links(relative_path, lines))
    result["list_items"].extend(_extract_list_items(relative_path, lines))
    result["references"].extend(_extract_references(relative_path, lines))


def _extract_doc_headings(relative_path: str, lines: list[str]) -> list[dict[str, Any]]:
    headings = []

    for line_number, line in enumerate(lines, start=1):
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if not match:
            continue
        headings.append(
            {
                "file": relative_path,
                "level": len(match.group(1)),
                "title": match.group(2).strip(),
                "line_start": line_number,
                "line_end": line_number,
                "evidence": evidence(relative_path, lines, line_number),
            }
        )

    return headings


def _extract_doc_sections(relative_path: str, lines: list[str]) -> list[dict[str, Any]]:
    headings = _extract_doc_headings(relative_path, lines)
    sections = []

    if not headings and lines:
        sections.append(
            {
                "file": relative_path,
                "title": "<document>",
                "level": 0,
                "line_start": 1,
                "line_end": len(lines),
                "evidence": evidence(relative_path, lines, 1, min(len(lines), 3)),
            }
        )
        return sections

    for index, heading in enumerate(headings):
        next_start = (
            headings[index + 1]["line_start"] if index + 1 < len(headings) else len(lines) + 1
        )
        section_start = heading["line_start"]
        section_end = max(section_start, next_start - 1)
        sections.append(
            {
                "file": relative_path,
                "title": heading["title"],
                "level": heading["level"],
                "line_start": section_start,
                "line_end": section_end,
                "evidence": evidence(relative_path, lines, section_start),
            }
        )

    return sections


def _extract_code_blocks(relative_path: str, lines: list[str]) -> list[dict[str, Any]]:
    blocks = []
    in_block = False
    fence = ""
    language = ""
    start_line = 0

    for line_number, line in enumerate(lines, start=1):
        match = re.match(r"^\s*(```+|~~~+)\s*([A-Za-z0-9_-]*)\s*$", line)
        if not match:
            continue

        if not in_block:
            in_block = True
            fence = match.group(1)
            language = match.group(2)
            start_line = line_number
            continue

        if line.strip().startswith(fence):
            blocks.append(
                {
                    "file": relative_path,
                    "language": language,
                    "line_start": start_line,
                    "line_end": line_number,
                    "evidence": evidence(relative_path, lines, start_line, line_number),
                }
            )
            in_block = False
            fence = ""
            language = ""
            start_line = 0

    if in_block:
        blocks.append(
            {
                "file": relative_path,
                "language": language,
                "line_start": start_line,
                "line_end": len(lines),
                "evidence": evidence(relative_path, lines, start_line, len(lines)),
            }
        )

    return blocks


def _extract_commands(relative_path: str, lines: list[str]) -> list[dict[str, Any]]:
    commands = []
    command_pattern = re.compile(
        r"^\s*(?:\$|>)?\s*(python|pip|pytest|uv|git|npm|yarn|pnpm|docker|make|poetry|ruff|mypy)\b",
        re.IGNORECASE,
    )

    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        command_text = stripped[2:].strip() if stripped.startswith(("$ ", "> ")) else stripped

        if command_pattern.match(stripped):
            commands.append(
                {
                    "file": relative_path,
                    "command": command_text,
                    "line_start": line_number,
                    "line_end": line_number,
                    "evidence": evidence(relative_path, lines, line_number),
                }
            )

        for inline in re.findall(r"`([^`]+)`", line):
            if command_pattern.match(inline.strip()):
                commands.append(
                    {
                        "file": relative_path,
                        "command": inline.strip(),
                        "line_start": line_number,
                        "line_end": line_number,
                        "evidence": evidence(relative_path, lines, line_number),
                    }
                )

    return commands


def _extract_links(relative_path: str, lines: list[str]) -> list[dict[str, Any]]:
    links = []
    markdown_link = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
    raw_url = re.compile(r"https?://[^\s)>]+")

    for line_number, line in enumerate(lines, start=1):
        for text, url in markdown_link.findall(line):
            links.append(
                {
                    "file": relative_path,
                    "text": text,
                    "url": url,
                    "line_start": line_number,
                    "line_end": line_number,
                    "evidence": evidence(relative_path, lines, line_number),
                }
            )

        for url in raw_url.findall(line):
            if any(item["url"] == url and item["line_start"] == line_number for item in links):
                continue
            links.append(
                {
                    "file": relative_path,
                    "text": "",
                    "url": url,
                    "line_start": line_number,
                    "line_end": line_number,
                    "evidence": evidence(relative_path, lines, line_number),
                }
            )

    return links


def _extract_list_items(relative_path: str, lines: list[str]) -> list[dict[str, Any]]:
    items = []
    list_pattern = re.compile(r"^\s*(?:(\d+)\.|\-|\*)\s+(.+?)\s*$")

    for line_number, line in enumerate(lines, start=1):
        match = list_pattern.match(line)
        if not match:
            continue
        items.append(
            {
                "file": relative_path,
                "ordinal": int(match.group(1)) if match.group(1) else None,
                "text": match.group(2).strip(),
                "line_start": line_number,
                "line_end": line_number,
                "evidence": evidence(relative_path, lines, line_number),
            }
        )

    return items


def _extract_references(relative_path: str, lines: list[str]) -> list[dict[str, Any]]:
    references = []
    import_pattern = re.compile(
        r"\bfrom\s+([A-Za-z_][\w.]*)\s+import\s+([A-Za-z_][\w]*(?:\s*,\s*[A-Za-z_][\w]*)*)"
    )
    dotted_pattern = re.compile(r"\b[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)+\b")

    for line_number, line in enumerate(lines, start=1):
        for module, names in import_pattern.findall(line):
            references.append(
                {
                    "file": relative_path,
                    "kind": "python_import",
                    "value": f"from {module} import {names}",
                    "line_start": line_number,
                    "line_end": line_number,
                    "evidence": evidence(relative_path, lines, line_number),
                }
            )

        for value in dotted_pattern.findall(line):
            if value.startswith(("http.", "https.", "www.")):
                continue
            references.append(
                {
                    "file": relative_path,
                    "kind": "dotted_reference",
                    "value": value,
                    "line_start": line_number,
                    "line_end": line_number,
                    "evidence": evidence(relative_path, lines, line_number),
                }
            )

    return references


def _doc_kind(relative_path: str) -> str:
    name = Path(relative_path).name.lower()
    if name.startswith("readme"):
        return "readme"
    return Path(relative_path).suffix.lower().lstrip(".") or "text"

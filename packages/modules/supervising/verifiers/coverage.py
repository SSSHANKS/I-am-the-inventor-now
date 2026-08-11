import json
from typing import Any

# Output fields whose schema forces every entry to label="missing" (e.g. open_questions).
# Coverage min_items check is meaningless for these — the agent CAN'T produce a
# documented/inferred entry there.
_MISSING_ONLY_FIELDS: frozenset[str] = frozenset({"open_questions"})


class CoverageVerifier:
    def verify_documentation(
        self,
        artifact: str | dict[str, Any],
        source_doc_index: dict[str, Any],
        plan: str | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = _load_mapping(artifact)
        mini_tasks = _iter_mini_tasks(plan) if plan is not None else []
        issues: list[dict[str, Any]] = []

        issues.extend(_verify_doc_artifact_not_empty(payload, source_doc_index))
        issues.extend(_verify_doc_required_fields(payload, source_doc_index))

        if mini_tasks:
            issues.extend(_verify_mini_task_outputs(payload, mini_tasks))

        return {
            "valid": not any(issue["severity"] == "error" for issue in issues),
            "checked_item_count": _count_findings(payload),
            "issues": issues,
        }

    def verify_plan(
        self,
        plan: str | dict[str, Any],
        stage: str,
        source_doc_index: dict[str, Any] | None = None,
        source_code_index: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from packages.modules.supervising.verifiers.planning import PlanVerifier

        return PlanVerifier().verify(
            plan=plan,
            stage=stage,
            source_doc_index=source_doc_index,
            source_code_index=source_code_index,
        )


def _verify_doc_artifact_not_empty(
    payload: dict[str, Any],
    source_doc_index: dict[str, Any],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    has_doc_content = bool(
        source_doc_index.get("sections")
        or source_doc_index.get("commands")
        or source_doc_index.get("code_blocks")
        or source_doc_index.get("references")
    )
    if not has_doc_content:
        return issues

    documented_count = _count_by_label(payload, "documented")
    missing_count = _count_by_label(payload, "missing")
    total_count = _count_findings(payload)

    if documented_count == 0:
        issues.append(
            _issue(
                "error",
                "$",
                "Documentation artifact contains no documented findings even though source_doc_index contains documentation content.",
            )
        )

    if total_count > 0 and missing_count == total_count:
        issues.append(
            _issue(
                "error",
                "$",
                "Documentation artifact marks every finding as missing even though source_doc_index contains documentation content.",
            )
        )

    return issues


def _verify_doc_required_fields(
    payload: dict[str, Any],
    source_doc_index: dict[str, Any],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    sections = source_doc_index.get("sections") or []
    commands = source_doc_index.get("commands") or []
    code_blocks = source_doc_index.get("code_blocks") or []
    references = source_doc_index.get("references") or []

    if _has_purpose_section(sections) and not _has_documented(payload, "project_purpose"):
        # If the planner produced a documented summary from a purpose-like section,
        # treat the missing project_purpose entries as a soft gap (warning) instead
        # of a hard fail: the same source information is already in summary.
        summary_documented = (
            isinstance(payload.get("summary"), dict)
            and payload["summary"].get("label") == "documented"
        )
        if summary_documented:
            issues.append(
                _issue(
                    "warning",
                    "$.project_purpose",
                    "source_doc_index contains a purpose/motivation section; project_purpose has no documented entry but summary does — overlap is acceptable, but consider planning a dedicated project_purpose task as well.",
                )
            )
        else:
            issues.append(
                _issue(
                    "error",
                    "$.project_purpose",
                    "source_doc_index contains a purpose/motivation section, but project_purpose has no documented finding.",
                )
            )

    if commands and not _has_documented(payload, "setup_and_run"):
        issues.append(
            _issue(
                "error",
                "$.setup_and_run",
                "source_doc_index contains command lines, but setup_and_run has no documented finding.",
            )
        )

    if code_blocks and not _has_documented(payload, "api_surface"):
        issues.append(
            _issue(
                "warning",
                "$.api_surface",
                "source_doc_index contains code blocks, but api_surface has no documented finding.",
            )
        )

    if references and not _has_documented(payload, "features"):
        issues.append(
            _issue(
                "warning",
                "$.features",
                "source_doc_index contains documented references or capabilities, but features has no documented finding.",
            )
        )

    useful_evidence_count = _count_file_evidence(payload)
    expected_min = min(
        12, max(4, len([s for s in sections if not _looks_like_code_heading(s.get("title", ""))]))
    )
    if useful_evidence_count < expected_min:
        issues.append(
            _issue(
                "warning",
                "$",
                f"Documentation artifact has low file-backed evidence coverage: expected at least {expected_min}, got {useful_evidence_count}.",
            )
        )

    return issues


def _verify_mini_task_outputs(
    payload: dict[str, Any],
    mini_tasks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []

    for task in mini_tasks:
        output_field = task.get("output_field")
        task_id = task.get("task_id", "<missing>")

        if not output_field or output_field not in payload:
            issues.append(
                _issue(
                    "error",
                    f"$.mini_tasks.{task_id}",
                    f"Mini task output_field {output_field!r} is missing from artifact.",
                )
            )
            continue

        items = _field_items(payload, output_field)
        min_items = int(task.get("min_items") or 0)

        # For fields whose schema permits only label="missing" (e.g. open_questions),
        # require ANY entry from this mini task, not "non-missing" — those are unreachable.
        if output_field in _MISSING_ONLY_FIELDS:
            if min_items > 0 and len(items) == 0:
                issues.append(
                    _issue(
                        "error",
                        f"$.{output_field}",
                        f"Mini task {task_id} produced 0 entries for missing-only field {output_field!r}; expected at least {min_items}.",
                    )
                )
            continue

        documented_or_inferred = [
            item
            for item in items
            if isinstance(item, dict)
            and item.get("label") in {"documented", "observed", "inferred"}
        ]

        if len(documented_or_inferred) >= min_items:
            continue

        # The agent did not produce enough documented/inferred entries. Two distinct
        # situations matter:
        #   1) zero entries at all -> agent failed to deliver anything (error)
        #   2) some entries, but all marked "missing" -> agent worked but the source
        #      simply does not contain the requested kind of content (warning, not error,
        #      because that is a legitimate factual finding)
        if len(items) == 0:
            issues.append(
                _issue(
                    "error",
                    f"$.{output_field}",
                    f"Mini task {task_id} produced 0 entries; expected at least {min_items}.",
                )
            )
        else:
            issues.append(
                _issue(
                    "warning",
                    f"$.{output_field}",
                    f"Mini task {task_id} produced {len(items)} entries but none are documented/inferred (all marked missing); expected at least {min_items}.",
                )
            )

    return issues


def _field_items(payload: dict[str, Any], field: str) -> list[dict[str, Any]]:
    value = payload.get(field)
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _has_documented(payload: dict[str, Any], field: str) -> bool:
    return any(item.get("label") == "documented" for item in _field_items(payload, field))


def _count_findings(payload: dict[str, Any]) -> int:
    count = 0
    for value in payload.values():
        if isinstance(value, dict) and "label" in value:
            count += 1
        elif isinstance(value, list):
            count += len([item for item in value if isinstance(item, dict) and "label" in item])
    return count


def _count_by_label(payload: dict[str, Any], label: str) -> int:
    count = 0
    for value in payload.values():
        if isinstance(value, dict) and value.get("label") == label:
            count += 1
        elif isinstance(value, list):
            count += len(
                [item for item in value if isinstance(item, dict) and item.get("label") == label]
            )
    return count


def _count_file_evidence(payload: dict[str, Any]) -> int:
    count = 0
    for value in payload.values():
        if isinstance(value, dict):
            evidence = value.get("evidence")
            if isinstance(evidence, dict) and evidence.get("file"):
                count += 1
        elif isinstance(value, list):
            for item in value:
                if not isinstance(item, dict):
                    continue
                evidence = item.get("evidence")
                if isinstance(evidence, dict) and evidence.get("file"):
                    count += 1
    return count


def _has_purpose_section(sections: list[dict[str, Any]]) -> bool:
    purpose_words = {
        "about",
        "overview",
        "purpose",
        "motivation",
        "motivação",
        "objetivo",
        "introduction",
        "intro",
    }
    for section in sections:
        title = str(section.get("title", "")).strip().lower()
        if any(word in title for word in purpose_words):
            return True
    return False


def _looks_like_code_heading(title: str) -> bool:
    lowered = title.strip().lower()
    return lowered.startswith("-*-") or lowered in {"builtin", "internal module"}


def _load_mapping(value: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    payload = json.loads(value)
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object, got {type(payload).__name__}")
    return payload


def _iter_mini_tasks(mini_tasks: str | dict[str, Any] | list[Any] | None) -> list[dict[str, Any]]:
    if mini_tasks is None:
        return []

    payload: Any = mini_tasks
    if isinstance(mini_tasks, str):
        try:
            payload = json.loads(mini_tasks)
        except json.JSONDecodeError:
            return []

    if isinstance(payload, dict) and isinstance(payload.get("mini_tasks"), list):
        return [item for item in payload["mini_tasks"] if isinstance(item, dict)]

    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    return []


def _issue(severity: str, path: str, message: str) -> dict[str, Any]:
    return {
        "severity": severity,
        "path": path,
        "message": message,
    }

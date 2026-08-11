"""Structural checks on a plan.

Deliberately narrow. A plan has two kinds of problem and they need different tools:

- **Structural** - a task cites an id that does not exist, two tasks share an id, a task
  targets a field this stage does not have. Deterministic, cheap, and checkable here.
- **Judgemental** - did the plan find the crux, is attention distributed sensibly, is
  anything important missing. No schema can measure that, so it belongs to the plan judge
  (Step 1.5), not to this file.

This used to attempt the second kind by matching `file` / `line_start` / `line_end`
against the index and snapping near-misses. Plans now cite opaque evidence ids
(CLAUDE.md section 2), so there is nothing to snap: an id either resolves or it does not.
"""

import json
import logging
from typing import Any

from packages.modules.supervising.schemas.behavior_analyzer import BEHAVIOR_OUTPUT_FIELDS
from packages.modules.supervising.schemas.code_facts_analyzer import CODE_FACTS_OUTPUT_FIELDS
from packages.modules.supervising.schemas.doc_analyzer import DOC_OUTPUT_FIELDS

log = logging.getLogger(__name__)

SPECIFICATION_OUTPUT_FIELDS: tuple[str, ...] = (
    "source_snapshot",
    "scope",
    "project_purpose",
    "system_overview",
    "components_and_interfaces",
    "functional_requirements",
    "behavioral_requirements",
    "error_handling",
    "configuration",
    "acceptance_criteria",
    "test_candidates",
    "gaps_and_open_questions",
    "evidence_references",
)

OUTPUT_FIELDS_BY_STAGE: dict[str, frozenset[str]] = {
    "documentation": frozenset(DOC_OUTPUT_FIELDS.keys()),
    "code_facts": frozenset(CODE_FACTS_OUTPUT_FIELDS),
    "behavior": frozenset(BEHAVIOR_OUTPUT_FIELDS),
    "specification": frozenset(SPECIFICATION_OUTPUT_FIELDS),
}

#: Sections a specification cannot be useful without. These may NEVER be declared "not
#: applicable" - a reader cannot rebuild a system from a document with no error handling
#: or no behavioural requirements, however conventional the material looks.
#:
#: The first live run marked Behavioral Requirements and Error Handling not-applicable with
#: the identical templated sentence in both, which is what this list exists to stop.
SPEC_CORE_FIELDS: frozenset[str] = frozenset(
    {
        "scope",
        "project_purpose",
        "system_overview",
        "components_and_interfaces",
        "functional_requirements",
        "behavioral_requirements",
        "error_handling",
    }
)

CORE_FIELDS_BY_STAGE: dict[str, frozenset[str]] = {"specification": SPEC_CORE_FIELDS}

#: A justification shorter than this cannot be specific about anything.
MIN_JUSTIFICATION_LENGTH = 40


class PlanSemanticError(Exception):
    """Raised when a plan is still structurally wrong after its repair budget."""

    def __init__(self, stage: str, issues: list[dict[str, Any]], raw_output: str):
        self.stage = stage
        self.issues = issues
        self.raw_output = raw_output
        preview = json.dumps(issues, ensure_ascii=False)[:500]
        super().__init__(f"Plan for stage {stage!r} failed structural checks: {preview}")


class PlanVerifier:
    """Checks that a plan is well formed and points at things that exist."""

    def __init__(self, alias_map: Any | None = None):
        self.alias_map = alias_map

    def verify(
        self,
        plan: str | dict[str, Any],
        stage: str,
        alias_map: Any | None = None,
        **_ignored: Any,
    ) -> dict[str, Any]:
        """Return the standard verification result: valid, count, issues.

        Extra keyword arguments are accepted and ignored so the supervisor can keep
        passing stage context that only the judge cares about now.
        """
        alias_map = alias_map or self.alias_map
        payload = _load(plan)
        issues: list[dict[str, Any]] = []

        if payload.get("stage") != stage:
            issues.append(
                _issue(
                    "error",
                    "$.stage",
                    f"Plan stage must be {stage!r}, got {payload.get('stage')!r}.",
                )
            )

        mini_tasks = [t for t in (payload.get("mini_tasks") or []) if isinstance(t, dict)]
        if not mini_tasks:
            issues.append(_issue("error", "$.mini_tasks", "Plan contains no mini tasks."))

        allowed = OUTPUT_FIELDS_BY_STAGE.get(stage, frozenset())
        seen_task_ids: set[str] = set()

        for index, task in enumerate(mini_tasks):
            path = f"$.mini_tasks[{index}]"
            issues.extend(self._verify_task(task, path, allowed, seen_task_ids, alias_map))

        issues.extend(self._verify_coverage(payload, mini_tasks, stage, allowed))

        return {
            "valid": not any(issue["severity"] == "error" for issue in issues),
            "checked_task_count": len(mini_tasks),
            "issues": issues,
        }

    def _verify_coverage(
        self,
        payload: dict[str, Any],
        mini_tasks: list[dict[str, Any]],
        stage: str,
        allowed: frozenset[str],
    ) -> list[dict[str, Any]]:
        """Presence: every allowed field is either planned or explicitly excused.

        Depth follows how hard material is to reproduce; presence does not. A field may be
        left out only by declaring it not applicable *with a reason specific to this
        section and this project* - and core sections may not be left out at all.
        """
        if not allowed:
            return []

        issues: list[dict[str, Any]] = []
        planned = {t.get("output_field") for t in mini_tasks}
        core = CORE_FIELDS_BY_STAGE.get(stage, frozenset())

        excused: dict[str, str] = {}
        for position, entry in enumerate(payload.get("not_applicable") or []):
            path = f"$.not_applicable[{position}]"
            if not isinstance(entry, dict):
                issues.append(_issue("error", path, "Each not_applicable entry must be an object."))
                continue
            field = entry.get("output_field")
            justification = entry.get("justification")

            if field not in allowed:
                issues.append(
                    _issue("error", path, f"{field!r} is not an output field of this stage.")
                )
                continue
            if field in core:
                issues.append(
                    _issue(
                        "error",
                        path,
                        f"{field!r} is a core section and can never be not applicable. "
                        "Plan at least one task for it, however conventional the material.",
                    )
                )
                continue
            if (
                not isinstance(justification, str)
                or len(justification.strip()) < MIN_JUSTIFICATION_LENGTH
            ):
                issues.append(
                    _issue(
                        "error",
                        path,
                        "not_applicable needs a justification specific to this section and "
                        f"this project (at least {MIN_JUSTIFICATION_LENGTH} characters).",
                    )
                )
                continue
            excused[field] = justification.strip()

        # The same sentence in two places is templated by definition: it cannot be specific
        # to either section. This is the exact abuse seen on the first live run.
        by_text: dict[str, list[str]] = {}
        for field, justification in excused.items():
            by_text.setdefault(" ".join(justification.lower().split()), []).append(field)
        for fields in by_text.values():
            if len(fields) > 1:
                issues.append(
                    _issue(
                        "error",
                        "$.not_applicable",
                        f"Fields {sorted(fields)} share one justification, so it is templated "
                        "rather than specific. Justify each separately or plan a task.",
                    )
                )

        for field in sorted(allowed - planned - set(excused)):
            issues.append(
                _issue(
                    "error",
                    f"$.coverage.{field}",
                    f"{field!r} has no task and was not declared not applicable. Every section "
                    "needs at least one task, or an explicit and specific justification.",
                )
            )

        return issues

    def _verify_task(
        self,
        task: dict[str, Any],
        path: str,
        allowed: frozenset[str],
        seen_task_ids: set[str],
        alias_map: Any | None,
    ) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []

        task_id = task.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            issues.append(_issue("error", f"{path}.task_id", "task_id must be a non-empty string."))
        elif task_id in seen_task_ids:
            issues.append(_issue("error", f"{path}.task_id", f"Duplicate task_id {task_id!r}."))
        else:
            seen_task_ids.add(task_id)

        output_field = task.get("output_field")
        if allowed and output_field not in allowed:
            issues.append(
                _issue(
                    "error",
                    f"{path}.output_field",
                    f"output_field {output_field!r} is not one this stage produces. "
                    f"Allowed: {sorted(allowed)}.",
                )
            )

        requirements = task.get("requirements")
        if not isinstance(requirements, list) or not any(
            isinstance(r, str) and r.strip() for r in requirements
        ):
            issues.append(
                _issue(
                    "error",
                    f"{path}.requirements",
                    "requirements must be a non-empty list of strings.",
                )
            )

        min_items = task.get("min_items")
        if not isinstance(min_items, int) or isinstance(min_items, bool) or min_items < 1:
            issues.append(
                _issue("error", f"{path}.min_items", "min_items must be a positive integer.")
            )

        issues.extend(self._verify_refs(task, path, alias_map))
        return issues

    def _verify_refs(
        self,
        task: dict[str, Any],
        path: str,
        alias_map: Any | None,
    ) -> list[dict[str, Any]]:
        """Every cited evidence id must resolve, and none may repeat within a task."""
        issues: list[dict[str, Any]] = []
        refs = task.get("input_refs")
        if not isinstance(refs, list):
            issues.append(_issue("error", f"{path}.input_refs", "input_refs must be a list."))
            return issues

        seen: set[str] = set()
        for position, ref in enumerate(refs):
            ref_path = f"{path}.input_refs[{position}]"
            if not isinstance(ref, dict):
                issues.append(_issue("error", ref_path, "Each input_ref must be an object."))
                continue

            evidence_id = ref.get("evidence_id")
            if not isinstance(evidence_id, str) or not evidence_id.strip():
                issues.append(
                    _issue(
                        "error",
                        ref_path,
                        "input_ref must cite an evidence_id, for example 'EV-014'.",
                    )
                )
                continue

            if evidence_id in seen:
                issues.append(
                    _issue("warning", ref_path, f"Evidence {evidence_id} cited twice in one task.")
                )
            seen.add(evidence_id)

            # A path or line number here means the planner reached around the boundary.
            for leaked in ("file", "line_start", "line_end", "evidence"):
                if ref.get(leaked) is not None:
                    issues.append(
                        _issue(
                            "error",
                            f"{ref_path}.{leaked}",
                            f"input_ref must not carry {leaked!r}; a plan cites evidence ids only "
                            "(CLAUDE.md section 2).",
                        )
                    )

            if alias_map is not None and alias_map.location_for(evidence_id) is None:
                issues.append(
                    _issue(
                        "error",
                        ref_path,
                        f"Evidence {evidence_id!r} does not exist. Cite an id from the "
                        "evidence catalogue; do not invent one.",
                    )
                )

        return issues


def _load(plan: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(plan, dict):
        return plan
    try:
        payload = json.loads(plan)
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _issue(severity: str, path: str, message: str) -> dict[str, Any]:
    return {"severity": severity, "path": path, "message": message}


__all__ = [
    "OUTPUT_FIELDS_BY_STAGE",
    "SPECIFICATION_OUTPUT_FIELDS",
    "PlanSemanticError",
    "PlanVerifier",
]

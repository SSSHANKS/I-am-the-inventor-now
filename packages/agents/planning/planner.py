"""The PlanningAgent: decides where analysis effort goes.

Superordinate rather than a dirt_team member - planning is a capability the clean team and
Border will need too (CLAUDE.md section 3). Its judge lives in dirt_team, because judging
requires reading the original.

The plan it emits is a **crossing artifact**: it cites opaque evidence ids and never names
anything from the original. The controller resolves those ids dirty-side when it pre-reads
source for an executing agent.
"""

import json
import logging
from typing import Any

from packages.agents.base_agent import BaseAgent
from packages.agents.dirt_team.plan_judge_agent import total_score
from packages.agents.planning.loop import MAX_ROUNDS, enforce_neutrality, run_plan_loop
from packages.agents.planning.prompts import PLANNER_INSTRUCTION
from packages.agents.planning.utils.common import log_mini_tasks
from packages.modules.border.corpus import evidence_excerpts
from packages.modules.ingesting import SourceManifest
from packages.modules.reconstruction_ir import build_planning_priority_batches
from packages.modules.supervising import PlanningPolicy
from packages.modules.supervising.verifiers.planning import (
    OUTPUT_FIELDS_BY_STAGE,
    PlanSemanticError,
    PlanVerifier,
)

log = logging.getLogger(__name__)

_STAGE_EVIDENCE_TYPES = {
    "documentation": frozenset({"documentation"}),
    "code_facts": frozenset({"code"}),
    "behavior": frozenset({"code", "documentation"}),
    "specification": frozenset({"code", "documentation"}),
}
PLANNER_CATALOGUE_LIMIT = 80


class PlanningAgent(BaseAgent):
    agent_name = "Planning Agent"
    instruction = PLANNER_INSTRUCTION

    def plan(
        self,
        stage: str,
        source_manifest: SourceManifest,
        evidence_catalogue: list[dict[str, str]] | None = None,
        allowed_output_fields: tuple[str, ...] | list[str] | None = None,
        judge: Any | None = None,
        code_index: dict[str, Any] | None = None,
        doc_index: dict[str, Any] | None = None,
        max_rounds: int = MAX_ROUNDS,
        reconstruction_inventory: dict[str, Any] | None = None,
        **_legacy: Any,
    ) -> str:
        """Produce a plan for one stage, refined against a judge.

        The judge is optional so the planner still works alone - useful in tests and when
        quota is tight - but with one supplied this runs the full loop: draft, gate on
        neutrality, score, revise, and keep the best neutral version.

        Extra keyword arguments from the older call style are accepted and ignored.
        """
        log.info("Planning Agent stage -> %s", stage)
        catalogue = filter_evidence_catalogue(evidence_catalogue or [], stage)
        priority_batches = build_planning_priority_batches(
            reconstruction_inventory, catalogue, stage
        )
        priorities = priority_batches[0] if priority_batches else []
        all_priorities = [item for batch in priority_batches for item in batch]
        visible_catalogue = _bounded_planner_catalogue(catalogue, priorities)
        fields = tuple(allowed_output_fields or OUTPUT_FIELDS_BY_STAGE.get(stage, ()))
        source_texts = _stage_source_texts(stage, code_index, doc_index)
        if not catalogue:
            log.warning(
                "Planning Agent has an empty evidence catalogue for %s; it can only plan blind",
                stage,
            )

        def draft(feedback: list[str]) -> str:
            return self.run(
                task_instruction=build_task_instruction(
                    stage=stage,
                    evidence_catalogue=visible_catalogue,
                    allowed_output_fields=fields,
                    feedback=feedback,
                    reconstruction_priorities=priorities,
                ),
                agent_name=f"Planning Agent [{stage}]",
                supervisor_policy=PlanningPolicy(alias_map=self.alias_map),
                supervisor_context={
                    "stage": stage,
                    "alias_map": self.alias_map,
                    "evidence_catalogue": visible_catalogue,
                    "reconstruction_priorities": priorities,
                },
                repo_local_path=source_manifest.repo_local_path,
                recorder_scope="planner",
                recorder_sub_scope=f"{stage} round",
            )

        def score(plan_text: str) -> dict[str, Any]:
            judgement = judge.judge(
                plan=plan_text,
                stage=stage,
                allowed_output_fields=fields,
                code_index=code_index,
                doc_index=doc_index,
                repo_local_path=source_manifest.repo_local_path,
            )
            judgement["_total_score"] = total_score(judgement)
            return judgement

        if self.alias_map is None:
            plan_text = _complete_priority_plan(
                draft([]), stage, priority_batches[1:], catalogue, all_priorities, None
            )
            _log_plan(plan_text, stage)
            return plan_text

        if judge is None:
            plan_text, neutral, leaks, _scrubbed = enforce_neutrality(
                draft([]),
                self.alias_map,
                source_texts,
            )
            if not neutral:
                log.error(
                    "Planning Agent [%s] still carries %d leak(s) after deterministic scrub",
                    stage,
                    len(leaks),
                )
            plan_text = _complete_priority_plan(
                plan_text,
                stage,
                priority_batches[1:],
                catalogue,
                all_priorities,
                self.alias_map,
            )
            _log_plan(plan_text, stage)
            return plan_text

        outcome = run_plan_loop(
            draft=draft,
            judge=score,
            alias_map=self.alias_map,
            stage=stage,
            max_rounds=max_rounds,
            source_texts=source_texts,
        )
        self.last_outcome = outcome
        if outcome.degraded:
            for note in outcome.border_review:
                log.error("%s", note)
        plan_text = _complete_priority_plan(
            outcome.plan,
            stage,
            priority_batches[1:],
            catalogue,
            all_priorities,
            self.alias_map,
        )
        _log_plan(plan_text, stage)
        return plan_text


def _bounded_planner_catalogue(
    catalogue: list[dict[str, Any]],
    priorities: list[dict[str, Any]],
    limit: int = PLANNER_CATALOGUE_LIMIT,
) -> list[dict[str, Any]]:
    """Keep the prompt bounded while always retaining its required priority ids."""
    required_ids = {
        item.get("evidence_id") for item in priorities if isinstance(item, dict)
    }
    required = [item for item in catalogue if item.get("evidence_id") in required_ids]
    supporting = [item for item in catalogue if item.get("evidence_id") not in required_ids]
    return [*required, *supporting[: max(0, limit - len(required))]]


def _complete_priority_plan(
    plan_text: str,
    stage: str,
    overflow_batches: list[list[dict[str, Any]]],
    catalogue: list[dict[str, Any]],
    all_priorities: list[dict[str, Any]],
    alias_map: Any | None,
) -> str:
    """Append neutral controller-owned tasks for priorities outside the LLM window."""
    payload = json.loads(plan_text)
    tasks = payload.setdefault("mini_tasks", [])
    excused = payload.get("not_applicable") or []
    generated_fields: set[str] = set()

    for batch_number, batch in enumerate(overflow_batches, start=2):
        grouped: dict[str, list[dict[str, Any]]] = {}
        for priority in batch:
            eligible = [
                field
                for field in priority.get("required_output_fields", [])
                if field in OUTPUT_FIELDS_BY_STAGE.get(stage, frozenset())
            ]
            if not eligible:
                raise ValueError(
                    f"Priority {priority.get('evidence_id')} has no eligible output field "
                    f"for stage {stage!r}"
                )
            grouped.setdefault(eligible[0], []).append(priority)

        for group_number, (output_field, priorities) in enumerate(
            sorted(grouped.items()), start=1
        ):
            generated_fields.add(output_field)
            tasks.append(
                {
                    "task_id": (
                        f"{stage[:4].upper()}-OVERFLOW-{batch_number:03d}-{group_number:02d}"
                    ),
                    "task_type": "extract_required_reconstruction_contracts",
                    "output_field": output_field,
                    "input_refs": [
                        {
                            "source": "reconstruction_priority",
                            "evidence_id": priority["evidence_id"],
                        }
                        for priority in priorities
                    ],
                    "requirements": [
                        "Extract one distinct, evidence-grounded reconstruction contract "
                        "for every required reference; do not merge or omit contracts."
                    ],
                    "min_items": len(priorities),
                }
            )

    if generated_fields:
        payload["not_applicable"] = [
            item
            for item in excused
            if not isinstance(item, dict) or item.get("output_field") not in generated_fields
        ]

    completed = json.dumps(payload, ensure_ascii=False, indent=2)
    verification = PlanVerifier(alias_map=alias_map).verify(
        completed,
        stage,
        alias_map=alias_map,
        evidence_catalogue=catalogue,
        reconstruction_priorities=all_priorities,
    )
    if not verification["valid"]:
        raise PlanSemanticError(stage, verification["issues"], completed)
    return completed


def build_task_instruction(
    stage: str,
    evidence_catalogue: list[dict[str, str]],
    allowed_output_fields: tuple[str, ...] | list[str],
    feedback: list[str] | None = None,
    reconstruction_priorities: list[dict[str, Any]] | None = None,
) -> str:
    """The planner's user prompt: what to plan, what it may cite, what to fix.

    Carries no manifest, no index and no reports - only opaque evidence ids with neutral
    descriptions. The planner cannot leak what it was never shown.
    """
    catalogue_lines = (
        "\n".join(
            f"- {entry['evidence_id']} "
            f"[{entry.get('source_type', 'evidence')}; {entry.get('source_role', 'source')}] "
            f"({entry.get('kind', 'evidence')}): {entry.get('about', '')}"
            for entry in evidence_catalogue
        )
        or "- <no evidence available>"
    )
    feedback_block = (
        "\n".join(f"- {line}" for line in feedback)
        if feedback
        else "- (first attempt; no feedback yet)"
    )
    priorities_block = json.dumps(
        reconstruction_priorities or [], ensure_ascii=False, indent=2
    )

    return f"""
<stage>
{stage}
</stage>

<allowed_output_fields>
{", ".join(allowed_output_fields) or "<none supplied>"}
</allowed_output_fields>

<evidence_catalogue>
{catalogue_lines}
</evidence_catalogue>

<reconstruction_priorities>
{priorities_block}
</reconstruction_priorities>

<feedback_on_your_previous_attempt>
{feedback_block}
</feedback_on_your_previous_attempt>

Decide how many tasks each allowed output field deserves - several for what the rebuilding
team cannot guess, one for the ordinary, none where nothing is hard to reproduce. Then write
those tasks, citing only evidence ids from the catalogue above.

Every entry in reconstruction_priorities with required=true MUST be cited by at least one
mini task whose output_field appears in that entry's required_output_fields. Group related
references in one task when appropriate, but set min_items to at least the number of required
priority references assigned to that task. Set source="reconstruction_priority" on those
required input_refs; use source="evidence_catalogue" for ordinary supporting references.
A behavioral_evidence entry describes behavior to extract; it is evidence and must not be
proposed as output code.

If feedback is present, address it directly: it comes from a reviewer who can see material
you cannot.

Return ONLY the JSON plan object.
""".strip()


def filter_evidence_catalogue(
    catalogue: list[dict[str, str]], stage: str
) -> list[dict[str, str]]:
    """Offer each planner only evidence its executing agent can read."""
    allowed = _STAGE_EVIDENCE_TYPES.get(stage)
    if not allowed:
        return list(catalogue)
    return [
        entry
        for entry in catalogue
        if entry.get("source_type") in allowed or "source_type" not in entry
    ]


def _stage_source_texts(
    stage: str,
    code_index: dict[str, Any] | None,
    doc_index: dict[str, Any] | None,
) -> tuple[str, ...]:
    """Dirty-only corpus used to remove copied prose before a plan can cross."""
    allowed = _STAGE_EVIDENCE_TYPES.get(stage, frozenset())
    sources: list[dict[str, Any] | None] = []
    if "code" in allowed:
        sources.append(code_index)
    if "documentation" in allowed:
        sources.append(doc_index)
    return evidence_excerpts(*sources)


def _log_plan(content: str, stage: str) -> None:
    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        log.warning("Planning Agent output could not be parsed for logging")
        return
    tasks = payload.get("mini_tasks") or []
    per_field: dict[str, int] = {}
    for task in tasks:
        if isinstance(task, dict):
            per_field[str(task.get("output_field"))] = (
                per_field.get(str(task.get("output_field")), 0) + 1
            )
    log.info("Planning Agent stage=%s tasks=%d distribution=%s", stage, len(tasks), per_field)
    log_mini_tasks("Planning Agent", payload, phase="planned")

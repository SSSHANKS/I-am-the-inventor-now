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
from packages.agents.planning.loop import MAX_ROUNDS, run_plan_loop
from packages.agents.planning.prompts import PLANNER_INSTRUCTION
from packages.agents.planning.utils.common import log_mini_tasks
from packages.modules.ingesting import SourceManifest
from packages.modules.supervising import PlanningPolicy
from packages.modules.supervising.verifiers.planning import OUTPUT_FIELDS_BY_STAGE

log = logging.getLogger(__name__)


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
        **_legacy: Any,
    ) -> str:
        """Produce a plan for one stage, refined against a judge.

        The judge is optional so the planner still works alone - useful in tests and when
        quota is tight - but with one supplied this runs the full loop: draft, gate on
        neutrality, score, revise, and keep the best neutral version.

        Extra keyword arguments from the older call style are accepted and ignored.
        """
        log.info("Planning Agent stage -> %s", stage)
        catalogue = evidence_catalogue or []
        fields = tuple(allowed_output_fields or OUTPUT_FIELDS_BY_STAGE.get(stage, ()))
        if not catalogue:
            log.warning(
                "Planning Agent has an empty evidence catalogue for %s; it can only plan blind",
                stage,
            )

        def draft(feedback: list[str]) -> str:
            return self.run(
                task_instruction=build_task_instruction(
                    stage=stage,
                    evidence_catalogue=catalogue,
                    allowed_output_fields=fields,
                    feedback=feedback,
                ),
                agent_name=f"Planning Agent [{stage}]",
                supervisor_policy=PlanningPolicy(alias_map=self.alias_map),
                supervisor_context={"stage": stage, "alias_map": self.alias_map},
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

        if judge is None or self.alias_map is None:
            plan_text = draft([])
            _log_plan(plan_text, stage)
            return plan_text

        outcome = run_plan_loop(
            draft=draft,
            judge=score,
            alias_map=self.alias_map,
            stage=stage,
            max_rounds=max_rounds,
        )
        self.last_outcome = outcome
        if outcome.degraded:
            for note in outcome.border_review:
                log.error("%s", note)
        _log_plan(outcome.plan, stage)
        return outcome.plan


def build_task_instruction(
    stage: str,
    evidence_catalogue: list[dict[str, str]],
    allowed_output_fields: tuple[str, ...] | list[str],
    feedback: list[str] | None = None,
) -> str:
    """The planner's user prompt: what to plan, what it may cite, what to fix.

    Carries no manifest, no index and no reports - only opaque evidence ids with neutral
    descriptions. The planner cannot leak what it was never shown.
    """
    catalogue_lines = (
        "\n".join(
            f"- {entry['evidence_id']} ({entry.get('kind', 'evidence')}): {entry.get('about', '')}"
            for entry in evidence_catalogue
        )
        or "- <no evidence available>"
    )
    feedback_block = (
        "\n".join(f"- {line}" for line in feedback)
        if feedback
        else "- (first attempt; no feedback yet)"
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

<feedback_on_your_previous_attempt>
{feedback_block}
</feedback_on_your_previous_attempt>

Decide how many tasks each allowed output field deserves - several for what the rebuilding
team cannot guess, one for the ordinary, none where nothing is hard to reproduce. Then write
those tasks, citing only evidence ids from the catalogue above.

If feedback is present, address it directly: it comes from a reviewer who can see material
you cannot.

Return ONLY the JSON plan object.
""".strip()


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

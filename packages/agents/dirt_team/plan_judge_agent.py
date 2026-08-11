"""The plan judge: grades a plan it did not write.

Dirt-side by definition. It reads the real project material, because a judge that cannot
see where the difficulty actually sits is grading in a vacuum - it could only check the
plan against itself.

Two deliberate limits:

- **It does not decide neutrality.** A deterministic scanner does. A judgement that
  disqualifies work has to be reliable and free, and this judge runs on the same model as
  the planner, so it shares the planner's blind spots (decided in Q20).
- **Its feedback is scrubbed before the planner sees it.** The judge names originals
  because it can see them; letting those words travel back into the planning prompt would
  manufacture the exact leak the gate then catches (decided in Q21).
"""

import json
import logging
from typing import Any

from packages.agents.base_agent import BaseAgent
from packages.agents.planning.prompts import JUDGE_INSTRUCTION
from packages.modules.supervising.schemas import PlanJudgementSchema

log = logging.getLogger(__name__)

#: Pillars that sum into the quality score. Neutrality is deliberately absent: it is a
#: gate, not a dimension, so a leaking plan can never out-score its way past it.
SCORED_PILLARS: tuple[str, ...] = (
    "crux_coverage",
    "proportional_decomposition",
    "completeness",
)
MAX_PILLAR_SCORE = 5


class PlanJudgeAgent(BaseAgent):
    agent_name = "Plan Judge Agent"
    instruction = JUDGE_INSTRUCTION

    def judge(
        self,
        plan: str,
        stage: str,
        allowed_output_fields: tuple[str, ...] | list[str],
        code_index: dict[str, Any] | None = None,
        doc_index: dict[str, Any] | None = None,
        repo_local_path: str | None = None,
    ) -> dict[str, Any]:
        """Score one plan and say what to change. Never raises on a bad verdict."""
        content = self.run(
            task_instruction=_build_judge_prompt(
                plan=plan,
                stage=stage,
                allowed_output_fields=allowed_output_fields,
                code_index=code_index,
                doc_index=doc_index,
            ),
            agent_name=f"Plan Judge Agent [{stage}]",
            schema=PlanJudgementSchema(),
            repo_local_path=repo_local_path,
            recorder_scope="planner",
            recorder_sub_scope=f"{stage} judge",
        )
        return _parse_judgement(content)


def total_score(judgement: dict[str, Any]) -> int:
    """Summed quality across the scored pillars. Neutrality is not part of this."""
    scores = judgement.get("scores") or {}
    return sum(
        min(MAX_PILLAR_SCORE, max(0, int(scores.get(pillar, 0) or 0))) for pillar in SCORED_PILLARS
    )


def _parse_judgement(content: str) -> dict[str, Any]:
    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        log.warning("Plan judge returned unparseable output; treating as a zero-score verdict")
        return {"strongest_objection": "", "scores": {}, "actions": []}
    return payload if isinstance(payload, dict) else {"scores": {}, "actions": []}


def _build_judge_prompt(
    plan: str,
    stage: str,
    allowed_output_fields: tuple[str, ...] | list[str],
    code_index: dict[str, Any] | None,
    doc_index: dict[str, Any] | None,
) -> str:
    return f"""
<stage>
{stage}
</stage>

<allowed_output_fields>
{", ".join(allowed_output_fields)}
</allowed_output_fields>

<plan_under_review>
{plan}
</plan_under_review>

<project_material>
{_summarise_material(code_index, doc_index)}
</project_material>

Review this plan. Give your strongest objection first, then score the three pillars 0-5,
then list specific actions citing evidence ids.

Before scoring, check the allowed output fields above against the plan one by one. Any field
with no task is a hole in the resulting document and must cost the plan marks - coverage is
the floor. Then judge whether depth varies with difficulty, or is flat.
""".strip()


def _summarise_material(
    code_index: dict[str, Any] | None,
    doc_index: dict[str, Any] | None,
) -> str:
    """A compact dirty-side view: enough to locate the difficulty, not the whole index.

    The judge needs to know what the project actually contains. It does not need every
    call site, and a prompt stuffed with the full index crowds out its actual job.
    """
    lines: list[str] = []

    if code_index:
        lines.append("Implementation:")
        for collection, label in (
            ("analysis_targets", "analysis targets"),
            ("classes", "components"),
            ("functions", "operations"),
            ("configs", "configuration documents"),
        ):
            items = code_index.get(collection) or []
            if items:
                lines.append(f"- {len(items)} {label}")
        for item in (code_index.get("analysis_targets") or [])[:25]:
            excerpt = (item.get("evidence") or {}).get("excerpt", "")
            lines.append(f"  * {item.get('target_type', '?')}: {str(excerpt)[:110]}")

    if doc_index:
        lines.append("")
        lines.append("Documentation:")
        for item in (doc_index.get("sections") or [])[:20]:
            lines.append(f"  * section: {str(item.get('title', ''))[:110]}")
        for item in (doc_index.get("commands") or [])[:10]:
            lines.append(f"  * command: {str(item.get('command', ''))[:110]}")

    return "\n".join(lines) or "<no project material available>"

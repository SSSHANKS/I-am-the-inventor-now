from __future__ import annotations

import json
from typing import Any

from packages.agents.base_agent import BaseAgent
from packages.agents.clean_team.prompts import PLANNER_INSTRUCTION
from packages.modules.supervising.schemas import CleanPlanSchema


class CleanPlannerAgent(BaseAgent):
    agent_name = "Clean Planner"
    instruction = PLANNER_INSTRUCTION

    def plan(
        self,
        specification: str,
        *,
        compatibility_mode: str = "renamed",
        executable_validation_policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        prompt = (
            "<clean_plan_request>\n"
            "Create the reconstruction plan from this complete approved specification.\n"
            "Clean may deterministically prefix otherwise unlabeled requirement "
            "statements with traceability IDs. These labels add no behavior; use every "
            "supplied requirement ID and treat its labeled statement as authoritative.\n"
            "<compatibility_mode>\n"
            f"{compatibility_mode}\n"
            "</compatibility_mode>\n"
            "<executable_validation_policy>\n"
            f"{json.dumps(executable_validation_policy or {'enabled': False}, ensure_ascii=False, indent=2)}\n"
            "</executable_validation_policy>\n"
            "<approved_specification>\n"
            f"{specification}\n"
            "</approved_specification>\n"
            "</clean_plan_request>"
        )
        content = self.run(task_instruction=prompt, schema=CleanPlanSchema())
        return json.loads(content)

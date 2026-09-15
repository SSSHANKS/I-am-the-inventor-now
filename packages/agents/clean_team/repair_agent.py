from __future__ import annotations

import json
from typing import Any

from packages.agents.base_agent import BaseAgent
from packages.agents.clean_team.prompts import REPAIR_INSTRUCTION
from packages.modules.supervising.schemas import CleanRepairSchema


class CleanRepairAgent(BaseAgent):
    agent_name = "Clean Repairer"
    instruction = REPAIR_INSTRUCTION

    def repair_files(
        self,
        specification_context: str,
        plan_context: dict[str, Any],
        failures: list[dict[str, Any]],
        current_files: dict[str, str],
        allowed_paths: set[str],
        *,
        compatibility_mode: str = "renamed",
        related_files: dict[str, str] | None = None,
        omitted_related_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        prompt = (
            "<clean_repair_request>\n"
            "<compatibility_mode>\n"
            f"{compatibility_mode}\n"
            "</compatibility_mode>\n"
            "<relevant_specification_context>\n"
            f"{specification_context}\n"
            "</relevant_specification_context>\n"
            "<scoped_clean_plan>\n"
            f"{json.dumps(plan_context, ensure_ascii=False, indent=2)}\n"
            "</scoped_clean_plan>\n"
            "<allowed_paths>\n"
            f"{json.dumps(sorted(allowed_paths), ensure_ascii=False)}\n"
            "</allowed_paths>\n"
            "<sanitized_failures>\n"
            f"{json.dumps(failures, ensure_ascii=False, indent=2)}\n"
            "</sanitized_failures>\n"
            "<current_generated_files>\n"
            f"{json.dumps(current_files, ensure_ascii=False, indent=2)}\n"
            "</current_generated_files>\n"
            "<related_generated_files_read_only>\n"
            f"{json.dumps(related_files or {}, ensure_ascii=False, indent=2)}\n"
            "</related_generated_files_read_only>\n"
            "<omitted_related_paths>\n"
            f"{json.dumps(omitted_related_paths or [], ensure_ascii=False)}\n"
            "</omitted_related_paths>\n"
            "</clean_repair_request>"
        )
        content = self.run(task_instruction=prompt, schema=CleanRepairSchema())
        return json.loads(content)

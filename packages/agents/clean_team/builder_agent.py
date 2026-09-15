from __future__ import annotations

import json
from typing import Any

from packages.agents.base_agent import BaseAgent
from packages.agents.clean_team.prompts import BUILDER_INSTRUCTION
from packages.modules.supervising.schemas import CleanFileBatchSchema


class CleanBuilderAgent(BaseAgent):
    agent_name = "Clean Builder"
    instruction = BUILDER_INSTRUCTION

    def build_file(
        self,
        specification_context: str,
        plan_context: dict[str, Any],
        file_task: dict[str, Any],
        *,
        compatibility_mode: str = "renamed",
        dependency_files: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        prompt = (
            "<clean_file_task>\n"
            "<compatibility_mode>\n"
            f"{compatibility_mode}\n"
            "</compatibility_mode>\n"
            "<relevant_specification_context>\n"
            f"{specification_context}\n"
            "</relevant_specification_context>\n"
            "<scoped_clean_plan>\n"
            f"{json.dumps(plan_context, ensure_ascii=False, indent=2)}\n"
            "</scoped_clean_plan>\n"
            "<completed_dependency_files>\n"
            f"{json.dumps(dependency_files or {}, ensure_ascii=False, indent=2)}\n"
            "</completed_dependency_files>\n"
            "<requested_file>\n"
            f"{json.dumps(file_task, ensure_ascii=False, indent=2)}\n"
            "</requested_file>\n"
            "</clean_file_task>"
        )
        content = self.run(task_instruction=prompt, schema=CleanFileBatchSchema())
        return json.loads(content)

from __future__ import annotations

import json
import re
from typing import Any

from packages.agents.base_agent import BaseAgent
from packages.agents.clean_team.prompts import ARCHITECT_INSTRUCTION
from packages.modules.supervising.schemas import CleanArchitectureSchema


class CleanArchitectAgent(BaseAgent):
    agent_name = "Clean Architect"
    instruction = ARCHITECT_INSTRUCTION

    def design(
        self,
        specification: str,
        *,
        compatibility_mode: str = "renamed",
        runtime_policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        behavior_ids, scenario_ids = _requirement_inventory(specification)
        prompt = (
            "<clean_architecture_request>\n"
            "Design a project-first architecture from this complete approved "
            "specification. Do not choose project files or generate tests.\n"
            "<compatibility_mode>\n"
            f"{compatibility_mode}\n"
            "</compatibility_mode>\n"
            "<runtime_policy>\n"
            f"{json.dumps(runtime_policy or {'enabled': False}, ensure_ascii=False, indent=2)}\n"
            "</runtime_policy>\n"
            "<required_behavior_ids>\n"
            f"{json.dumps(behavior_ids)}\n"
            "</required_behavior_ids>\n"
            "<required_scenario_ids>\n"
            f"{json.dumps(scenario_ids)}\n"
            "</required_scenario_ids>\n"
            "<approved_specification>\n"
            f"{specification}\n"
            "</approved_specification>\n"
            "</clean_architecture_request>"
        )
        content = self.run(task_instruction=prompt, schema=CleanArchitectureSchema())
        return json.loads(content)

    def revise(
        self,
        specification: str,
        architecture: dict[str, Any],
        diagnostic: str,
        *,
        compatibility_mode: str = "renamed",
        runtime_policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Repair a schema-valid architecture rejected by deterministic validation."""
        behavior_ids, scenario_ids = _requirement_inventory(specification)
        prompt = (
            "<clean_architecture_repair_request>\n"
            "Return a complete corrected architecture. Preserve valid design choices, "
            "but resolve the deterministic validation diagnostic exactly. Do not choose "
            "project files or generate tests.\n"
            "<compatibility_mode>\n"
            f"{compatibility_mode}\n"
            "</compatibility_mode>\n"
            "<runtime_policy>\n"
            f"{json.dumps(runtime_policy or {'enabled': False}, ensure_ascii=False, indent=2)}\n"
            "</runtime_policy>\n"
            "<validation_diagnostic>\n"
            f"{diagnostic}\n"
            "</validation_diagnostic>\n"
            "<required_behavior_ids>\n"
            f"{json.dumps(behavior_ids)}\n"
            "</required_behavior_ids>\n"
            "<required_scenario_ids>\n"
            f"{json.dumps(scenario_ids)}\n"
            "</required_scenario_ids>\n"
            "<architecture_under_review>\n"
            f"{json.dumps(architecture, ensure_ascii=False, indent=2)}\n"
            "</architecture_under_review>\n"
            "<approved_specification>\n"
            f"{specification}\n"
            "</approved_specification>\n"
            "</clean_architecture_repair_request>"
        )
        content = self.run(task_instruction=prompt, schema=CleanArchitectureSchema())
        return json.loads(content)


def _requirement_inventory(specification: str) -> tuple[list[str], list[str]]:
    identifiers = list(
        dict.fromkeys(
            re.findall(r"(?m)^\s*-\s+((?:FR|BR|EH|AC|TC)-\d{3,})\s*:", specification)
        )
    )
    return (
        [identifier for identifier in identifiers if not identifier.startswith("TC-")],
        [identifier for identifier in identifiers if identifier.startswith("TC-")],
    )

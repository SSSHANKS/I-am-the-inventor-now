from __future__ import annotations

import json
from typing import Any

from packages.agents.base_agent import BaseAgent
from packages.agents.clean_team.prompts import BEHAVIOR_PROBE_INSTRUCTION
from packages.modules.supervising.schemas import CleanBehaviorProbeSuiteSchema


class CleanBehaviorProbeAgent(BaseAgent):
    """Design stable, ephemeral behavior oracles from approved Clean inputs."""

    agent_name = "Clean Behavioral Verifier"
    instruction = BEHAVIOR_PROBE_INSTRUCTION

    def design(
        self,
        specification: str,
        architecture: dict[str, Any],
        manifest: dict[str, Any],
        *,
        target_capability: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        prompt = self._prompt(
            "clean_behavior_probe_request",
            specification,
            architecture,
            manifest,
            target_capability=target_capability,
        )
        content = self.run(
            task_instruction=prompt,
            schema=CleanBehaviorProbeSuiteSchema(),
        )
        return json.loads(content)

    def revise(
        self,
        specification: str,
        architecture: dict[str, Any],
        manifest: dict[str, Any],
        probes: dict[str, Any],
        diagnostic: str,
        *,
        target_capability: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        prompt = self._prompt(
            "clean_behavior_probe_repair_request",
            specification,
            architecture,
            manifest,
            probes=probes,
            diagnostic=diagnostic,
            target_capability=target_capability,
        )
        content = self.run(
            task_instruction=prompt,
            schema=CleanBehaviorProbeSuiteSchema(),
        )
        return json.loads(content)

    @staticmethod
    def _prompt(
        request_tag: str,
        specification: str,
        architecture: dict[str, Any],
        manifest: dict[str, Any],
        *,
        probes: dict[str, Any] | None = None,
        diagnostic: str | None = None,
        target_capability: dict[str, Any] | None = None,
    ) -> str:
        parts = [
            f"<{request_tag}>",
            "Create a complete behavior probe suite for the validated architecture.",
        ]
        if target_capability is not None:
            parts.extend(
                [
                    "<target_capability>",
                    json.dumps(target_capability, ensure_ascii=False, indent=2),
                    "</target_capability>",
                    "Return probes only for this capability and cover every one of its "
                    "requirement IDs exactly once.",
                ]
            )
        if diagnostic is not None:
            parts.extend(
                [
                    "<validation_diagnostic>",
                    diagnostic,
                    "</validation_diagnostic>",
                    "<probe_suite_under_review>",
                    json.dumps(probes, ensure_ascii=False, indent=2),
                    "</probe_suite_under_review>",
                ]
            )
        parts.extend(
            [
                "<approved_specification>",
                specification,
                "</approved_specification>",
                "<validated_architecture>",
                json.dumps(architecture, ensure_ascii=False, indent=2),
                "</validated_architecture>",
                "<validated_manifest>",
                json.dumps(manifest, ensure_ascii=False, indent=2),
                "</validated_manifest>",
                f"</{request_tag}>",
            ]
        )
        return "\n".join(parts)

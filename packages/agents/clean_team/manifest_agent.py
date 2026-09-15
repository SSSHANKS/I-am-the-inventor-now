from __future__ import annotations

import json
from typing import Any

from packages.agents.base_agent import BaseAgent
from packages.agents.clean_team.prompts import MANIFEST_INSTRUCTION
from packages.modules.supervising.schemas import CleanManifestSchema


class CleanManifestAgent(BaseAgent):
    agent_name = "Clean Manifest Designer"
    instruction = MANIFEST_INSTRUCTION

    def derive(
        self,
        architecture: dict[str, Any],
        *,
        adapter_policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Delay loading the legacy Clean facade until the agent package has finished
        # initializing; the facade still eagerly imports the compatibility runner.
        from packages.modules.clean.manifest import (
            architecture_sha256,
            normalise_manifest,
        )

        architecture_hash = architecture_sha256(architecture)
        constraints = _manifest_constraints(architecture)
        prompt = (
            "<clean_manifest_request>\n"
            "Derive the project files and readiness obligations from this validated "
            "architecture. Do not generate project tests or file content.\n"
            "<architecture_sha256>\n"
            f"{architecture_hash}\n"
            "</architecture_sha256>\n"
            "<adapter_policy>\n"
            f"{json.dumps(adapter_policy or {'enabled': False}, ensure_ascii=False, indent=2)}\n"
            "</adapter_policy>\n"
            "<manifest_constraints>\n"
            f"{json.dumps(constraints, ensure_ascii=False, indent=2)}\n"
            "</manifest_constraints>\n"
            "<validated_architecture>\n"
            f"{json.dumps(architecture, ensure_ascii=False, indent=2)}\n"
            "</validated_architecture>\n"
            "</clean_manifest_request>"
        )
        content = self.run(task_instruction=prompt, schema=CleanManifestSchema())
        return normalise_manifest(json.loads(content), architecture)

    def revise(
        self,
        architecture: dict[str, Any],
        manifest: dict[str, Any],
        diagnostic: str,
        *,
        adapter_policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Repair a schema-valid manifest rejected by deterministic validation."""
        from packages.modules.clean.manifest import architecture_sha256, normalise_manifest

        prompt = (
            "<clean_manifest_repair_request>\n"
            "Return a complete corrected manifest. Preserve valid file decisions and "
            "resolve the deterministic validation diagnostic exactly. Do not generate "
            "file content or project tests.\n"
            "<architecture_sha256>\n"
            f"{architecture_sha256(architecture)}\n"
            "</architecture_sha256>\n"
            "<adapter_policy>\n"
            f"{json.dumps(adapter_policy or {'enabled': False}, ensure_ascii=False, indent=2)}\n"
            "</adapter_policy>\n"
            "<validation_diagnostic>\n"
            f"{diagnostic}\n"
            "</validation_diagnostic>\n"
            "<manifest_under_review>\n"
            f"{json.dumps(manifest, ensure_ascii=False, indent=2)}\n"
            "</manifest_under_review>\n"
            "<validated_architecture>\n"
            f"{json.dumps(architecture, ensure_ascii=False, indent=2)}\n"
            "</validated_architecture>\n"
            "</clean_manifest_repair_request>"
        )
        content = self.run(task_instruction=prompt, schema=CleanManifestSchema())
        return normalise_manifest(json.loads(content), architecture)


def _manifest_constraints(architecture: dict[str, Any]) -> dict[str, Any]:
    contracts_by_component: dict[str, list[str]] = {
        item["component_id"]: [] for item in architecture["components"]
    }
    for contract in architecture["contracts"]:
        contracts_by_component[contract["component_id"]].append(contract["contract_id"])
    return {
        "components": [
            {
                "component_id": item["component_id"],
                "requirement_ids": item["requirement_ids"],
                "depends_on": item["depends_on"],
                "owned_contract_ids": contracts_by_component[item["component_id"]],
            }
            for item in architecture["components"]
        ]
    }

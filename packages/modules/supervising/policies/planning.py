"""Supervisor policy for plans.

Schema validation plus the structural checks in `PlanVerifier`. There is no autofixer any
more: the old one repaired plans by snapping a ref's `file`/`line_start`/`line_end` onto
the nearest indexed candidate, and plans now cite opaque evidence ids, which either
resolve or do not. Guessing which id a planner *meant* would be inventing evidence -
exactly what the boundary exists to prevent.
"""

from typing import Any, NoReturn

from marshmallow import Schema

from packages.modules.supervising.policies.base import BaseSupervisorPolicy
from packages.modules.supervising.prompts.planning import build_semantic_correction_prompt
from packages.modules.supervising.schemas.planner import PlanningSchema
from packages.modules.supervising.schemas.validator import build_correction_prompt
from packages.modules.supervising.verifiers.planning import PlanSemanticError, PlanVerifier


class PlanningPolicy(BaseSupervisorPolicy):
    """Supervisor policy for PlanningAgent outputs."""

    name = "Planning Agent"
    verification_phase = "structural"

    def __init__(self, verifier: PlanVerifier | None = None, alias_map: Any | None = None):
        self.alias_map = alias_map
        self.verifier = verifier or PlanVerifier(alias_map=alias_map)

    def schema(self, context: dict[str, Any]) -> Schema | None:
        return PlanningSchema()

    def verify(self, content: str, context: dict[str, Any]) -> dict[str, Any] | None:
        return self.verifier.verify(
            plan=content,
            stage=self._stage(context),
            alias_map=context.get("alias_map") or self.alias_map,
        )

    def build_schema_repair_prompt(self, errors: dict[str, Any], context: dict[str, Any]) -> str:
        return (
            "Schema repair phase. Do not add new planning ideas. "
            "Every mini task needs requirements as a non-empty list and min_items as a "
            "positive integer, and every input_ref needs an evidence_id.\n\n"
            f"{build_correction_prompt(errors)}"
        )

    def build_verification_repair_prompt(
        self, issues: list[dict[str, Any]], context: dict[str, Any]
    ) -> str:
        return (
            "Structural repair phase. Keep your judgement about what matters; fix only the "
            "mechanics. Cite evidence ids that appear in the evidence catalogue you were "
            "given - never invent an id, and never add a file path or line number.\n\n"
            f"{build_semantic_correction_prompt(issues)}"
        )

    def raise_verification_error(
        self, issues: list[dict[str, Any]], content: str, context: dict[str, Any]
    ) -> NoReturn:
        raise PlanSemanticError(self._stage(context), issues, content)

    def _stage(self, context: dict[str, Any]) -> str:
        stage = context.get("stage")
        if not isinstance(stage, str) or not stage.strip():
            raise ValueError("PlanningPolicy requires context['stage'] as a non-empty string.")
        return stage

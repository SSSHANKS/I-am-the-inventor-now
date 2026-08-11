import json
from typing import Any, NoReturn

from marshmallow import Schema

from packages.modules.supervising.policies.base import BaseSupervisorPolicy
from packages.modules.supervising.prompts.artifact import build_artifact_correction_prompt
from packages.modules.supervising.schemas.validator import build_correction_prompt
from packages.modules.supervising.utils.common import AutofixResult
from packages.modules.supervising.verifiers import ArtifactSemanticError, ArtifactVerifier


class ArtifactPolicy(BaseSupervisorPolicy):
    """Supervisor policy for evidence-backed agent artifacts."""

    verification_phase = "artifact"

    def __init__(
        self,
        name: str,
        schema: Schema | None = None,
        verifier: ArtifactVerifier | None = None,
    ):
        self.name = name
        self._schema = schema
        self.verifier = verifier

    def schema(self, context: dict[str, Any]) -> Schema | None:
        return self._schema

    def autofix(self, content: str, context: dict[str, Any]) -> AutofixResult:
        if self._schema is None:
            return AutofixResult(content=content)

        fixed, fixes = _normalise_entry_content(content, self._schema)
        return AutofixResult(
            content=fixed,
            fixes=fixes,
            repair_content=content,
        )

    def verify(self, content: str, context: dict[str, Any]) -> dict[str, Any] | None:
        if self.verifier is None:
            return None
        return self.verifier.verify(
            artifact=content,
            allowed_files=context.get("allowed_files") or [],
        )

    def build_schema_repair_prompt(
        self,
        errors: dict[str, Any],
        context: dict[str, Any],
    ) -> str:
        return (
            "Schema repair phase. Do not call tools. "
            "Rewrite your previous output to match the required schema exactly.\n\n"
            f"{build_correction_prompt(errors)}"
        )

    def build_verification_repair_prompt(
        self,
        issues: list[dict[str, Any]],
        context: dict[str, Any],
    ) -> str:
        return (
            "Artifact verification phase. Do not call tools. "
            "Reuse the indexed items, tool results, and findings already in this conversation; "
            "do not invent files, line numbers, or excerpts.\n\n"
            f"{build_artifact_correction_prompt(issues)}"
        )

    def raise_verification_error(
        self,
        issues: list[dict[str, Any]],
        content: str,
        context: dict[str, Any],
    ) -> NoReturn:
        raise ArtifactSemanticError(self.name, issues, content)


def _normalise_entry_content(content: str, schema: Schema) -> tuple[str, list[dict[str, Any]]]:
    """Best-effort fixup for model outputs that almost match an entry schema."""

    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return content, []
    if not isinstance(payload, dict):
        return content, []

    schema_fields = set(schema.fields.keys()) if hasattr(schema, "fields") else set()
    fixes: list[dict[str, Any]] = []

    inner = payload.get("content")
    if isinstance(inner, dict) and ("label" in inner or "value" in inner or "evidence" in inner):
        payload = inner
        fixes.append({"kind": "unwrap_content_envelope"})

    if (
        "value" in schema_fields
        and "value" not in payload
        and isinstance(payload.get("content"), str)
    ):
        payload["value"] = payload.pop("content")
        fixes.append({"kind": "rename_content_to_value"})

    if not fixes:
        return content, []
    return json.dumps(payload, ensure_ascii=False, indent=2), fixes

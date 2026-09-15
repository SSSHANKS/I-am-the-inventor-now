import json
from typing import Any, NoReturn

from marshmallow import Schema

from packages.modules.supervising.policies.artifact import ArtifactPolicy
from packages.modules.supervising.utils.common import SupervisorVerificationError


class ExtractionPolicy(ArtifactPolicy):
    """Validate that one narrow extraction fulfils its planned contract."""

    verification_phase = "extraction_coverage"

    def __init__(self, name: str, schema: Schema):
        super().__init__(name=name, schema=schema)

    def verify(self, content: str, context: dict[str, Any]) -> dict[str, Any]:
        payload = json.loads(content)
        items = payload.get("items")
        issues: list[dict[str, Any]] = []

        if not isinstance(items, list):
            # Some narrow documentation contracts are a single object. Schema
            # validation has already established that object is complete.
            return {"valid": True, "issues": []}

        distinct_items = {
            json.dumps(item, ensure_ascii=False, sort_keys=True)
            for item in items
            if isinstance(item, dict)
        }
        minimum = context.get("minimum_items", 0)
        if isinstance(minimum, int) and len(distinct_items) < minimum:
            issues.append(
                _issue(
                    "$.items",
                    f"The plan requires at least {minimum} distinct item(s), but the result "
                    f"contains {len(distinct_items)}.",
                )
            )

        valid_refs = {
            value for value in context.get("valid_source_refs", []) if isinstance(value, int)
        }
        used_refs = {
            item.get("source_ref")
            for item in items
            if isinstance(item, dict) and isinstance(item.get("source_ref"), int)
        }
        invalid_refs = sorted(used_refs - valid_refs)
        if invalid_refs:
            issues.append(
                _issue(
                    "$.items",
                    f"Items cite unavailable source_ref values: {invalid_refs}.",
                )
            )

        required_refs = {
            value for value in context.get("required_source_refs", []) if isinstance(value, int)
        }
        missing_refs = sorted(required_refs - used_refs)
        if missing_refs:
            issues.append(
                _issue(
                    "$.items",
                    "No extracted item is grounded in required source_ref value(s): "
                    f"{missing_refs}.",
                )
            )

        required_evidence = {
            value
            for value in context.get("required_evidence_ids", [])
            if isinstance(value, str) and value
        }
        rendered = json.dumps(payload, ensure_ascii=False)
        missing_evidence = sorted(value for value in required_evidence if value not in rendered)
        if missing_evidence:
            issues.append(
                _issue(
                    "$.items",
                    "The extraction does not cite required evidence identifier(s): "
                    f"{missing_evidence}.",
                )
            )

        return {"valid": not issues, "issues": issues}

    def build_verification_repair_prompt(
        self,
        issues: list[dict[str, Any]],
        context: dict[str, Any],
    ) -> str:
        messages = "\n".join(f"- {issue['message']}" for issue in issues)
        return (
            "Extraction completeness repair. Rewrite the previous JSON object only.\n"
            "Add the missing distinct items using only the source_sections and verified "
            "artifacts already present in this conversation. Preserve correct items. Do not "
            "invent facts, source_ref values, or evidence identifiers.\n\n"
            f"Problems to repair:\n{messages}"
        )

    def raise_verification_error(
        self,
        issues: list[dict[str, Any]],
        content: str,
        context: dict[str, Any],
    ) -> NoReturn:
        raise SupervisorVerificationError(self.name, issues, content)


def _issue(path: str, message: str) -> dict[str, str]:
    return {"severity": "error", "path": path, "message": message}

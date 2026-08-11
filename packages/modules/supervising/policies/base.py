from typing import Any, NoReturn

from marshmallow import Schema

from packages.modules.supervising.schemas.validator import (
    SchemaValidationError,
    build_correction_prompt,
)
from packages.modules.supervising.utils.common import AutofixResult, SupervisorVerificationError


class BaseSupervisorPolicy:
    """Domain hooks used by `BaseSupervisor`.

    Subclasses define the stage-specific rules: schema, deterministic autofixes,
    verifier call, and semantic repair prompt. The base policy is intentionally
    permissive so simple agents can opt into only the phases they need.
    """

    name = "Supervisor"
    verification_phase = "verification"

    def schema(self, context: dict[str, Any]) -> Schema | None:
        return None

    def autofix(self, content: str, context: dict[str, Any]) -> AutofixResult:
        return AutofixResult(content=content)

    def verify(self, content: str, context: dict[str, Any]) -> dict[str, Any] | None:
        return None

    def blocking_issues(
        self,
        verification: dict[str, Any],
        context: dict[str, Any],
    ) -> list[dict[str, Any]]:
        issues = verification.get("issues") or []
        return [issue for issue in issues if issue.get("severity") == "error"]

    def build_schema_repair_prompt(
        self,
        errors: dict[str, Any],
        context: dict[str, Any],
    ) -> str:
        return build_correction_prompt(errors)

    def build_verification_repair_prompt(
        self,
        issues: list[dict[str, Any]],
        context: dict[str, Any],
    ) -> str:
        raise NotImplementedError(
            f"{self.__class__.__name__} must build a verification repair prompt."
        )

    def raise_schema_error(
        self,
        errors: dict[str, Any],
        content: str,
        context: dict[str, Any],
    ) -> NoReturn:
        raise SchemaValidationError(self.name, errors, content)

    def raise_verification_error(
        self,
        issues: list[dict[str, Any]],
        content: str,
        context: dict[str, Any],
    ) -> NoReturn:
        raise SupervisorVerificationError(self.name, issues, content)

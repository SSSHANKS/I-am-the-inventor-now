from dataclasses import dataclass, field
from typing import Any


class SupervisorVerificationError(Exception):
    """Raised when supervised output fails verifier checks after retries."""

    def __init__(self, supervisor_name: str, issues: list[dict[str, Any]], raw_output: str):
        self.supervisor_name = supervisor_name
        self.issues = issues
        self.raw_output = raw_output
        preview = str(issues)[:500]
        super().__init__(f"{supervisor_name} output failed verification: {preview}")


@dataclass(frozen=True)
class AutofixResult:
    """Result of a deterministic policy autofix step.

    `content` is the candidate that should be validated. `repair_content` is the
    text shown back to the model if validation still fails. Keeping those separate
    lets a policy validate an auto-fixed candidate without teaching the model to
    copy mechanical injected fixes during repair turns.
    """

    content: str
    fixes: list[dict[str, Any]] = field(default_factory=list)
    repair_content: str | None = None

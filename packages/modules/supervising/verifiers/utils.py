from typing import Any, NamedTuple


class VerificationIssue(NamedTuple):
    severity: str  # error | warning
    path: str
    message: str
    evidence: dict[str, Any]


class ArtifactVerificationResult(NamedTuple):
    valid: bool
    checked_evidence_count: int
    issues: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return self._asdict()

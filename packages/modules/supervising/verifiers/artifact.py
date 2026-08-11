import json
import logging
from typing import Any

from packages.modules.skills.reading import Reader
from packages.modules.supervising.verifiers.utils import ArtifactVerificationResult

log = logging.getLogger(__name__)


class ArtifactSemanticError(Exception):
    """Raised when an agent's artifact fails ArtifactVerifier checks after retries are exhausted."""

    def __init__(self, agent_name: str, issues: list[dict[str, Any]], raw_output: str):
        self.agent_name = agent_name
        self.issues = issues
        self.raw_output = raw_output
        preview = json.dumps(issues, ensure_ascii=False)[:500]
        super().__init__(f"{agent_name} artifact verification failed: {preview}")


class ArtifactVerifier:
    def __init__(self, source_reader: Reader):
        self.source_reader = source_reader

    def verify(
        self,
        artifact: str | dict[str, Any],
        allowed_files: list[str],
    ) -> dict[str, Any]:
        payload = self._load_artifact(artifact)
        allowed_files_set = set(allowed_files)

        issues = []
        checked_evidence_count = 0

        for path, label, evidence in self._iter_evidence(payload):
            checked_evidence_count += 1

            evidence_issues = self._verify_evidence(
                path=path,
                label=label,
                evidence=evidence,
                allowed_files=allowed_files_set,
            )
            issues.extend(evidence_issues)

        result = ArtifactVerificationResult(
            valid=not any(issue["severity"] == "error" for issue in issues),
            checked_evidence_count=checked_evidence_count,
            issues=issues,
        )

        return result.to_dict()

    def _load_artifact(self, artifact: str | dict[str, Any]) -> dict[str, Any]:
        if isinstance(artifact, dict):
            return artifact

        if isinstance(artifact, str):
            try:
                payload = json.loads(artifact)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Artifact must be a JSON object string. "
                    f"Invalid JSON at line {exc.lineno}, col {exc.colno}: {exc.msg}"
                ) from exc

            if not isinstance(payload, dict):
                raise ValueError(
                    f"Artifact must decode to a JSON object, got {type(payload).__name__}"
                )

            return payload

        raise TypeError(f"Unsupported artifact type: {type(artifact)}")

    def _iter_evidence(self, value: Any, path: str = "$"):
        if isinstance(value, dict):
            if "evidence" in value and isinstance(value["evidence"], dict):
                yield path, value.get("label"), value["evidence"]

            for key, child in value.items():
                yield from self._iter_evidence(child, f"{path}.{key}")

        elif isinstance(value, list):
            for index, child in enumerate(value):
                yield from self._iter_evidence(child, f"{path}[{index}]")

    def _verify_evidence(
        self,
        path: str,
        label: str | None,
        evidence: dict[str, Any],
        allowed_files: set[str],
    ) -> list[dict[str, Any]]:
        issues = []

        file_path = evidence.get("file")
        line_start = evidence.get("line_start")
        line_end = evidence.get("line_end")
        excerpt = evidence.get("excerpt")

        if label == "missing" and file_path is None:
            if line_start not in (None, 0) or line_end not in (None, 0):
                issues.append(
                    self._issue(
                        severity="error",
                        path=path,
                        message=(
                            "Missing findings without file evidence must use null "
                            "or 0 for line_start and line_end."
                        ),
                        evidence=evidence,
                    )
                )
            if not isinstance(excerpt, str) or not excerpt.strip():
                issues.append(
                    self._issue(
                        severity="warning",
                        path=path,
                        message="Missing finding has no explanatory evidence excerpt.",
                        evidence=evidence,
                    )
                )
            return issues

        if file_path is None:
            issues.append(
                self._issue(
                    severity="error",
                    path=path,
                    message="Evidence file is required for non-missing findings.",
                    evidence=evidence,
                )
            )
            return issues

        if not isinstance(file_path, str) or not file_path.strip():
            issues.append(
                self._issue(
                    severity="error",
                    path=path,
                    message="Evidence file must be a non-empty string.",
                    evidence=evidence,
                )
            )
            return issues

        if file_path not in allowed_files:
            issues.append(
                self._issue(
                    severity="error",
                    path=path,
                    message=f"Evidence file is not in allowed files: {file_path}",
                    evidence=evidence,
                )
            )
            return issues

        if not self.source_reader.file_exists(file_path):
            issues.append(
                self._issue(
                    severity="error",
                    path=path,
                    message=f"Evidence file does not exist: {file_path}",
                    evidence=evidence,
                )
            )
            return issues

        if not isinstance(line_start, int):
            issues.append(
                self._issue(
                    severity="error",
                    path=path,
                    message="Evidence line_start must be an integer.",
                    evidence=evidence,
                )
            )

        if not isinstance(line_end, int):
            issues.append(
                self._issue(
                    severity="error",
                    path=path,
                    message="Evidence line_end must be an integer.",
                    evidence=evidence,
                )
            )

        if not isinstance(excerpt, str):
            issues.append(
                self._issue(
                    severity="error",
                    path=path,
                    message="Evidence excerpt must be a string.",
                    evidence=evidence,
                )
            )

        if issues:
            return issues

        if line_start < 1:
            issues.append(
                self._issue(
                    severity="error",
                    path=path,
                    message="Evidence line_start must be >= 1.",
                    evidence=evidence,
                )
            )

        if line_end < line_start:
            issues.append(
                self._issue(
                    severity="error",
                    path=path,
                    message="Evidence line_end must be >= line_start.",
                    evidence=evidence,
                )
            )

        if issues:
            return issues

        content = self.source_reader.read_file(file_path)
        lines = content.splitlines()

        if line_start > len(lines):
            issues.append(
                self._issue(
                    severity="error",
                    path=path,
                    message=(
                        f"Evidence line_start {line_start} is outside file range. "
                        f"File has {len(lines)} lines."
                    ),
                    evidence=evidence,
                )
            )
            return issues

        if line_end > len(lines):
            issues.append(
                self._issue(
                    severity="error",
                    path=path,
                    message=(
                        f"Evidence line_end {line_end} is outside file range. "
                        f"File has {len(lines)} lines."
                    ),
                    evidence=evidence,
                )
            )
            return issues

        selected_text = "\n".join(lines[line_start - 1 : line_end]).strip()
        expected_excerpt = excerpt.strip()

        normalized_selected_text = self._normalize_excerpt(selected_text)
        normalized_expected_excerpt = self._normalize_excerpt(expected_excerpt)

        if (
            normalized_expected_excerpt
            and normalized_expected_excerpt not in normalized_selected_text
        ):
            issues.append(
                self._issue(
                    severity="error",
                    path=path,
                    message=(
                        "Evidence excerpt does not match the referenced line range. "
                        f"Expected excerpt: {expected_excerpt!r}. "
                        f"Actual text: {selected_text!r}."
                    ),
                    evidence=evidence,
                )
            )

        if not normalized_expected_excerpt:
            issues.append(
                self._issue(
                    severity="warning",
                    path=path,
                    message="Evidence excerpt is empty.",
                    evidence=evidence,
                )
            )

        return issues

    def _normalize_excerpt(self, text: str) -> str:
        normalized_lines = []

        for line in text.splitlines():
            stripped = line.strip()

            # Handles "34: content"
            if ": " in stripped:
                prefix, rest = stripped.split(": ", maxsplit=1)
                if prefix.isdigit():
                    stripped = rest

            # Handles accidental duplicated numbering like "6: 6: content"
            if ": " in stripped:
                prefix, rest = stripped.split(": ", maxsplit=1)
                if prefix.isdigit():
                    stripped = rest

            normalized_lines.append(stripped)

        return "\n".join(normalized_lines).strip()

    def _issue(
        self,
        severity: str,
        path: str,
        message: str,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "severity": severity,
            "path": path,
            "message": message,
            "evidence": evidence,
        }

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CleanDiagnostic:
    """A stable, machine-readable Clean validation failure."""

    stage: str
    check_id: str
    message: str
    paths: tuple[str, ...] = ()
    severity: str = "error"
    contract_ids: tuple[str, ...] = ()
    requirement_ids: tuple[str, ...] = ()
    expected: str | None = None
    actual: str | None = None
    repair_hint: str | None = None

    @property
    def diagnostic_id(self) -> str:
        return f"D-{self.fingerprint[:12].upper()}"

    @property
    def fingerprint(self) -> str:
        return _hash(self._identity())

    def to_dict(self) -> dict[str, Any]:
        return {
            "diagnostic_id": self.diagnostic_id,
            "stage": self.stage,
            "check_id": self.check_id,
            "severity": self.severity,
            "message": self.message,
            "paths": list(self.paths),
            "contract_ids": list(self.contract_ids),
            "requirement_ids": list(self.requirement_ids),
            "expected": self.expected,
            "actual": self.actual,
            "repair_hint": self.repair_hint,
        }

    def _identity(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "check_id": self.check_id,
            "severity": self.severity,
            "message": self.message,
            "paths": sorted(self.paths),
            "contract_ids": sorted(self.contract_ids),
            "requirement_ids": sorted(self.requirement_ids),
            "expected": self.expected,
            "actual": self.actual,
            "repair_hint": self.repair_hint,
        }


def failure_fingerprint(checks: Iterable[Mapping[str, Any]]) -> str:
    """Return a stable fingerprint for the complete current failure set."""
    identities: list[dict[str, Any]] = []
    for check in checks:
        if check.get("status") != "fail":
            continue
        diagnostics = check.get("diagnostics") or []
        if diagnostics:
            identities.extend(_diagnostic_identity(item) for item in diagnostics)
            continue
        identities.append(
            {
                "stage": "legacy",
                "check_id": str(check.get("name", "unknown")),
                "severity": "error",
                "message": str(check.get("message", "")),
                "paths": sorted(str(path) for path in check.get("paths", [])),
                "contract_ids": [],
                "requirement_ids": [],
                "expected": None,
                "actual": None,
                "repair_hint": None,
            }
        )
    return _hash(sorted(identities, key=_canonical))


def content_hashes(contents: Mapping[str, str]) -> dict[str, str]:
    return {
        path: hashlib.sha256(content.encode("utf-8")).hexdigest()
        for path, content in sorted(contents.items())
    }


def _diagnostic_identity(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "stage": str(value.get("stage", "unknown")),
        "check_id": str(value.get("check_id", "unknown")),
        "severity": str(value.get("severity", "error")),
        "message": str(value.get("message", "")),
        "paths": sorted(str(path) for path in value.get("paths", [])),
        "contract_ids": sorted(str(item) for item in value.get("contract_ids", [])),
        "requirement_ids": sorted(
            str(item) for item in value.get("requirement_ids", [])
        ),
        "expected": value.get("expected"),
        "actual": value.get("actual"),
        "repair_hint": value.get("repair_hint"),
    }


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

"""Pass/fail evaluation of crossing artifacts.

Scanners propose findings. Hard findings fail immediately. Soft findings
(DESCRIPTIVE / UNCERTAIN only) may be handed to an LLM adjudicator. Without an
adjudicator they fail — doubt never silently clears a flag.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from packages.modules.boundary import (
    BORDER_REVIEW,
    DESCRIPTIVE,
    LIFTED,
    UNCERTAIN,
    AliasMap,
    ResidualFinding,
    scan_content_leaks,
    scan_residual_originals,
)

log = logging.getLogger(__name__)

STRICT_POLICY = "strict-q3"
ADJUDICATED_POLICY = "llm-adjudicated-q3"
STATUS_PASS = "pass"
STATUS_FAIL = "fail"
DECISION_FAIL = "fail"
DECISION_DISMISS = "dismiss"

#: Shape kinds that are never soft — a path or URL is a leak regardless of reading.
_HARD_KINDS = frozenset(
    {
        "URL",
        "file:line reference",
        "source-like path",
        LIFTED,
    }
)

#: Soft only when every occurrence reading is in this set.
_SOFT_CLASSIFICATIONS = frozenset({DESCRIPTIVE, UNCERTAIN})


class BorderGateError(Exception):
    """Raised when Border refuses to let an artifact cross."""

    def __init__(self, verdict: BorderVerdict):
        self.verdict = verdict
        count = verdict.finding_count
        artifacts = ", ".join(verdict.failed_artifacts) or (
            verdict.artifacts_reviewed[0] if verdict.artifacts_reviewed else "<unknown>"
        )
        super().__init__(
            f"Border refused {count} finding(s) in {artifacts}. "
            "See border_verdict.json; Clean must not receive these artifacts."
        )


@dataclass(frozen=True)
class BorderFindingRecord:
    """One scanner finding, ready for adjudication and storage."""

    artifact: str
    original: str
    alias: str | None
    kind: str
    classifications: tuple[str, ...]
    occurrence_count: int
    decision: str
    summary: str
    examples: tuple[str, ...] = ()
    finding_id: str = ""
    rationale: str | None = None
    soft: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        # `soft` is an internal routing flag — keep the stored verdict focused on the
        # decision the Clean team / operator needs.
        payload.pop("soft", None)
        return payload


@dataclass
class BorderVerdict:
    """Border's decision for one pipeline run's crossing surfaces."""

    status: str
    policy: str
    artifacts_reviewed: list[str] = field(default_factory=list)
    finding_count: int = 0
    findings: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.status == STATUS_PASS

    @property
    def failed_artifacts(self) -> list[str]:
        return sorted(
            {
                item["artifact"]
                for item in self.findings
                if item.get("decision") == DECISION_FAIL
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "policy": self.policy,
            "artifacts_reviewed": list(self.artifacts_reviewed),
            "finding_count": self.finding_count,
            "findings": list(self.findings),
            "notes": list(self.notes),
            "failed_artifacts": self.failed_artifacts,
        }


AdjudicateFn = Callable[[list[BorderFindingRecord]], list[BorderFindingRecord]]


def strip_border_review_section(markdown: str) -> str:
    """Remove Dirty's advisory appendix so Border judges the body alone."""
    marker = f"## {BORDER_REVIEW}"
    if marker not in markdown:
        return markdown
    return markdown.split(marker, 1)[0].rstrip()


def is_soft_finding(record: BorderFindingRecord) -> bool:
    """Whether this finding may be dismissed by the LLM adjudicator."""
    if record.kind in _HARD_KINDS:
        return False
    if not record.classifications:
        return False
    # Soft only when every reading is DESCRIPTIVE or UNCERTAIN (including uncertain
    # command-shaped prose). VERBATIM / NAME-SHAPED anywhere → hard fail.
    return all(c in _SOFT_CLASSIFICATIONS for c in record.classifications)


def review_text(
    artifact: str,
    text: str,
    alias_map: AliasMap,
    source_texts: tuple[str, ...] | list[str] = (),
    *,
    include_content_scan: bool = True,
) -> list[BorderFindingRecord]:
    """Scan one crossing text into provisional finding records (all decision=fail)."""
    findings = scan_residual_originals(text, alias_map)
    if include_content_scan:
        findings += scan_content_leaks(text, source_texts)
    return [_to_record(artifact, finding) for finding in findings]


def evaluate_crossing_artifacts(
    *,
    alias_map: AliasMap,
    specification: str | None = None,
    plans: dict[str, str] | None = None,
    evidence_catalogue: dict[str, Any] | str | None = None,
    neutral_manifest: dict[str, Any] | str | None = None,
    source_texts: tuple[str, ...] | list[str] = (),
    policy: str | None = None,
    adjudicate: AdjudicateFn | None = None,
) -> BorderVerdict:
    """Review every crossing surface Dirty hands toward Clean.

    When `adjudicate` is supplied, soft findings are passed through it. Hard findings
    never reach the model. Without an adjudicator, soft findings fail (strict).
    """
    records: list[BorderFindingRecord] = []
    reviewed: list[str] = []
    notes: list[str] = []

    if specification is not None:
        reviewed.append("specification.md")
        body = strip_border_review_section(specification)
        records.extend(review_text("specification.md", body, alias_map, source_texts))

    for name, plan_text in sorted((plans or {}).items()):
        reviewed.append(name)
        records.extend(
            review_text(name, plan_text, alias_map, source_texts, include_content_scan=True)
        )

    if evidence_catalogue is not None:
        reviewed.append("evidence_catalogue.json")
        catalogue = _as_mapping(evidence_catalogue)
        catalogue_text = json.dumps(catalogue, ensure_ascii=False)
        records.extend(
            review_text(
                "evidence_catalogue.json",
                catalogue_text,
                alias_map,
                source_texts=(),
                include_content_scan=True,
            )
        )
        for note in catalogue.get("border_review") or []:
            if isinstance(note, str) and note.strip():
                notes.append(f"catalogue note: {note.strip()}")

    if neutral_manifest is not None:
        reviewed.append("neutral_manifest.json")
        manifest_text = (
            neutral_manifest
            if isinstance(neutral_manifest, str)
            else json.dumps(neutral_manifest, ensure_ascii=False)
        )
        records.extend(
            review_text(
                "neutral_manifest.json",
                manifest_text,
                alias_map,
                source_texts=(),
                include_content_scan=True,
            )
        )

    numbered = _assign_ids(records)
    hard = [r for r in numbered if not r.soft]
    soft = [r for r in numbered if r.soft]

    resolved_policy = policy or (ADJUDICATED_POLICY if adjudicate else STRICT_POLICY)

    if soft and adjudicate is not None:
        try:
            soft = adjudicate(soft)
        except Exception as exc:
            log.error("Border adjudicator failed (%s); failing all soft findings", type(exc).__name__)
            soft = [
                replace(
                    record,
                    decision=DECISION_FAIL,
                    rationale=f"adjudicator error: {type(exc).__name__}",
                    summary=record.summary.replace(" [pending]", " [enforced]"),
                )
                for record in soft
            ]
        soft = _coerce_adjudicated(soft)
    elif soft:
        soft = [
            replace(
                record,
                decision=DECISION_FAIL,
                rationale="no adjudicator; strict policy fails soft findings",
                summary=record.summary.replace(" [pending]", " [enforced]"),
            )
            for record in soft
        ]

    final = list(hard) + list(soft)
    failing = [r for r in final if r.decision == DECISION_FAIL]
    for record in soft:
        if record.decision == DECISION_DISMISS and record.rationale:
            notes.append(
                f"dismissed {record.finding_id} ({record.original!r}): {record.rationale}"
            )

    finding_dicts = [record.to_dict() for record in final]
    status = STATUS_FAIL if failing else STATUS_PASS
    verdict = BorderVerdict(
        status=status,
        policy=resolved_policy,
        artifacts_reviewed=reviewed,
        finding_count=len(failing),
        findings=finding_dicts,
        notes=notes,
    )
    if verdict.passed:
        log.info(
            "Border passed %d crossing artifact(s) under %s (%d dismissed)",
            len(reviewed),
            resolved_policy,
            sum(1 for r in soft if r.decision == DECISION_DISMISS),
        )
    else:
        log.error(
            "Border failed %d finding(s) across %s",
            verdict.finding_count,
            ", ".join(verdict.failed_artifacts),
        )
    return verdict


def _assign_ids(records: list[BorderFindingRecord]) -> list[BorderFindingRecord]:
    numbered: list[BorderFindingRecord] = []
    for index, record in enumerate(records, start=1):
        soft = is_soft_finding(record)
        summary = record.summary
        if soft:
            summary = summary.replace(" [enforced]", " [pending]")
        numbered.append(
            replace(
                record,
                finding_id=f"BF-{index:03d}",
                soft=soft,
                summary=summary,
            )
        )
    return numbered


def _coerce_adjudicated(records: list[BorderFindingRecord]) -> list[BorderFindingRecord]:
    """Any soft finding without a clear dismiss stays a fail."""
    coerced: list[BorderFindingRecord] = []
    for record in records:
        decision = record.decision if record.decision in {DECISION_FAIL, DECISION_DISMISS} else DECISION_FAIL
        if decision == DECISION_DISMISS:
            summary = record.summary.replace(" [pending]", " [dismissed]").replace(
                " [enforced]", " [dismissed]"
            )
        else:
            summary = record.summary.replace(" [pending]", " [enforced]")
        coerced.append(
            replace(
                record,
                decision=decision,
                summary=summary,
                rationale=record.rationale or (
                    "adjudicator did not dismiss" if decision == DECISION_FAIL else record.rationale
                ),
            )
        )
    return coerced


def _to_record(artifact: str, finding: ResidualFinding) -> BorderFindingRecord:
    classifications = tuple(dict.fromkeys(o.classification for o in finding.occurrences))
    examples = tuple(o.phrase for o in finding.occurrences[:3])
    summary = finding.summary.replace(" [advisory]", " [enforced]")
    return BorderFindingRecord(
        artifact=artifact,
        original=finding.original,
        alias=finding.alias,
        kind=finding.kind,
        classifications=classifications,
        occurrence_count=len(finding.occurrences),
        decision=DECISION_FAIL,
        summary=summary,
        examples=examples,
    )


def _as_mapping(value: dict[str, Any] | str) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}

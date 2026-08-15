"""Border: the enforcement gate between Dirty and Clean.

Dirty detects and annotates. Border decides — scanners propose findings, an LLM
adjudicates soft ones, hard leaks fail immediately. A crossing artifact that still
carries anything from the original does not pass.
"""

from packages.modules.border.corpus import evidence_excerpts
from packages.modules.border.gate import (
    ADJUDICATED_POLICY,
    DECISION_DISMISS,
    DECISION_FAIL,
    STRICT_POLICY,
    BorderGateError,
    BorderVerdict,
    evaluate_crossing_artifacts,
    is_soft_finding,
    review_text,
    strip_border_review_section,
)

__all__ = [
    "ADJUDICATED_POLICY",
    "DECISION_DISMISS",
    "DECISION_FAIL",
    "STRICT_POLICY",
    "BorderGateError",
    "BorderVerdict",
    "evaluate_crossing_artifacts",
    "evidence_excerpts",
    "is_soft_finding",
    "review_text",
    "strip_border_review_section",
]

"""The clean-room boundary layer (CLAUDE.md section 2).

Holds the private original -> neutral map and builds the neutral artifacts that cross
to the clean team. Detection of residual leaks is advisory here; enforcement lives in
`packages.modules.border` / the Border team.
"""

from packages.modules.boundary.alias_map import (
    DEFAULT_PROJECT_NAME,
    MIN_IDENTIFIER_LENGTH,
    UNIVERSAL_IDENTIFIERS,
    AliasMap,
    is_registrable_identifier,
)
from packages.modules.boundary.neutralise import (
    BORDER_REVIEW,
    COMMAND,
    DESCRIPTIVE,
    FOREIGN,
    LIFTED,
    NAME_SHAPED,
    UNCERTAIN,
    VERBATIM,
    Occurrence,
    ResidualFinding,
    annotate_border_review,
    build_evidence_catalogue,
    classify_occurrence,
    evidence_catalogue,
    find_residual_originals,
    mint_evidence_ids,
    neutral_evidence_reference,
    neutral_manifest,
    neutral_report,
    register_code_identifiers,
    scan_content_leaks,
    scan_residual_originals,
    scrub_identifiers,
)

__all__ = [
    "BORDER_REVIEW",
    "COMMAND",
    "DEFAULT_PROJECT_NAME",
    "DESCRIPTIVE",
    "FOREIGN",
    "LIFTED",
    "MIN_IDENTIFIER_LENGTH",
    "NAME_SHAPED",
    "UNCERTAIN",
    "UNIVERSAL_IDENTIFIERS",
    "VERBATIM",
    "AliasMap",
    "Occurrence",
    "ResidualFinding",
    "annotate_border_review",
    "build_evidence_catalogue",
    "classify_occurrence",
    "evidence_catalogue",
    "find_residual_originals",
    "is_registrable_identifier",
    "mint_evidence_ids",
    "neutral_evidence_reference",
    "neutral_manifest",
    "neutral_report",
    "register_code_identifiers",
    "scan_content_leaks",
    "scan_residual_originals",
    "scrub_identifiers",
]

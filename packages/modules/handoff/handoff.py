from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from marshmallow import ValidationError

from packages.modules.border import BorderVerdict, strip_border_review_section
from packages.modules.supervising.schemas import CleanHandoffSchema

HANDOFF_FILES = frozenset({"handoff.json", "specification.md"})
COMPATIBILITY_MODES = frozenset({"drop-in", "renamed"})


class HandoffError(Exception):
    """The approved Clean input is absent, modified, or unsafe."""


@dataclass(frozen=True)
class CleanHandoff:
    specification: str
    specification_sha256: str
    compatibility_mode: str


def export_clean_handoff(
    output_dir: str | Path,
    specification: str,
    verdict: BorderVerdict | dict[str, Any],
    *,
    compatibility_mode: str = "renamed",
) -> Path:
    """Write exactly the approved specification and its integrity manifest."""
    status = verdict.status if isinstance(verdict, BorderVerdict) else verdict.get("status")
    if status != "pass":
        raise HandoffError("Clean handoff requires a passing Border verdict")
    if compatibility_mode not in COMPATIBILITY_MODES:
        raise HandoffError(
            f"Compatibility mode must be one of {sorted(COMPATIBILITY_MODES)}"
        )

    root = Path(output_dir)
    _require_new_or_empty_directory(root)
    body = strip_border_review_section(specification).strip() + "\n"
    if not body.startswith("# "):
        raise HandoffError("Approved specification must be raw Markdown")
    encoded = body.encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    manifest = {
        "schema_version": 2,
        "approval": "border-passed",
        "compatibility_mode": compatibility_mode,
        "specification_file": "specification.md",
        "specification_sha256": digest,
    }
    CleanHandoffSchema().load(manifest)
    root.mkdir(parents=True, exist_ok=True)
    _atomic_write(root / "specification.md", encoded)
    _atomic_write(
        root / "handoff.json",
        (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    return root


def load_clean_handoff(handoff_dir: str | Path) -> CleanHandoff:
    """Load only a minimal handoff and verify path safety plus the specification hash."""
    root = Path(handoff_dir)
    if not root.is_dir() or root.is_symlink():
        raise HandoffError("Handoff path must be a real directory, not a symlink")
    extras = {item.name for item in root.iterdir()} - HANDOFF_FILES
    missing = HANDOFF_FILES - {item.name for item in root.iterdir()}
    if extras or missing:
        raise HandoffError(
            f"Handoff must contain exactly {sorted(HANDOFF_FILES)}; "
            f"extra={sorted(extras)}, missing={sorted(missing)}"
        )
    for name in HANDOFF_FILES:
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise HandoffError(f"Handoff member {name!r} must be a regular file")

    try:
        manifest = CleanHandoffSchema().load(
            json.loads((root / "handoff.json").read_text(encoding="utf-8"))
        )
        encoded = (root / manifest["specification_file"]).read_bytes()
        specification = encoded.decode("utf-8")
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise HandoffError(f"Invalid Clean handoff: {exc}") from exc
    actual = hashlib.sha256(encoded).hexdigest()
    if actual != manifest["specification_sha256"]:
        raise HandoffError("Handoff specification hash does not match handoff.json")
    if not specification.startswith("# "):
        raise HandoffError("Handoff specification is not Markdown")
    # Deliberately do not retain the directory Path: its parent can contain Dirty-only
    # artifacts. Past this loader, the Clean controller gets text plus an integrity hash.
    return CleanHandoff(
        specification=specification,
        specification_sha256=actual,
        compatibility_mode=manifest.get("compatibility_mode", "renamed"),
    )


def _require_new_or_empty_directory(root: Path) -> None:
    if root.exists():
        if root.is_symlink() or not root.is_dir():
            raise HandoffError("Handoff output must be a directory, not a file or symlink")
        if any(root.iterdir()):
            raise HandoffError("Handoff output directory must be empty")


def _atomic_write(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()

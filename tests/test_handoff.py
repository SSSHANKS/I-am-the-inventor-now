import json

import pytest

from packages.modules.handoff import HandoffError, export_clean_handoff, load_clean_handoff

SPECIFICATION = """# Reconstructed Service

## Functional Requirements

- FR-001: Return a greeting.

## BORDER-REVIEW

- BORDER-REVIEW: Dirty-side advisory text.
"""


def test_export_and_load_minimal_approved_handoff(tmp_path):
    root = export_clean_handoff(tmp_path / "handoff", SPECIFICATION, {"status": "pass"})

    assert {item.name for item in root.iterdir()} == {"handoff.json", "specification.md"}
    assert "BORDER-REVIEW" not in (root / "specification.md").read_text(encoding="utf-8")
    loaded = load_clean_handoff(root)
    manifest = json.loads((root / "handoff.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 2
    assert manifest["compatibility_mode"] == "renamed"
    assert loaded.specification_sha256 == manifest["specification_sha256"]
    assert loaded.compatibility_mode == "renamed"
    assert not hasattr(loaded, "root")


def test_failed_border_cannot_export_handoff(tmp_path):
    with pytest.raises(HandoffError, match="passing Border"):
        export_clean_handoff(tmp_path / "handoff", SPECIFICATION, {"status": "fail"})
    assert not (tmp_path / "handoff").exists()


def test_inline_border_review_marker_cannot_cross_even_with_pass_verdict(tmp_path):
    contaminated = "# Specification\n\nBORDER-REVIEW: unresolved wording.\n"

    with pytest.raises(HandoffError, match="still contains"):
        export_clean_handoff(
            tmp_path / "handoff", contaminated, {"status": "pass"}
        )

    assert not (tmp_path / "handoff").exists()


def test_modified_or_expanded_handoff_is_rejected(tmp_path):
    root = export_clean_handoff(tmp_path / "handoff", SPECIFICATION, {"status": "pass"})
    (root / "specification.md").write_text("# Modified\n", encoding="utf-8")
    with pytest.raises(HandoffError, match="hash"):
        load_clean_handoff(root)

    (root / "specification.md").write_text("# Reconstructed Service\n", encoding="utf-8")
    (root / "dirty-index.json").write_text("{}", encoding="utf-8")
    with pytest.raises(HandoffError, match="exactly"):
        load_clean_handoff(root)


def test_drop_in_compatibility_mode_round_trips(tmp_path):
    root = export_clean_handoff(
        tmp_path / "handoff",
        SPECIFICATION,
        {"status": "pass"},
        compatibility_mode="drop-in",
    )

    loaded = load_clean_handoff(root)

    assert loaded.compatibility_mode == "drop-in"


def test_invalid_compatibility_mode_is_rejected_before_writing(tmp_path):
    output = tmp_path / "handoff"

    with pytest.raises(HandoffError, match="Compatibility mode"):
        export_clean_handoff(
            output,
            SPECIFICATION,
            {"status": "pass"},
            compatibility_mode="unknown",
        )

    assert not output.exists()


def test_version_one_handoff_loads_as_renamed_for_backward_compatibility(tmp_path):
    root = export_clean_handoff(tmp_path / "handoff", SPECIFICATION, {"status": "pass"})
    manifest_path = root / "handoff.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = 1
    manifest.pop("compatibility_mode")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    loaded = load_clean_handoff(root)

    assert loaded.compatibility_mode == "renamed"


def test_version_two_handoff_requires_an_explicit_compatibility_mode(tmp_path):
    root = export_clean_handoff(tmp_path / "handoff", SPECIFICATION, {"status": "pass"})
    manifest_path = root / "handoff.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("compatibility_mode")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(HandoffError, match="compatibility_mode"):
        load_clean_handoff(root)

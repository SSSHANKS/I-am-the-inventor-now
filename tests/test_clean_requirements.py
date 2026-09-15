import pytest

from packages.modules.clean.requirements import (
    RequirementCatalogueError,
    prepare_specification,
)


def test_assigns_stable_ids_to_standard_requirement_sections():
    specification = """# Project

## Functional Requirements

- Return a greeting.
- Preserve an explicit value.

## Error Handling

- Raise an error for invalid input.

## Acceptance Criteria

- The greeting is observable.

## Test Candidates

- Verify the greeting.

## Evidence References

- EV-001 supports the behavior.
"""

    first = prepare_specification(specification)
    second = prepare_specification(specification)

    assert first == second
    assert "- FR-001: Return a greeting." in first.text
    assert "- FR-002: Preserve an explicit value." in first.text
    assert "- EH-001: Raise an error for invalid input." in first.text
    assert "- AC-001: The greeting is observable." in first.text
    assert "- TC-001: Verify the greeting." in first.text
    assert "- EV-001 supports the behavior." in first.text
    assert first.requirement_ids == (
        "AC-001",
        "EH-001",
        "FR-001",
        "FR-002",
        "TC-001",
    )
    statements = {
        item["requirement_id"]: item["statement"]
        for item in first.audit_record()["requirement_statements"]
    }
    assert statements["FR-001"] == "FR-001: Return a greeting."
    assert statements["TC-001"] == "TC-001: Verify the greeting."


def test_preserves_existing_ids_and_avoids_number_collisions():
    specification = """## Functional Requirements

- FR-002: Keep this identifier.
- Add another operation.
"""

    prepared = prepare_specification(specification)

    assert "- FR-002: Keep this identifier." in prepared.text
    assert "- FR-003: Add another operation." in prepared.text
    assert [label.requirement_id for label in prepared.generated_labels] == ["FR-003"]


def test_labels_prose_when_a_requirement_section_has_no_list_items():
    prepared = prepare_specification(
        """## Behavioral Requirements

The operation preserves the supplied value.
"""
    )

    assert "BR-001: The operation preserves the supplied value." in prepared.text


def test_does_not_number_precondition_elaborations_when_primary_requirements_exist():
    specification = """## Functional Requirements

- Perform the lookup operation.

### Operational Workflows and Preconditions/Postconditions

#### Lookup Workflow

- **Preconditions**: The requested name is supplied.
- **Postconditions**: The resolved value is returned.
"""

    prepared = prepare_specification(specification)

    assert prepared.requirement_ids == ("FR-001",)
    assert "- FR-001: Perform the lookup operation." in prepared.text
    assert "- **Preconditions**: The requested name is supplied." in prepared.text
    assert "- **Postconditions**: The resolved value is returned." in prepared.text


def test_numbers_preconditions_when_they_are_the_only_requirement_statements():
    specification = """## Functional Requirements

### Preconditions/Postconditions

- **Preconditions**: The requested name is supplied.
- **Postconditions**: The resolved value is returned.
"""

    prepared = prepare_specification(specification)

    assert prepared.requirement_ids == ("FR-001", "FR-002")


def test_reference_heading_containing_category_words_does_not_create_requirements():
    specification = """## Error Handling

### Context Manager Exception Safety

Temporary connections are removed even if the managed block raises.

### Open Questions on Cleanup

Exact cleanup timing remains unknown.

## Evidence References - Garbage Collection and Error Handling

- EV-020: Evidence for exception cleanup.
"""

    prepared = prepare_specification(specification)

    assert prepared.requirement_ids == ("EH-001",)
    assert "EH-001: Temporary connections are removed" in prepared.text
    assert "Exact cleanup timing remains unknown." in prepared.text
    assert "- EV-020: Evidence for exception cleanup." in prepared.text


def test_gap_subsection_resets_inherited_requirement_category():
    specification = """## Functional Requirements

Provide stable lookup behavior.

### Gap: Namespace Collisions

Collision behavior requires further verification.
"""

    prepared = prepare_specification(specification)

    assert prepared.requirement_ids == ("FR-001",)
    assert "FR-001: Provide stable lookup behavior." in prepared.text
    assert "FR-002" not in prepared.text


def test_rejects_specification_without_traceable_requirements():
    with pytest.raises(RequirementCatalogueError, match="no formal requirement IDs"):
        prepare_specification("# Notes\n\nSome background text.\n")

import pytest

from packages.modules.clean.context import build_scoped_context
from packages.modules.clean.workspace import WorkspaceError

SPECIFICATION = """# Example Project

## Functional Requirements

- FR-001: Return a greeting.
  The greeting is stable.
- FR-002: Persist an audit record.
  The record contains a timestamp.

## Error Handling

- EH-001: Reject empty values.
"""


def _plan():
    return {
        "schema_version": 2,
        "summary": "Example project",
        "project_kind": "application",
        "runtime": {"language": "ExampleLang", "minimum_version": "1"},
        "dependencies": [],
        "packages": [],
        "entry_points": [{"path": "app.example", "description": "Application"}],
        "symbol_contracts": [
            {
                "symbol_id": "SYM-001",
                "qualified_name": "app.greet",
                "kind": "function",
                "signature": "greet()",
                "visibility": "public",
                "requirement_ids": ["FR-001"],
            },
            {
                "symbol_id": "SYM-002",
                "qualified_name": "audit.persist",
                "kind": "function",
                "signature": "persist(record)",
                "visibility": "public",
                "requirement_ids": ["FR-002"],
            },
        ],
        "files": [
            {
                "path": "app.example",
                "purpose": "Greeting entry point",
                "requirement_ids": ["FR-001"],
                "provides": ["SYM-001"],
                "requires": [],
                "depends_on": [],
                "generation_order": 1,
            },
            {
                "path": "audit.example",
                "purpose": "Audit persistence",
                "requirement_ids": ["FR-002"],
                "provides": ["SYM-002"],
                "requires": [],
                "depends_on": [],
                "generation_order": 2,
            },
        ],
        "validation_strategy": ["Run the configured adapter"],
        "open_questions": [
            "FR-002: Which timestamp precision is required?",
            "Which deployment environment is used?",
        ],
    }


def test_scoped_context_excludes_unrelated_requirements_and_files():
    context = build_scoped_context(SPECIFICATION, _plan(), ["app.example"])

    assert "FR-001: Return a greeting" in context.specification
    assert "The greeting is stable" in context.specification
    assert "FR-002" not in context.specification
    assert "EH-001" not in context.specification
    assert context.requirement_ids == ("FR-001",)
    assert context.symbol_ids == ("SYM-001",)
    assert [item["path"] for item in context.plan["files"]] == ["app.example"]
    assert [item["symbol_id"] for item in context.plan["symbol_contracts"]] == [
        "SYM-001"
    ]
    assert context.plan["requirement_contracts"] == [
        {
            "requirement_id": "FR-001",
            "statement": "FR-001: Return a greeting.",
            "implementation_paths": ["app.example"],
            "test_paths": [],
        }
    ]
    assert context.plan["entry_points"] == _plan()["entry_points"]
    assert context.plan["open_questions"] == [
        "Which deployment environment is used?"
    ]


def test_scoped_context_combines_requirements_for_related_files():
    context = build_scoped_context(
        SPECIFICATION,
        _plan(),
        ["app.example", "audit.example"],
    )

    assert context.requirement_ids == ("FR-001", "FR-002")
    assert context.symbol_ids == ("SYM-001", "SYM-002")
    assert "FR-001" in context.specification
    assert "FR-002" in context.specification
    assert "EH-001" not in context.specification


def test_implementation_context_includes_dependent_test_candidate_as_evidence():
    specification = SPECIFICATION + """
## Test Candidates

- TC-001: Invoke the greeting operation and inspect its stable output.
"""
    plan = _plan()
    plan["files"].append(
        {
            "path": "tests/test_greeting.py",
            "purpose": "Verify greeting output",
            "requirement_ids": ["FR-001", "TC-001"],
            "provides": [],
            "requires": ["SYM-001"],
            "depends_on": ["app.example"],
            "generation_order": 3,
        }
    )

    context = build_scoped_context(specification, plan, ["app.example"])

    assert context.requirement_ids == ("FR-001",)
    assert context.supporting_requirement_ids == ("TC-001",)
    assert "TC-001: Invoke the greeting operation" in context.specification
    assert context.plan["supporting_test_candidates"] == [
        {
            "requirement_id": "TC-001",
            "statement": (
                "TC-001: Invoke the greeting operation and inspect its stable output."
            ),
            "role": "read-only implementation design evidence",
        }
    ]
    assert [item["path"] for item in context.plan["files"]] == ["app.example"]


def test_implementation_context_includes_project_first_scenario_without_test_file():
    specification = SPECIFICATION + """
## Test Candidates

- TC-001: Invoke the greeting operation and inspect its stable output.
"""
    plan = _plan()
    plan["files"][0]["scenario_ids"] = ["TC-001"]

    context = build_scoped_context(specification, plan, ["app.example"])

    assert context.requirement_ids == ("FR-001",)
    assert context.supporting_requirement_ids == ("TC-001",)
    assert "TC-001: Invoke the greeting operation" in context.specification
    assert not any(item["path"].startswith("tests/") for item in context.plan["files"])
    assert context.plan["supporting_test_candidates"] == [
        {
            "requirement_id": "TC-001",
            "statement": (
                "TC-001: Invoke the greeting operation and inspect its stable output."
            ),
            "role": "read-only implementation design evidence",
        }
    ]


def test_scoped_context_rejects_missing_requirement_text():
    plan = _plan()
    plan["files"][0]["requirement_ids"] = ["FR-999"]

    with pytest.raises(WorkspaceError, match="could not find"):
        build_scoped_context(SPECIFICATION, plan, ["app.example"])


def test_scoped_context_enforces_prompt_limits(monkeypatch):
    monkeypatch.setattr(
        "packages.modules.clean.context.MAX_SCOPED_SPECIFICATION_BYTES",
        16,
    )

    with pytest.raises(WorkspaceError, match="128 KiB"):
        build_scoped_context(SPECIFICATION, _plan(), ["app.example"])


def test_scoped_context_enforces_plan_prompt_limit(monkeypatch):
    monkeypatch.setattr(
        "packages.modules.clean.context.MAX_SCOPED_PLAN_BYTES",
        16,
    )

    with pytest.raises(WorkspaceError, match="256 KiB"):
        build_scoped_context(SPECIFICATION, _plan(), ["app.example"])

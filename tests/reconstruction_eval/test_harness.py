from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from .harness import (
    EvaluationFixtureError,
    expectation_failures,
    load_evaluation_fixture,
    run_evaluation_fixture,
)

SPECIFICATION = """# Arithmetic Library

## Functional Requirements

- FR-001: Add two integers and return their sum.

## Acceptance Criteria

- AC-001: Adding two and three returns five.

## Test Candidates

- TC-001: Verify integer addition.
"""


def _plan() -> dict:
    return {
        "schema_version": 2,
        "summary": "A minimal arithmetic library.",
        "project_kind": "library",
        "runtime": {"language": "Python", "minimum_version": "3.12"},
        "dependencies": [],
        "packages": [],
        "entry_points": [{"path": "arithmetic.py", "description": "Public module"}],
        "symbol_contracts": [
            {
                "symbol_id": "SYM-001",
                "qualified_name": "arithmetic.add",
                "kind": "function",
                "signature": "add(left: int, right: int) -> int",
                "visibility": "public",
                "requirement_ids": ["FR-001", "AC-001"],
            }
        ],
        "files": [
            {
                "path": "arithmetic.py",
                "purpose": "Implement addition.",
                "requirement_ids": ["FR-001", "AC-001"],
                "provides": ["SYM-001"],
                "requires": [],
                "depends_on": [],
                "generation_order": 1,
            },
            {
                "path": "tests/test_arithmetic.py",
                "purpose": "Verify addition.",
                "requirement_ids": ["FR-001", "AC-001", "TC-001"],
                "provides": [],
                "requires": ["SYM-001"],
                "depends_on": ["arithmetic.py"],
                "generation_order": 2,
            },
        ],
        "validation_strategy": ["Import the module and run pytest."],
        "open_questions": [],
    }


def _write_fixture(root: Path, *, manifest_updates: dict | None = None) -> Path:
    root.mkdir()
    assets = root / "assets"
    assets.mkdir()
    (root / "specification.md").write_text(SPECIFICATION, encoding="utf-8")
    (root / "plan.json").write_text(json.dumps(_plan()), encoding="utf-8")
    (assets / "arithmetic.py.txt").write_text(
        "def add(left: int, right: int) -> int:\n    return left + right\n",
        encoding="utf-8",
    )
    (assets / "generated_test.py.txt").write_text(
        "from arithmetic import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    (assets / "hidden_test.py.txt").write_text(
        (
            "from arithmetic import add\n\n\n"
            "def test_hidden_negative_values():\n"
            "    assert add(-4, 1) == -3\n\n\n"
            "def test_hidden_identity():\n"
            "    assert add(7, 0) == 7\n"
        ),
        encoding="utf-8",
    )
    manifest = {
        "schema_version": 1,
        "name": "arithmetic-library",
        "archetype": "library",
        "compatibility_mode": "renamed",
        "specification_file": "specification.md",
        "plan_file": "plan.json",
        "generated_files": {
            "arithmetic.py": "assets/arithmetic.py.txt",
            "tests/test_arithmetic.py": "assets/generated_test.py.txt",
        },
        "hidden_tests": ["assets/hidden_test.py.txt"],
        "expected": {
            "status": "success",
            "validation_level": "executable",
            "requirement_ids": ["AC-001", "FR-001", "TC-001"],
            "minimum_generated_tests": 1,
            "minimum_hidden_tests": 2,
        },
    }
    if manifest_updates:
        manifest.update(manifest_updates)
    (root / "fixture.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def test_evaluation_fixture_runs_clean_and_an_independent_hidden_oracle(tmp_path):
    fixture = load_evaluation_fixture(_write_fixture(tmp_path / "fixture"))

    assert fixture.seeded_faults == ()

    result = run_evaluation_fixture(
        fixture,
        tmp_path / "run",
        python_executable=sys.executable,
    )

    assert expectation_failures(fixture, result.metrics) == ()
    assert result.metrics.generated_file_count == 2
    assert result.metrics.generated_test_file_count == 1
    assert result.metrics.hidden.tests_run == 2
    assert result.metrics.model_call_count == 3
    assert result.metrics.repair_rounds == 0
    assert all("test_hidden_negative_values" not in prompt for prompt in result.prompts)
    assert result.report_path.is_file()


def test_evaluation_fixture_rejects_unknown_manifest_fields(tmp_path):
    root = _write_fixture(tmp_path / "fixture", manifest_updates={"surprise": True})

    with pytest.raises(EvaluationFixtureError, match="unknown: surprise"):
        load_evaluation_fixture(root)


def test_evaluation_fixture_rejects_asset_traversal(tmp_path):
    root = _write_fixture(
        tmp_path / "fixture",
        manifest_updates={"specification_file": "../outside.md"},
    )

    with pytest.raises(EvaluationFixtureError, match="Unsafe evaluation asset path"):
        load_evaluation_fixture(root)


def test_evaluation_fixture_rejects_invalid_expected_requirement_ids(tmp_path):
    root = _write_fixture(tmp_path / "fixture")
    manifest_path = root / "fixture.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["expected"]["requirement_ids"] = ["EV-001"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(EvaluationFixtureError, match="Invalid expected requirement IDs"):
        load_evaluation_fixture(root)


def test_evaluation_fixture_v2_requires_explicit_seeded_faults(tmp_path):
    root = _write_fixture(tmp_path / "fixture")
    manifest_path = root / "fixture.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = 2
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(EvaluationFixtureError, match="missing: seeded_faults"):
        load_evaluation_fixture(root)

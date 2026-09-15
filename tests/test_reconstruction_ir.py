from copy import deepcopy

import pytest
from marshmallow import ValidationError

from packages.modules.reconstruction_ir import (
    ReconstructionIRError,
    reconstruction_ir_coverage,
    validate_reconstruction_ir,
)
from packages.modules.supervising.schemas import ReconstructionIRSchema


def _ir():
    return {
        "schema_version": 1,
        "project": {
            "kind": "library",
            "language": "Python",
            "runtime_version": "3.12",
            "build_system": "setuptools",
            "layout": "src",
        },
        "evidence": [
            {
                "evidence_id": "EV-001",
                "source_kind": "test",
                "summary": "A greeting test observes the returned name.",
                "location": "tests/test_greeting.py:10",
                "confidence": "high",
            }
        ],
        "requirements": [
            {
                "requirement_id": "FR-001",
                "category": "FR",
                "statement": "Return a greeting containing the supplied name.",
                "evidence_ids": ["EV-001"],
                "confidence": "high",
            }
        ],
        "modules": [
            {
                "module_id": "MOD-001",
                "neutral_name": "greeting",
                "kind": "module",
                "public": True,
                "evidence_ids": ["EV-001"],
            }
        ],
        "symbols": [
            {
                "symbol_id": "SYM-001",
                "module_id": "MOD-001",
                "neutral_name": "create_greeting",
                "kind": "function",
                "declaration": "create_greeting(name: str) -> str",
                "visibility": "public",
                "requirement_ids": ["FR-001"],
                "evidence_ids": ["EV-001"],
                "confidence": "high",
            }
        ],
        "behaviors": [
            {
                "behavior_id": "BEH-001",
                "kind": "return",
                "description": "The returned greeting contains the input name.",
                "requirement_ids": ["FR-001"],
                "subject_symbol_ids": ["SYM-001"],
                "preconditions": ["The name is non-empty."],
                "inputs": [{"name": "name", "value": "Ada"}],
                "observations": [
                    {
                        "observation_id": "OBS-001",
                        "kind": "contains",
                        "target": "return-value",
                        "value": "Ada",
                    }
                ],
                "evidence_ids": ["EV-001"],
                "confidence": "high",
            }
        ],
        "entry_points": [
            {
                "entry_point_id": "EP-001",
                "kind": "import",
                "neutral_name": "greeting.create_greeting",
                "module_id": "MOD-001",
                "symbol_ids": ["SYM-001"],
                "behavior_ids": ["BEH-001"],
                "evidence_ids": ["EV-001"],
            }
        ],
        "dependencies": [],
        "configurations": [],
        "packaging": {
            "distribution_kind": "library",
            "import_roots": ["greeting"],
            "build_system": "setuptools",
            "runtime_dependencies": [],
            "entry_point_ids": ["EP-001"],
            "evidence_ids": ["EV-001"],
        },
        "gaps": [],
    }


def test_reconstruction_ir_schema_and_semantic_validation_accept_complete_ir():
    ir = ReconstructionIRSchema().load(_ir())

    assert validate_reconstruction_ir(ir) is ir
    assert reconstruction_ir_coverage(ir) == {
        "requirement_count": 1,
        "mapped_requirement_count": 1,
        "observed_requirement_count": 1,
        "public_symbol_count": 1,
        "exercised_public_symbol_count": 1,
        "entry_point_count": 1,
        "exercised_entry_point_count": 1,
        "unmapped_requirement_ids": [],
        "unobserved_requirement_ids": [],
        "unexercised_public_symbol_ids": [],
        "unexercised_entry_point_ids": [],
        "gap_count": 0,
        "blocking_gap_count": 0,
    }


def test_reconstruction_ir_schema_rejects_unknown_fields():
    ir = _ir()
    ir["surprise"] = True

    with pytest.raises(ValidationError, match="Unknown field"):
        ReconstructionIRSchema().load(ir)


@pytest.mark.parametrize(
    "mutate, message",
    [
        (
            lambda ir: ir["symbols"][0].update(module_id="MOD-999"),
            "unknown module_id",
        ),
        (
            lambda ir: ir["requirements"][0].update(category="EH"),
            "has category",
        ),
        (
            lambda ir: ir["evidence"].append(deepcopy(ir["evidence"][0])),
            "repeats evidence",
        ),
        (
            lambda ir: ir["dependencies"].append(
                {
                    "dependency_id": "DEP-001",
                    "source_id": "SYM-001",
                    "target_id": "SYM-001",
                    "kind": "call",
                    "evidence_ids": ["EV-001"],
                }
            ),
            "references itself",
        ),
    ],
)
def test_reconstruction_ir_validation_rejects_inconsistent_references(mutate, message):
    ir = ReconstructionIRSchema().load(_ir())
    mutate(ir)

    with pytest.raises(ReconstructionIRError, match=message):
        validate_reconstruction_ir(ir)


def test_reconstruction_ir_requires_observable_coverage_or_explicit_gap():
    ir = ReconstructionIRSchema().load(_ir())
    ir["behaviors"] = []
    ir["entry_points"][0]["behavior_ids"] = []

    with pytest.raises(ReconstructionIRError, match="unobserved_requirement_ids=FR-001"):
        validate_reconstruction_ir(ir)

    ir["gaps"] = [
        {
            "gap_id": "GAP-001",
            "description": "No observable oracle survived analysis.",
            "impact": "blocking",
            "status": "unverifiable",
            "scope_ids": ["FR-001", "SYM-001", "EP-001"],
            "evidence_ids": ["EV-001"],
        }
    ]

    assert validate_reconstruction_ir(ir) is ir
    assert reconstruction_ir_coverage(ir)["blocking_gap_count"] == 1

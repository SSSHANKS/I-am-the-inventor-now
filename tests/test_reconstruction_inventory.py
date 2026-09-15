import json
from copy import deepcopy

from packages.modules.boundary import AliasMap
from packages.modules.reconstruction_ir import (
    build_dirty_inventory,
    build_planning_priority_batches,
)
from packages.modules.supervising.schemas import ReconstructionInventorySchema


def _index():
    return {
        "files_indexed": ["src/widget.py", "settings.json"],
        "files_skipped": [{"file": "src/native.xyz", "reason": "unsupported"}],
        "errors": [],
        "classes": [
            {
                "file": "src/widget.py",
                "name": "Widget",
                "qualified_name": "Widget",
                "bases": [],
                "line_start": 1,
                "line_end": 12,
                "evidence": {
                    "file": "src/widget.py",
                    "line_start": 1,
                    "line_end": 12,
                },
            }
        ],
        "functions": [
            {
                "file": "src/widget.py",
                "name": "render",
                "qualified_name": "Widget.render",
                "args": ["self", "value"],
                "line_start": 4,
                "line_end": 8,
                "evidence": {
                    "file": "src/widget.py",
                    "line_start": 4,
                    "line_end": 8,
                },
            },
            {
                "file": "src/widget.py",
                "name": "helper",
                "qualified_name": "helper",
                "args": [],
                "line_start": 15,
                "line_end": 16,
                "evidence": {
                    "file": "src/widget.py",
                    "line_start": 15,
                    "line_end": 16,
                },
            },
        ],
        "entrypoints": [],
        "imports": [],
        "configs": [
            {
                "file": "settings.json",
                "kind": "json",
                "evidence": {
                    "file": "settings.json",
                    "line_start": 1,
                    "line_end": 1,
                },
            }
        ],
    }


def test_dirty_inventory_is_complete_stable_and_schema_valid():
    index = _index()
    report = {
        "files_read": ["src/widget.py"],
        "symbols": [
            {
                "evidence": {
                    "file": "src/widget.py",
                    "line_start": 5,
                    "line_end": 6,
                }
            }
        ],
    }

    first = build_dirty_inventory(index, AliasMap(), code_facts_report=report)
    second = build_dirty_inventory(deepcopy(index), AliasMap(), code_facts_report=report)

    assert ReconstructionInventorySchema().load(first) == first
    assert first == second
    assert first["coverage"] == {
        "total_count": 5,
        "covered_count": 3,
        "uncovered_count": 2,
        "coverage_percent": 60.0,
        "by_kind": {
            "module": {"total": 1, "covered": 1, "uncovered": 0},
            "class": {"total": 1, "covered": 1, "uncovered": 0},
            "function": {"total": 2, "covered": 1, "uncovered": 1},
            "entry_point": {"total": 0, "covered": 0, "uncovered": 0},
            "configuration": {"total": 1, "covered": 0, "uncovered": 1},
        },
        "by_priority": {
            "critical": {"total": 0, "covered": 0, "uncovered": 0},
            "high": {"total": 4, "covered": 2, "uncovered": 2},
            "medium": {"total": 1, "covered": 1, "uncovered": 0},
            "low": {"total": 0, "covered": 0, "uncovered": 0},
        },
        "reconstruction_target_count": 4,
        "covered_reconstruction_target_count": 2,
        "uncovered_reconstruction_target_count": 2,
        "reconstruction_target_coverage_percent": 50.0,
        "uncovered_inventory_ids": [
            item["inventory_id"] for item in first["items"] if not item["covered_by"]
        ],
        "uncovered_reconstruction_target_ids": [
            item["inventory_id"]
            for item in first["items"]
            if item["reconstruction_target"] and not item["covered_by"]
        ],
        "issue_count": 1,
    }


def test_inventory_coverage_uses_overlapping_evidence_ranges():
    report = {
        "behaviors": [
            {
                "evidence": {
                    "file": "src/widget.py",
                    "line_start": 6,
                    "line_end": 10,
                }
            }
        ]
    }

    inventory = build_dirty_inventory(_index(), AliasMap(), behavior_report=report)
    covered = {
        item["qualified_name"]: item["covered_by"]
        for item in inventory["items"]
        if item["kind"] in {"class", "function"}
    }

    assert covered == {
        "Widget": ["behavior"],
        "Widget.render": ["behavior"],
        "helper": [],
    }


def test_inventory_accepts_agent_reports_as_json_text():
    report = {
        "files_read": ["src/widget.py"],
        "symbols": [
            {
                "evidence": {
                    "file": "src/widget.py",
                    "line_start": 4,
                    "line_end": 8,
                }
            }
        ],
    }

    decoded = build_dirty_inventory(_index(), AliasMap(), code_facts_report=report)
    encoded = build_dirty_inventory(
        _index(), AliasMap(), code_facts_report=json.dumps(report)
    )

    assert encoded == decoded


def test_empty_index_has_full_vacuous_coverage():
    inventory = build_dirty_inventory(
        {
            "files_indexed": [],
            "files_skipped": [],
            "errors": [],
            "classes": [],
            "functions": [],
            "entrypoints": [],
            "configs": [],
        },
        AliasMap(),
    )

    assert inventory["coverage"]["coverage_percent"] == 100.0
    assert ReconstructionInventorySchema().load(inventory) == inventory


def test_inventory_prioritizes_contracts_without_reconstructing_test_or_example_code():
    index = _index()
    index["files_indexed"].extend(
        ["tests/test_widget.py", "examples/widget_demo.py", "src/_private.py"]
    )
    index["functions"].extend(
        [
            {
                "file": "tests/test_widget.py",
                "name": "test_render",
                "qualified_name": "test_render",
                "args": [],
                "line_start": 1,
                "line_end": 2,
                "evidence": {
                    "file": "tests/test_widget.py",
                    "line_start": 1,
                    "line_end": 2,
                },
            },
            {
                "file": "examples/widget_demo.py",
                "name": "demo",
                "qualified_name": "demo",
                "args": [],
                "line_start": 1,
                "line_end": 2,
                "evidence": {
                    "file": "examples/widget_demo.py",
                    "line_start": 1,
                    "line_end": 2,
                },
            },
            {
                "file": "src/_private.py",
                "name": "_helper",
                "qualified_name": "_helper",
                "args": [],
                "line_start": 1,
                "line_end": 2,
                "evidence": {
                    "file": "src/_private.py",
                    "line_start": 1,
                    "line_end": 2,
                },
            },
        ]
    )

    inventory = build_dirty_inventory(index, AliasMap())
    classified = {
        item["qualified_name"]: (
            item["source_scope"],
            item["contract_role"],
            item["priority"],
            item["reconstruction_target"],
        )
        for item in inventory["items"]
        if item["qualified_name"] in {"test_render", "demo", "_helper"}
    }

    assert classified == {
        "test_render": ("test", "excluded", "low", False),
        "demo": ("example", "excluded", "low", False),
        "_helper": ("production", "internal", "low", False),
    }


def test_package_reexports_promote_symbols_from_private_modules():
    index = _index()
    index["files_indexed"].extend(["src/__init__.py", "src/_implementation.py"])
    index["imports"].append(
        {
            "file": "src/__init__.py",
            "kind": "from_import",
            "module": "._implementation",
            "name": "public_api",
            "alias": None,
        }
    )
    index["functions"].append(
        {
            "file": "src/_implementation.py",
            "name": "public_api",
            "qualified_name": "public_api",
            "args": [],
            "line_start": 1,
            "line_end": 2,
            "evidence": {
                "file": "src/_implementation.py",
                "line_start": 1,
                "line_end": 2,
            },
        }
    )

    inventory = build_dirty_inventory(index, AliasMap())
    exported = next(
        item for item in inventory["items"] if item["qualified_name"] == "public_api"
    )

    assert exported["contract_role"] == "public_candidate"
    assert exported["priority"] == "high"
    assert exported["reconstruction_target"] is True


def test_package_surface_module_is_an_evidence_backed_reconstruction_target():
    index = _index()
    index["modules"] = [
        {
            "file": "src/__init__.py",
            "kind": "source_module",
            "line_count": 2,
            "evidence": {
                "file": "src/__init__.py",
                "line_start": 1,
                "line_end": 2,
            },
        }
    ]
    alias_map = AliasMap()
    alias_map.evidence_id("src/__init__.py", 1, 2)

    inventory = build_dirty_inventory(index, alias_map)
    package_surface = next(
        item for item in inventory["items"] if item["kind"] == "module"
    )

    assert package_surface["contract_role"] == "public_candidate"
    assert package_surface["reconstruction_target"] is True
    assert package_surface["evidence_id"].startswith("EV-")


def test_function_local_definitions_are_not_public_reconstruction_targets():
    index = _index()
    index["files_indexed"].append("src/signals.py")
    index["functions"].extend(
        [
            {
                "file": "src/signals.py",
                "name": "connect",
                "qualified_name": "Signal.connect",
                "owner": "Signal",
                "definition_scope": "class",
                "args": ["self"],
                "line_start": 1,
                "line_end": 8,
                "evidence": {
                    "file": "src/signals.py",
                    "line_start": 1,
                    "line_end": 8,
                },
            },
            {
                "file": "src/signals.py",
                "name": "cleanup",
                "qualified_name": "Signal.connect.cleanup",
                "owner": "Signal.connect",
                "definition_scope": "function",
                "args": [],
                "line_start": 3,
                "line_end": 4,
                "evidence": {
                    "file": "src/signals.py",
                    "line_start": 3,
                    "line_end": 4,
                },
            },
        ]
    )
    # A same-named package export elsewhere must not accidentally promote a closure.
    index["imports"].append(
        {
            "file": "src/__init__.py",
            "kind": "from_import",
            "module": ".public",
            "name": "cleanup",
            "alias": None,
        }
    )

    inventory = build_dirty_inventory(index, AliasMap())
    functions = {
        item["qualified_name"]: item
        for item in inventory["items"]
        if item["kind"] == "function"
    }

    assert functions["Signal.connect"]["contract_role"] == "public_candidate"
    assert functions["Signal.connect"]["reconstruction_target"] is True
    assert functions["Signal.connect.cleanup"]["contract_role"] == "internal"
    assert functions["Signal.connect.cleanup"]["priority"] == "low"
    assert functions["Signal.connect.cleanup"]["reconstruction_target"] is False


def test_priority_batches_retain_every_reconstruction_target():
    items = []
    catalogue = []
    for index in range(25):
        evidence_id = f"EV-{index + 1:03d}"
        items.append(
            {
                "inventory_id": f"INV-{index:012X}",
                "evidence_id": evidence_id,
                "priority": "high",
                "contract_role": "public_candidate",
                "source_scope": "production",
                "source_path": f"src/component_{index}.py",
                "kind": "function",
                "reconstruction_target": True,
            }
        )
        catalogue.append({"evidence_id": evidence_id})

    batches = build_planning_priority_batches(
        {"items": items}, catalogue, "code_facts", batch_size=12
    )

    assert [len(batch) for batch in batches] == [12, 12, 1]
    assert {item["evidence_id"] for batch in batches for item in batch} == {
        item["evidence_id"] for item in items
    }

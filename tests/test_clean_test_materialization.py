from packages.modules.clean.adapters.generic import GenericRuntimeAdapter
from packages.modules.clean.adapters.python_tests import (
    materialise_python_behavior_tests,
)
from packages.modules.clean.runner import _append_adapter_generated_tests


def _suite():
    return {
        "schema_version": 1,
        "probes": [
            {
                "probe_id": "PROBE-001",
                "capability_id": "CAP-001",
                "requirement_ids": ["FR-001"],
                "scenario_ids": ["TC-001"],
                "code": (
                    "from __future__ import annotations\n"
                    "value: str | None = 'ready'\n"
                    "assert value == 'ready'\n"
                ),
            },
            {
                "probe_id": "PROBE-002",
                "capability_id": "CAP-001",
                "requirement_ids": ["EH-001"],
                "scenario_ids": [],
                "code": "assert 2 + 2 == 4\n",
            },
        ],
    }


def test_python_adapter_materialises_probes_as_independent_project_tests():
    suite = _suite()

    files = materialise_python_behavior_tests(suite)

    assert [item["path"] for item in files] == [
        "tests/test_reconstruction_behavior.py"
    ]
    assert files[0]["requirement_ids"] == ["FR-001", "EH-001"]
    assert files[0]["scenario_ids"] == ["TC-001"]
    namespace = {}
    exec(compile(files[0]["content"], files[0]["path"], "exec"), namespace)
    assert namespace["_SOURCE_PROBE_001"] == suite["probes"][0]["code"]
    namespace["test_probe_001_fr_001_tc_001"]()
    namespace["test_probe_002_eh_001"]()


def test_adapter_tests_are_planned_after_production_files():
    plan = {
        "files": [
            {
                "path": "src/example.py",
                "generation_order": 1,
            }
        ]
    }
    generated = materialise_python_behavior_tests(_suite())

    _append_adapter_generated_tests(plan, generated)

    test_task = plan["files"][-1]
    assert test_task["generation_order"] == 2
    assert test_task["depends_on"] == ["src/example.py"]
    assert test_task["provides"] == []


def test_generic_adapter_does_not_assume_a_test_runtime():
    assert GenericRuntimeAdapter().materialise_behavior_tests({}, {}, _suite()) == []

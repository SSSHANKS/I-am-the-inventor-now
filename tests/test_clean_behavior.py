import sys
from pathlib import Path

import pytest

from packages.modules.clean.behavior import (
    BehaviorProbeError,
    LocalPythonBehaviorProbeExecutor,
    validate_behavior_probe_suite,
)
from packages.modules.clean.workspace import CleanWorkspace


def _architecture():
    return {
        "project_profile": {"layout": "flat"},
        "capabilities": [
            {
                "capability_id": "CAP-001",
                "purpose": "Return the configured greeting.",
                "requirement_ids": ["FR-001", "AC-001"],
                "scenario_ids": ["TC-001"],
                "component_ids": ["CMP-001"],
            }
        ],
        "contracts": [
            {
                "contract_id": "SYM-001",
                "component_id": "CMP-001",
                "qualified_name": "greeting.greeting",
                "requirement_ids": ["FR-001", "AC-001"],
            }
        ],
    }


def _manifest():
    return {
        "files": [
            {
                "path": "greeting.py",
                "category": "source",
                "component_id": "CMP-001",
                "requirement_ids": ["FR-001", "AC-001"],
            }
        ]
    }


def _suite(code="from greeting import greeting\nassert greeting() == 'hello'\n"):
    return {
        "schema_version": 1,
        "probes": [
            {
                "probe_id": "PROBE-001",
                "capability_id": "CAP-001",
                "requirement_ids": ["FR-001"],
                "scenario_ids": ["TC-001"],
                "code": code,
            },
            {
                "probe_id": "PROBE-002",
                "capability_id": "CAP-001",
                "requirement_ids": ["AC-001"],
                "scenario_ids": [],
                "code": code,
            },
        ],
    }


def _executor():
    return LocalPythonBehaviorProbeExecutor(
        python_executable=sys.executable,
        timeout_seconds=10,
    )


def test_behavior_probe_suite_requires_complete_requirement_coverage():
    suite = _suite()
    suite["probes"] = suite["probes"][:1]

    with pytest.raises(BehaviorProbeError, match="AC-001"):
        validate_behavior_probe_suite(suite, _architecture())


@pytest.mark.parametrize(
    "code, message",
    [
        ("import socket\nassert True\n", "forbidden module"),
        ("import subprocess\nsubprocess.run(['unsafe'])\nassert True\n", "forbidden operation"),
        ("import pytest\nassert True\n", "undeclared module"),
        ("from greeting import greeting\ngreeting()\n", "no module-executed assertions"),
        (
            "def test_later():\n    assert False\n",
            "no module-executed assertions",
        ),
        (
            "from greeting import greeting\nassert isinstance(greeting(), str)\n",
            "only type, existence, or non-null assertions",
        ),
        (
            "try:\n    raise ValueError()\nexcept Exception:\n    pass\nassert True\n",
            "unspecific exception",
        ),
        ("import os\nos.system('echo unsafe')\nassert True\n", "forbidden operation"),
    ],
)
def test_behavior_probe_suite_rejects_unsafe_or_non_observing_code(code, message):
    with pytest.raises(BehaviorProbeError, match=message):
        validate_behavior_probe_suite(_suite(code), _architecture())


def test_behavior_probe_suite_requires_mocking_for_external_boundaries():
    architecture = _architecture()
    architecture["capabilities"][0]["purpose"] = "Spawn an external editor process."

    with pytest.raises(BehaviorProbeError, match="must mock external boundary"):
        validate_behavior_probe_suite(_suite(), architecture)


def test_python_behavior_executor_passes_observable_behavior(tmp_path):
    workspace = CleanWorkspace(tmp_path / "output")
    workspace.write_generated_file(
        "greeting.py", "def greeting() -> str:\n    return 'hello'\n"
    )

    checks = _executor().validate(
        workspace.project_root,
        _architecture(),
        _manifest(),
        _suite(),
    )

    assert checks == [
        {
            "name": "python-behavior-probes",
            "status": "pass",
            "message": "Passed 2 behavior probe(s)",
            "paths": [],
        }
    ]


def test_python_behavior_executor_maps_failure_to_production_requirements(tmp_path):
    workspace = CleanWorkspace(tmp_path / "output")
    workspace.write_generated_file(
        "greeting.py", "def greeting() -> str:\n    return 'wrong'\n"
    )

    check = _executor().validate(
        workspace.project_root,
        _architecture(),
        _manifest(),
        _suite(),
    )[0]

    assert check["status"] == "fail"
    assert check["paths"] == ["greeting.py"]
    diagnostic = check["diagnostics"][0]
    assert diagnostic["requirement_ids"] == ["FR-001"]
    assert diagnostic["contract_ids"] == ["SYM-001"]
    assert diagnostic["actual"] == "exited 1"


def test_python_behavior_executor_rejects_probe_project_mutation(tmp_path):
    workspace = CleanWorkspace(tmp_path / "output")
    workspace.write_generated_file(
        "greeting.py", "def greeting() -> str:\n    return 'hello'\n"
    )
    suite = _suite(
        "from pathlib import Path\n"
        "import greeting\n"
        "Path(greeting.__file__).write_text(\"changed\", encoding=\"utf-8\")\n"
        "assert True\n"
    )

    check = _executor().validate(
        workspace.project_root,
        _architecture(),
        _manifest(),
        suite,
    )[0]

    assert check["status"] == "fail"
    assert "modified generated project files" in check["diagnostics"][0]["actual"]
    assert workspace.read_generated_file("greeting.py").startswith("def greeting")


def test_python_behavior_executor_sanitizes_host_runtime_paths(tmp_path):
    executor = _executor()
    python_root = Path(sys.executable).resolve().parent
    project_root = tmp_path / "project"
    probe_root = tmp_path / "probe"
    output = f"{python_root / 'Lib' / 'runpy.py'}\n{probe_root / 'probe.py'}"

    sanitized = executor._bounded(output, project_root, probe_root)

    assert str(python_root) not in sanitized
    assert str(probe_root) not in sanitized
    assert "<python-root>" in sanitized
    assert "<probe>" in sanitized

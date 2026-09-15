import pytest

from packages.modules.clean.adapters import (
    GenericRuntimeAdapter,
    PythonRuntimeAdapter,
    RuntimeAdapterError,
    project_first_planning_policy,
    select_runtime_adapter,
)
from packages.modules.clean.workspace import CleanWorkspace


def _architecture(language="Python"):
    return {
        "project_profile": {"language": language},
    }


def _manifest(*, validators=None, integration_checks=None, owner="model"):
    return {
        "files": [
            {
                "path": "sample.py",
                "generation_owner": owner,
                "local_validators": validators or ["utf8-text"],
                "integration_checks": integration_checks or [],
            }
        ]
    }


def _plan():
    return {
        "runtime": {"language": "Python", "minimum_version": "3.12"},
        "entry_points": [{"path": "sample.py", "description": "Provider"}],
        "symbol_contracts": [
            {
                "symbol_id": "SYM-001",
                "qualified_name": "sample.run",
                "kind": "function",
                "signature": "run(value: str) -> str",
                "visibility": "public",
                "requirement_ids": ["FR-001"],
            }
        ],
        "files": [
            {
                "path": "sample.py",
                "provides": ["SYM-001"],
                "requires": [],
                "depends_on": [],
            }
        ],
    }


def test_project_first_policy_advertises_runtime_adapters_without_tests():
    policy = project_first_planning_policy(syntax_checks=True)

    assert policy["generated_tests"] is False
    assert policy["network_installation"] is False
    assert policy["executable_validation"] is False
    assert [item["adapter"] for item in policy["adapters"]] == [
        "python-static",
        "generic-structure",
    ]
    assert "syntax" in policy["adapters"][0]["available_checks"]
    assert policy["adapters"][1]["readiness_ceiling"] == "structure"


def test_runtime_adapter_selection_is_language_driven():
    assert isinstance(select_runtime_adapter(_architecture("Python 3")), PythonRuntimeAdapter)
    assert isinstance(select_runtime_adapter(_architecture("Rust")), GenericRuntimeAdapter)


def test_python_adapter_rejects_manifest_checks_it_does_not_implement():
    adapter = PythonRuntimeAdapter()

    with pytest.raises(RuntimeAdapterError, match=r"unsupported.*package-build"):
        adapter.validate_manifest(
            _architecture(),
            _manifest(validators=["package-build"]),
            syntax_checks=True,
        )


def test_python_adapter_normalises_common_validator_aliases():
    architecture = {
        "project_profile": {"language": "Python", "layout": "src"},
        "contracts": [],
    }
    manifest = {
        "files": [
            {
                "path": "pyproject.toml",
                "category": "metadata",
                "provides": [],
                "depends_on": [],
                "local_validators": ["manifest"],
                "integration_checks": ["build", "packaging", "entry-point"],
            }
        ],
        "entry_points": [],
        "readiness_obligations": [],
    }

    result = PythonRuntimeAdapter().normalise_manifest(architecture, manifest)

    assert result["files"][0]["local_validators"] == ["manifest-integrity"]
    assert result["files"][0]["integration_checks"] == [
        "python-packaging",
        "python-entry-points",
    ]


def test_python_adapter_removes_src_prefix_from_approved_internal_imports():
    architecture = {
        "project_profile": {"language": "Python", "layout": "src"},
        "contracts": [
            {"qualified_name": "core.utilities.execute"},
            {"qualified_name": "cli.entry.main"},
        ],
    }
    content = (
        "from src.core.utilities import execute\n"
        "from src.unknown import value\n"
        "from pathlib import Path\n"
    )

    result = PythonRuntimeAdapter().normalise_generated_content(
        "src/cli/entry.py", content, architecture, {"files": []}
    )

    assert result == (
        "from core.utilities import execute\n"
        "from src.unknown import value\n"
        "from pathlib import Path\n"
    )


def test_python_adapter_preserves_src_prefix_outside_src_layout():
    architecture = {
        "project_profile": {"language": "Python", "layout": "flat"},
        "contracts": [{"qualified_name": "core.utilities.execute"}],
    }
    content = "from src.core.utilities import execute\n"

    result = PythonRuntimeAdapter().normalise_generated_content(
        "cli/entry.py", content, architecture, {"files": []}
    )

    assert result == content


def test_adapter_rejects_adapter_owned_files_without_scaffold_support():
    adapter = PythonRuntimeAdapter()

    with pytest.raises(RuntimeAdapterError, match=r"cannot generate.*sample.py"):
        adapter.validate_manifest(
            _architecture(),
            _manifest(owner="adapter"),
            syntax_checks=True,
        )


def test_python_adapter_aligns_provider_paths_and_references_with_contract_modules():
    architecture = {
        "project_profile": {"language": "Python", "layout": "src"},
        "contracts": [
            {
                "contract_id": "SYM-001",
                "qualified_name": "project_x.core.resolve",
            }
        ],
    }
    manifest = {
        "files": [
            {
                "path": "src/project_x/__init__.py",
                "category": "source",
                "provides": ["SYM-001"],
                "depends_on": [],
            },
            {
                "path": "src/project_x/consumer.py",
                "category": "source",
                "provides": [],
                "depends_on": ["src/project_x/__init__.py"],
            },
        ],
        "entry_points": [
            {"entry_point_id": "EP-001", "path": "src/project_x/__init__.py"}
        ],
        "readiness_obligations": [
            {
                "obligation_id": "READY-001",
                "paths": ["src/project_x/__init__.py"],
            }
        ],
    }

    result = PythonRuntimeAdapter().normalise_manifest(architecture, manifest)

    assert result["files"][0]["path"] == "src/project_x/core.py"
    assert result["files"][1]["depends_on"] == ["src/project_x/core.py"]
    assert result["entry_points"][0]["path"] == "src/project_x/core.py"
    assert result["readiness_obligations"][0]["paths"] == ["src/project_x/core.py"]
    assert manifest["files"][0]["path"] == "src/project_x/__init__.py"


def test_python_adapter_consolidates_same_component_contracts_in_one_module():
    architecture = {
        "project_profile": {"language": "Python", "layout": "src"},
        "contracts": [
            {"contract_id": "SYM-001", "qualified_name": "project_x.streams.Reader"},
            {"contract_id": "SYM-002", "qualified_name": "project_x.streams.Writer"},
        ],
    }
    manifest = {
        "files": [
            {
                "path": "src/project_x/streams.py",
                "category": "source",
                "component_id": "CMP-001",
                "purpose": "Read streams",
                "requirement_ids": ["FR-001"],
                "provides": ["SYM-001"],
                "requires": [],
                "depends_on": [],
                "generation_order": 1,
                "local_validators": ["syntax"],
                "integration_checks": [],
            },
            {
                "path": "src/project_x/streams_inspector.py",
                "category": "source",
                "component_id": "CMP-001",
                "purpose": "Write streams",
                "requirement_ids": ["FR-002"],
                "provides": ["SYM-002"],
                "requires": ["SYM-001"],
                "depends_on": ["src/project_x/streams.py"],
                "generation_order": 2,
                "local_validators": ["syntax"],
                "integration_checks": ["python-imports"],
            },
        ],
        "entry_points": [],
        "readiness_obligations": [
            {
                "obligation_id": "READY-001",
                "paths": [
                    "src/project_x/streams.py",
                    "src/project_x/streams_inspector.py",
                ],
            }
        ],
    }

    result = PythonRuntimeAdapter().normalise_manifest(architecture, manifest)

    assert len(result["files"]) == 1
    assert result["files"][0]["path"] == "src/project_x/streams.py"
    assert result["files"][0]["provides"] == ["SYM-001", "SYM-002"]
    assert result["files"][0]["requirement_ids"] == ["FR-001", "FR-002"]
    assert result["files"][0]["depends_on"] == []
    assert result["files"][0]["requires"] == []
    assert result["readiness_obligations"][0]["paths"] == [
        "src/project_x/streams.py"
    ]


def test_python_adapter_runs_exact_contract_validation(tmp_path):
    workspace = CleanWorkspace(tmp_path / "output")
    workspace.write_generated_file(
        "sample.py",
        "def run(value: bytes) -> bytes:\n    return value\n",
    )

    checks = PythonRuntimeAdapter().validate_project(
        workspace,
        _plan(),
        syntax_checks=True,
    )

    contracts = next(item for item in checks if item["name"] == "python-contracts")
    assert contracts["status"] == "fail"
    assert contracts["diagnostics"][0]["expected"] == "run(value: str) -> str"
    assert contracts["diagnostics"][0]["actual"] == "run(value: bytes) -> bytes"


def test_generic_adapter_reports_only_structural_readiness(tmp_path):
    workspace = CleanWorkspace(tmp_path / "output")
    workspace.write_generated_file("artifact.txt", "runtime-neutral content\n")
    plan = {
        "files": [{"path": "artifact.txt"}],
        "entry_points": [{"path": "artifact.txt"}],
        "symbol_contracts": [],
    }

    checks = GenericRuntimeAdapter().validate_project(
        workspace,
        plan,
        syntax_checks=True,
    )

    assert next(item for item in checks if item["name"] == "syntax")["status"] == "skipped"
    assert all(item["status"] != "fail" for item in checks)

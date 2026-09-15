import json
from copy import deepcopy

import pytest
from marshmallow import ValidationError

from packages.agents.base_agent import StubTextClient
from packages.agents.clean_team import CleanManifestAgent
from packages.modules.clean.manifest import (
    CleanManifestError,
    architecture_sha256,
    normalise_manifest,
    validate_manifest,
)
from packages.modules.supervising.schemas import CleanManifestSchema


def _architecture():
    return {
        "schema_version": 1,
        "project_profile": {
            "kind": "library",
            "language": "Python",
            "runtime_version": "3.12",
            "build_system": "setuptools",
            "layout": "src",
            "compatibility_mode": "renamed",
        },
        "capabilities": [
            {
                "capability_id": "CAP-001",
                "purpose": "Create greetings.",
                "requirement_ids": ["FR-001"],
                "scenario_ids": ["TC-001"],
                "component_ids": ["CMP-001"],
            }
        ],
        "components": [
            {
                "component_id": "CMP-001",
                "purpose": "Implement greeting behavior.",
                "kind": "domain",
                "requirement_ids": ["FR-001"],
                "depends_on": [],
            }
        ],
        "contracts": [
            {
                "contract_id": "SYM-001",
                "component_id": "CMP-001",
                "qualified_name": "greeting.greet",
                "kind": "function",
                "declaration": "greet(name: str) -> str",
                "visibility": "public",
                "requirement_ids": ["FR-001"],
            }
        ],
        "entry_points": [
            {
                "entry_point_id": "EP-001",
                "component_id": "CMP-001",
                "description": "Public greeting function.",
                "contract_ids": ["SYM-001"],
            }
        ],
        "dependency_decisions": [],
        "proposals": [],
        "unresolved_gaps": [],
    }


def _manifest(architecture=None):
    architecture = architecture or _architecture()
    return {
        "schema_version": 1,
        "architecture_sha256": architecture_sha256(architecture),
        "files": [
            {
                "path": "src/greeting.py",
                "category": "source",
                "component_id": "CMP-001",
                "purpose": "Provide the public greeting operation.",
                "requirement_ids": ["FR-001"],
                "provides": ["SYM-001"],
                "requires": [],
                "depends_on": [],
                "generation_order": 1,
                "generation_owner": "model",
                "local_validators": ["python-syntax", "python-contracts"],
                "integration_checks": ["python-imports"],
            },
            {
                "path": "README.md",
                "category": "documentation",
                "component_id": "CMP-001",
                "purpose": "Document installation and public usage.",
                "requirement_ids": [],
                "provides": [],
                "requires": [],
                "depends_on": ["src/greeting.py"],
                "generation_order": 2,
                "generation_owner": "model",
                "local_validators": ["utf8-text"],
                "integration_checks": [],
            },
        ],
        "entry_points": [
            {"entry_point_id": "EP-001", "path": "src/greeting.py"}
        ],
        "readiness_obligations": [
            {
                "obligation_id": "READY-001",
                "kind": "manifest",
                "description": "Every planned file is present and owned.",
                "component_ids": ["CMP-001"],
                "paths": ["src/greeting.py", "README.md"],
                "required": True,
            },
            {
                "obligation_id": "READY-002",
                "kind": "contracts",
                "description": "The public greeting contract is exact.",
                "component_ids": ["CMP-001"],
                "paths": ["src/greeting.py"],
                "required": True,
            },
        ],
    }


def test_clean_manifest_accepts_owned_dependency_ordered_files():
    architecture = _architecture()
    manifest = CleanManifestSchema().load(_manifest(architecture))

    assert validate_manifest(manifest, architecture) is manifest


def test_clean_manifest_hash_is_canonical_and_binds_architecture():
    architecture = _architecture()
    reordered = json.loads(json.dumps(architecture, sort_keys=True))

    assert architecture_sha256(architecture) == architecture_sha256(reordered)
    manifest = _manifest(architecture)
    manifest["architecture_sha256"] = "0" * 64
    with pytest.raises(CleanManifestError, match="architecture hash"):
        validate_manifest(manifest, architecture)


def test_manifest_normalisation_removes_behavior_from_non_executable_files():
    manifest = _manifest()
    metadata = manifest["files"][0]
    metadata["category"] = "metadata"
    metadata["requirement_ids"] = ["FR-001"]
    metadata["provides"] = ["SYM-001"]

    result = normalise_manifest(manifest)

    assert result["files"][0]["requirement_ids"] == []
    assert result["files"][0]["provides"] == []
    assert manifest["files"][0]["requirement_ids"] == ["FR-001"]


def test_manifest_normalisation_classifies_build_manifests_as_metadata():
    manifest = _manifest()
    manifest["files"][0]["path"] = "pyproject.toml"
    manifest["files"][0]["category"] = "configuration"

    result = normalise_manifest(manifest)

    assert result["files"][0]["category"] == "metadata"
    assert result["files"][0]["requirement_ids"] == []


def test_manifest_normalisation_removes_requirements_not_owned_by_file_component():
    architecture = _architecture()
    manifest = _manifest(architecture)
    manifest["files"][0]["requirement_ids"].append("EH-999")

    result = normalise_manifest(manifest, architecture)

    assert result["files"][0]["requirement_ids"] == ["FR-001"]


def test_manifest_normalisation_prunes_dependencies_outside_architecture_graph():
    architecture = _architecture()
    architecture["components"].append(
        {
            "component_id": "CMP-002",
            "purpose": "Provide an unrelated operation.",
            "kind": "domain",
            "requirement_ids": ["FR-002"],
            "depends_on": [],
        }
    )
    architecture["contracts"].append(
        {
            "contract_id": "SYM-002",
            "component_id": "CMP-002",
            "qualified_name": "unrelated.run",
            "kind": "function",
            "declaration": "run() -> None",
            "visibility": "public",
            "requirement_ids": ["FR-002"],
        }
    )
    manifest = _manifest(architecture)
    manifest["files"].append(
        {
            "path": "src/unrelated.py",
            "category": "source",
            "component_id": "CMP-002",
            "purpose": "Provide an unrelated operation.",
            "requirement_ids": ["FR-002"],
            "provides": ["SYM-002"],
            "requires": [],
            "depends_on": [],
            "generation_order": 3,
            "generation_owner": "model",
            "local_validators": ["python-syntax"],
            "integration_checks": [],
        }
    )
    consumer = manifest["files"][0]
    consumer["requires"] = ["SYM-002"]
    consumer["depends_on"] = ["src/unrelated.py"]

    result = normalise_manifest(manifest, architecture)

    assert result["files"][0]["requires"] == []
    assert result["files"][0]["depends_on"] == []
    assert manifest["files"][0]["requires"] == ["SYM-002"]


def test_manifest_normalisation_maps_component_dependency_to_unique_provider_file():
    architecture = _architecture()
    manifest = _manifest(architecture)
    manifest["files"].append(
        {
            "path": "src/consumer.py",
            "category": "source",
            "component_id": "CMP-001",
            "purpose": "Consume the public operation",
            "requirement_ids": ["FR-001"],
            "provides": [],
            "requires": ["SYM-001"],
            "depends_on": ["CMP-001"],
            "generation_order": 4,
            "generation_owner": "model",
            "local_validators": ["python-syntax"],
            "integration_checks": ["python-imports"],
        }
    )

    result = normalise_manifest(manifest, architecture)

    assert result["files"][-1]["depends_on"] == ["src/greeting.py"]


def test_clean_manifest_does_not_allocate_scenarios_to_generated_files():
    manifest = _manifest()
    manifest["files"][0]["requirement_ids"] = ["TC-001"]

    with pytest.raises(ValidationError):
        CleanManifestSchema().load(manifest)


def test_clean_manifest_rejects_documentation_as_functional_implementation():
    architecture = _architecture()
    manifest = _manifest(architecture)
    manifest["files"][1]["requirement_ids"] = ["FR-001"]

    with pytest.raises(CleanManifestError, match="cannot satisfy functional"):
        validate_manifest(manifest, architecture)


def test_clean_manifest_requires_exactly_one_contract_provider():
    architecture = _architecture()
    manifest = _manifest(architecture)
    manifest["files"][0]["provides"] = []

    with pytest.raises(CleanManifestError, match=r"does not provide.*SYM-001"):
        validate_manifest(manifest, architecture)


def test_clean_manifest_requires_consumers_to_depend_on_provider():
    architecture = _architecture()
    manifest = _manifest(architecture)
    manifest["files"].append(
        {
            "path": "src/consumer.py",
            "category": "source",
            "component_id": "CMP-001",
            "purpose": "Consume the greeting operation.",
            "requirement_ids": ["FR-001"],
            "provides": [],
            "requires": ["SYM-001"],
            "depends_on": [],
            "generation_order": 3,
            "generation_owner": "model",
            "local_validators": ["python-syntax"],
            "integration_checks": ["python-imports"],
        }
    )
    manifest["readiness_obligations"][0]["paths"].append("src/consumer.py")

    with pytest.raises(CleanManifestError, match="does not depend on provider"):
        validate_manifest(manifest, architecture)


def test_clean_manifest_rejects_late_file_dependencies():
    architecture = _architecture()
    manifest = _manifest(architecture)
    manifest["files"][0]["depends_on"] = ["README.md"]

    with pytest.raises(CleanManifestError, match="generated before"):
        validate_manifest(manifest, architecture)


def test_clean_manifest_rejects_unbound_entry_point_contracts():
    architecture = _architecture()
    manifest = _manifest(architecture)
    manifest["entry_points"][0]["path"] = "README.md"

    with pytest.raises(CleanManifestError, match="does not provide contracts"):
        validate_manifest(manifest, architecture)


def test_clean_manifest_agent_returns_deterministically_validated_manifest():
    architecture = _architecture()
    response = json.dumps(_manifest(architecture))
    client = StubTextClient([response])
    agent = CleanManifestAgent(model="stub/model", chat_client=client)

    result = agent.derive(
        architecture,
        adapter_policy={"adapter": "local-python", "generated_tests": False},
    )

    assert result == _manifest(architecture)
    assert client.call_count == 1
    prompt = client.prompts[0]
    assert architecture_sha256(architecture) in prompt
    assert '"owned_contract_ids": [' in prompt
    assert '"generated_tests": false' in prompt
    assert "Do not generate project tests or file content" in prompt
    assert agent.source_reader is None
    assert agent.alias_map is None


def test_clean_manifest_agent_revises_semantically_invalid_manifest():
    architecture = _architecture()
    response = json.dumps(_manifest(architecture))
    client = StubTextClient([response])
    agent = CleanManifestAgent(model="stub/model", chat_client=client)

    result = agent.revise(
        architecture,
        _manifest(architecture),
        "File 'src/consumer.py' consumes SYM-001 outside the dependency graph",
        adapter_policy={"adapter": "python-static"},
    )

    assert result == _manifest(architecture)
    prompt = client.prompts[0]
    assert "<clean_manifest_repair_request>" in prompt
    assert "outside the dependency graph" in prompt
    assert architecture_sha256(architecture) in prompt


def test_clean_manifest_validation_does_not_mutate_inputs():
    architecture = _architecture()
    manifest = _manifest(architecture)
    original_architecture = deepcopy(architecture)
    original_manifest = deepcopy(manifest)

    validate_manifest(manifest, architecture)

    assert architecture == original_architecture
    assert manifest == original_manifest

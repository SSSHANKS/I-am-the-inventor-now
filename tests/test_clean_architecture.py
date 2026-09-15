import json
from copy import deepcopy

import pytest
from marshmallow import ValidationError

from packages.agents.base_agent import StubTextClient
from packages.agents.clean_team import CleanArchitectAgent
from packages.modules.clean.architecture import (
    CleanArchitectureError,
    normalise_architecture,
    normalise_behavior_rule_evidence,
    validate_architecture,
)
from packages.modules.supervising.schemas import CleanArchitectureSchema

SPECIFICATION = """# Greeting

## Functional Requirements

- FR-001: Return a greeting for the supplied name.

## Error Handling

- EH-001: Reject an empty name.

## Test Candidates

- TC-001: A normal name produces a greeting.
"""


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
                "purpose": "Create validated greetings.",
                "requirement_ids": ["FR-001", "EH-001"],
                "scenario_ids": ["TC-001"],
                "component_ids": ["CMP-001"],
            }
        ],
        "components": [
            {
                "component_id": "CMP-001",
                "purpose": "Implement greeting behavior.",
                "kind": "domain",
                "requirement_ids": ["FR-001", "EH-001"],
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
                "requirement_ids": ["FR-001", "EH-001"],
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
        "proposals": [
            {
                "proposal_id": "PROP-001",
                "field": "project_profile.layout",
                "chosen_value": "src",
                "reason": "Use the runtime adapter baseline.",
                "source": "adapter-baseline",
                "affected_component_ids": ["CMP-001"],
                "affects_public_compatibility": False,
            }
        ],
        "unresolved_gaps": [],
    }


def test_clean_architecture_schema_and_traceability_accept_coherent_design():
    architecture = CleanArchitectureSchema().load(_architecture())

    assert (
        validate_architecture(
            architecture,
            SPECIFICATION,
            compatibility_mode="renamed",
        )
        is architecture
    )


def test_auxiliary_observer_behavior_requires_a_separate_contract():
    specification = SPECIFICATION.replace(
        "Return a greeting for the supplied name.",
        "Specific auxiliary signals notify observers after lifecycle operations.",
    )
    architecture = _architecture()
    architecture["contracts"][0]["behavior_rules"] = [
        {
            "aspect": "state",
            "statement": (
                "Specific auxiliary signals notify observers after lifecycle operations."
            ),
            "requirement_ids": ["FR-001"],
            "source": "specification",
            "evidence": [
                {
                    "requirement_id": "FR-001",
                    "excerpt": (
                        "Specific auxiliary signals notify observers after lifecycle operations."
                    ),
                }
            ],
        }
    ]

    with pytest.raises(CleanArchitectureError, match="jointly claimed"):
        validate_architecture(architecture, specification)

    architecture["contracts"].append(
        {
            "contract_id": "SYM-002",
            "component_id": "CMP-001",
            "qualified_name": "greeting.observe_lifecycle",
            "kind": "function",
            "declaration": "observe_lifecycle(callback: callable) -> None",
            "visibility": "public",
            "requirement_ids": ["FR-001"],
        }
    )
    validate_architecture(architecture, specification)


def test_architecture_reports_allocation_and_observer_defects_together():
    specification = SPECIFICATION.replace(
        "Return a greeting for the supplied name.",
        "Specific auxiliary signals notify observers after lifecycle operations.",
    )
    architecture = _architecture()
    architecture["capabilities"][0]["requirement_ids"] = []
    architecture["contracts"][0]["behavior_rules"] = [
        {
            "aspect": "state",
            "statement": (
                "Specific auxiliary signals notify observers after lifecycle operations."
            ),
            "requirement_ids": ["FR-001"],
            "source": "specification",
            "evidence": [
                {
                    "requirement_id": "FR-001",
                    "excerpt": (
                        "Specific auxiliary signals notify observers after lifecycle operations."
                    ),
                }
            ],
        }
    ]

    with pytest.raises(CleanArchitectureError) as captured:
        validate_architecture(architecture, specification)

    assert "does not allocate requirements to capabilities:" in str(captured.value)
    assert "FR-001" in str(captured.value)
    assert "jointly claimed" in str(captured.value)


def test_auxiliary_observer_contract_may_use_a_shared_capability_component():
    specification = SPECIFICATION.replace(
        "Return a greeting for the supplied name.",
        "Dedicated lifecycle events notify observers after operations.",
    )
    architecture = _architecture()
    architecture["contracts"][0]["behavior_rules"] = [
        {
            "aspect": "state",
            "statement": "Dedicated lifecycle events notify observers after operations.",
            "requirement_ids": ["FR-001"],
            "source": "specification",
            "evidence": [{
                "requirement_id": "FR-001",
                "excerpt": "Dedicated lifecycle events notify observers after operations.",
            }],
        }
    ]
    architecture["components"].append({
        "component_id": "CMP-002",
        "purpose": "Expose lifecycle observations.",
        "kind": "interface",
        "requirement_ids": ["FR-001"],
        "depends_on": [],
    })
    architecture["capabilities"][0]["component_ids"].append("CMP-002")
    architecture["contracts"].append({
        "contract_id": "SYM-002",
        "component_id": "CMP-002",
        "qualified_name": "observing.observe_lifecycle",
        "kind": "function",
        "declaration": "observe_lifecycle(callback: callable) -> None",
        "visibility": "public",
        "requirement_ids": ["FR-001"],
    })

    with pytest.raises(CleanArchitectureError, match="jointly claimed"):
        validate_architecture(architecture, specification)

    architecture["components"][0]["depends_on"] = ["CMP-002"]
    validate_architecture(architecture, specification)


def test_auxiliary_observer_contract_requires_a_public_interaction_path():
    specification = SPECIFICATION.replace(
        "Return a greeting for the supplied name.",
        "Dedicated lifecycle events notify observers after operations.",
    )
    architecture = _architecture()
    architecture["contracts"][0]["behavior_rules"] = [{
        "aspect": "state",
        "statement": "Dedicated lifecycle events notify observers after operations.",
        "requirement_ids": ["FR-001"],
        "source": "specification",
        "evidence": [{
            "requirement_id": "FR-001",
            "excerpt": "Dedicated lifecycle events notify observers after operations.",
        }],
    }]
    architecture["contracts"].append({
        "contract_id": "SYM-002",
        "component_id": "CMP-001",
        "qualified_name": "greeting.LifecycleObserver",
        "kind": "class",
        "declaration": "class LifecycleObserver:\n    def update(self, event: object) -> None: ...",
        "visibility": "public",
        "requirement_ids": ["FR-001"],
    })

    with pytest.raises(CleanArchitectureError, match="interaction path"):
        validate_architecture(architecture, specification)

    architecture["contracts"][0]["declaration"] = (
        "greet(name: str, observer: LifecycleObserver) -> str"
    )
    validate_architecture(architecture, specification)


def test_observer_interaction_may_be_registered_by_the_producer_contract():
    specification = SPECIFICATION.replace(
        "Return a greeting for the supplied name.",
        "Dedicated lifecycle events notify observers after operations.",
    )
    architecture = _architecture()
    architecture["contracts"][0]["declaration"] = (
        "class Greeter:\n"
        "    def greet(self, name: str) -> str: ...\n"
        "    def register_observer(self, callback: callable) -> None: ..."
    )
    architecture["contracts"][0]["behavior_rules"] = [{
        "aspect": "state",
        "statement": "Dedicated lifecycle events notify observers after operations.",
        "requirement_ids": ["FR-001"],
        "source": "specification",
        "evidence": [{
            "requirement_id": "FR-001",
            "excerpt": "Dedicated lifecycle events notify observers after operations.",
        }],
    }]
    architecture["contracts"].append({
        "contract_id": "SYM-002",
        "component_id": "CMP-001",
        "qualified_name": "greeting.LifecycleObserver",
        "kind": "class",
        "declaration": (
            "class LifecycleObserver:\n"
            "    def update(self, event: object) -> None: ..."
        ),
        "visibility": "public",
        "requirement_ids": ["FR-001"],
    })

    validate_architecture(architecture, specification)


def test_mixed_rule_normalisation_prunes_only_misattributed_evidence():
    architecture = _architecture()
    rule = {
        "aspect": "results",
        "statement": "Return a greeting for the supplied name.",
        "requirement_ids": ["FR-001", "EH-001"],
        "source": "specification",
        "evidence": [
            {
                "requirement_id": "FR-001",
                "excerpt": "Return a greeting for the supplied name.",
            },
            {
                "requirement_id": "EH-001",
                "excerpt": "Return a greeting for the supplied name.",
            },
        ],
    }
    architecture["contracts"][0]["behavior_rules"] = [rule]

    normalise_behavior_rule_evidence(architecture, SPECIFICATION)

    assert rule["requirement_ids"] == ["FR-001"]
    assert [item["requirement_id"] for item in rule["evidence"]] == ["FR-001"]


def test_rule_normalisation_drops_a_misattributed_duplicate_statement():
    architecture = _architecture()
    valid_rule = {
        "aspect": "results",
        "statement": "Return a greeting for the supplied name.",
        "requirement_ids": ["FR-001"],
        "source": "specification",
        "evidence": [{
            "requirement_id": "FR-001",
            "excerpt": "Return a greeting for the supplied name.",
        }],
    }
    duplicate = deepcopy(valid_rule)
    duplicate["requirement_ids"] = ["EH-001"]
    duplicate["evidence"] = [{
        "requirement_id": "EH-001",
        "excerpt": valid_rule["statement"],
    }]
    architecture["contracts"][0]["behavior_rules"] = [valid_rule, duplicate]

    normalise_behavior_rule_evidence(architecture, SPECIFICATION)

    assert architecture["contracts"][0]["behavior_rules"] == [valid_rule]


def test_combined_rule_does_not_make_unrelated_requirements_observer_contracts():
    specification = SPECIFICATION.replace(
        "Return a greeting for the supplied name.",
        "Dedicated lifecycle events notify observers after operations.",
    )
    architecture = _architecture()
    architecture["contracts"][0]["behavior_rules"] = [{
        "aspect": "state",
        "statement": (
            "Dedicated lifecycle events notify observers, while empty names are rejected."
        ),
        "requirement_ids": ["FR-001", "EH-001"],
        "source": "specification",
        "evidence": [
            {
                "requirement_id": "FR-001",
                "excerpt": "Dedicated lifecycle events notify observers after operations.",
            },
            {"requirement_id": "EH-001", "excerpt": "Reject an empty name."},
        ],
    }]
    architecture["contracts"].append({
        "contract_id": "SYM-002",
        "component_id": "CMP-001",
        "qualified_name": "greeting.observe_lifecycle",
        "kind": "function",
        "declaration": "observe_lifecycle(callback: callable) -> None",
        "visibility": "public",
        "requirement_ids": ["FR-001"],
    })

    validate_architecture(architecture, specification)


def test_clean_architecture_keeps_test_candidates_as_scenarios_only():
    architecture = _architecture()
    architecture["capabilities"][0]["requirement_ids"].append("TC-001")

    with pytest.raises(ValidationError):
        CleanArchitectureSchema().load(architecture)


def test_clean_architecture_rejects_unallocated_behavior():
    architecture = _architecture()
    architecture["capabilities"][0]["requirement_ids"].remove("EH-001")

    with pytest.raises(CleanArchitectureError, match="capabilities: EH-001"):
        validate_architecture(architecture, SPECIFICATION)


def test_clean_architecture_rejects_component_dependency_cycles():
    architecture = _architecture()
    architecture["components"][0]["depends_on"] = ["CMP-002"]
    architecture["components"].append(
        {
            "component_id": "CMP-002",
            "purpose": "Format greeting output.",
            "kind": "adapter",
            "requirement_ids": ["FR-001"],
            "depends_on": ["CMP-001"],
        }
    )
    architecture["capabilities"][0]["component_ids"].append("CMP-002")

    with pytest.raises(CleanArchitectureError, match="dependency cycle"):
        validate_architecture(architecture, SPECIFICATION)


def test_clean_architecture_rejects_contract_outside_component_ownership():
    architecture = _architecture()
    architecture["components"][0]["requirement_ids"].remove("EH-001")

    with pytest.raises(CleanArchitectureError, match="requirements are not owned"):
        validate_architecture(architecture, SPECIFICATION)


def test_clean_architecture_rejects_python_module_split_across_components():
    architecture = _architecture()
    architecture["components"].append(
        {
            "component_id": "CMP-002",
            "purpose": "Second component",
            "kind": "adapter",
            "requirement_ids": ["FR-001"],
            "depends_on": [],
        }
    )
    architecture["capabilities"][0]["component_ids"].append("CMP-002")
    architecture["contracts"].append(
        {
            "contract_id": "SYM-002",
            "component_id": "CMP-002",
            "qualified_name": "greeting.farewell",
            "kind": "function",
            "declaration": "farewell(name: str) -> str",
            "visibility": "public",
            "requirement_ids": ["FR-001"],
        }
    )

    with pytest.raises(CleanArchitectureError, match="modules cannot span"):
        validate_architecture(architecture, SPECIFICATION)


def test_clean_architecture_normalisation_merges_forced_python_module_owners():
    architecture = _architecture()
    architecture["components"].append(
        {
            "component_id": "CMP-002",
            "purpose": "Second greeting operation.",
            "kind": "domain",
            "requirement_ids": ["FR-001"],
            "depends_on": ["CMP-001"],
        }
    )
    architecture["capabilities"][0]["component_ids"].append("CMP-002")
    architecture["contracts"].append(
        {
            "contract_id": "SYM-002",
            "component_id": "CMP-002",
            "qualified_name": "greeting.farewell",
            "kind": "function",
            "declaration": "farewell(name: str) -> str",
            "visibility": "public",
            "requirement_ids": ["FR-001"],
        }
    )

    result = normalise_architecture(architecture)

    assert [item["component_id"] for item in result["components"]] == ["CMP-001"]
    assert result["components"][0]["depends_on"] == []
    assert result["capabilities"][0]["component_ids"] == ["CMP-001"]
    assert result["contracts"][1]["component_id"] == "CMP-001"
    assert len(architecture["components"]) == 2
    validate_architecture(result, SPECIFICATION)


def test_clean_architecture_normalisation_recovers_unique_capability_requirement():
    architecture = _architecture()
    architecture["capabilities"][0]["requirement_ids"].remove("EH-001")

    result = normalise_architecture(architecture)

    assert result["capabilities"][0]["requirement_ids"] == ["FR-001", "EH-001"]
    assert architecture["capabilities"][0]["requirement_ids"] == ["FR-001"]
    validate_architecture(result, SPECIFICATION)


def test_clean_architecture_normalisation_recovers_contract_owner_requirements():
    architecture = _architecture()
    architecture["components"][0]["requirement_ids"].remove("EH-001")
    architecture["capabilities"][0]["requirement_ids"].remove("EH-001")

    result = normalise_architecture(architecture)

    assert result["components"][0]["requirement_ids"] == ["FR-001", "EH-001"]
    assert result["capabilities"][0]["requirement_ids"] == ["FR-001", "EH-001"]
    assert architecture["components"][0]["requirement_ids"] == ["FR-001"]
    validate_architecture(result, SPECIFICATION)


def test_clean_architecture_normalisation_recovers_single_component_capability_ownership():
    architecture = _architecture()
    architecture["components"][0]["requirement_ids"].remove("EH-001")
    architecture["contracts"][0]["requirement_ids"].remove("EH-001")

    result = normalise_architecture(architecture)

    assert result["components"][0]["requirement_ids"] == ["FR-001", "EH-001"]
    assert architecture["components"][0]["requirement_ids"] == ["FR-001"]
    validate_architecture(result, SPECIFICATION)


def test_clean_architecture_normalisation_aligns_contract_declaration_name():
    architecture = _architecture()
    architecture["contracts"][0]["declaration"] = "wrong(name: str) -> str"

    result = normalise_architecture(architecture)

    assert result["contracts"][0]["declaration"] == "greet(name: str) -> str"
    assert architecture["contracts"][0]["declaration"] == "wrong(name: str) -> str"
    validate_architecture(result, SPECIFICATION)


def test_clean_architecture_normalisation_aligns_stub_declaration_name():
    architecture = _architecture()
    architecture["contracts"][0]["declaration"] = (
        "def wrong(name: str) -> str: ..."
    )

    result = normalise_architecture(architecture)

    assert result["contracts"][0]["declaration"] == (
        "def greet(name: str) -> str: ..."
    )
    validate_architecture(result, SPECIFICATION)


def test_clean_architecture_normalisation_uses_package_import_name():
    architecture = _architecture()
    architecture["contracts"][0]["qualified_name"] = "greeting.__init__.greet"

    result = normalise_architecture(architecture)

    assert result["contracts"][0]["qualified_name"] == "greeting.greet"
    assert (
        architecture["contracts"][0]["qualified_name"]
        == "greeting.__init__.greet"
    )
    validate_architecture(result, SPECIFICATION)


def test_clean_architecture_normalisation_recovers_assignment_contract_kind():
    architecture = _architecture()
    architecture["contracts"][0].update(
        {
            "qualified_name": "greeting.__all__",
            "kind": "function",
            "declaration": "exports = ['greet']",
        }
    )

    result = normalise_architecture(architecture)

    assert result["contracts"][0]["kind"] == "constant"
    assert result["contracts"][0]["declaration"] == "__all__ = ['greet']"
    assert architecture["contracts"][0]["kind"] == "function"
    validate_architecture(result, SPECIFICATION)


def test_clean_architecture_normalisation_expands_compact_class_method():
    architecture = _architecture()
    architecture["contracts"][0].update(
        {
            "qualified_name": "greeting.Greeter",
            "kind": "class",
            "declaration": "class Wrong: __init__(self, name: str)",
        }
    )

    result = normalise_architecture(architecture)

    assert result["contracts"][0]["declaration"] == (
        "class Greeter:\n"
        "    def __init__(self, name: str):\n"
        "        pass"
    )
    validate_architecture(result, SPECIFICATION)


def test_clean_architecture_normalisation_expands_semicolon_class_methods():
    architecture = _architecture()
    architecture["contracts"][0].update(
        {
            "qualified_name": "greeting.AtomicFileWriter",
            "kind": "class",
            "declaration": (
                "class AtomicFileWriter:\n"
                "    def __init__(self, file_obj: object, temp_filename: str, "
                "real_filename: str); close(self, replace: bool = True) -> None:\n"
                "        pass"
            ),
        }
    )

    result = normalise_architecture(architecture)

    assert result["contracts"][0]["declaration"] == (
        "class AtomicFileWriter:\n"
        "    def __init__(self, file_obj: object, temp_filename: str, "
        "real_filename: str):\n"
        "        ...\n"
        "    def close(self, replace: bool = True) -> None:\n"
        "        pass"
    )
    validate_architecture(result, SPECIFICATION)


def test_clean_architecture_normalisation_splits_cross_module_entry_point():
    architecture = _architecture()
    architecture["contracts"].append(
        {
            "contract_id": "SYM-002",
            "component_id": "CMP-001",
            "qualified_name": "farewell.farewell",
            "kind": "function",
            "declaration": "farewell(name: str) -> str",
            "visibility": "public",
            "requirement_ids": ["FR-001"],
        }
    )
    architecture["entry_points"][0]["contract_ids"].append("SYM-002")

    result = normalise_architecture(architecture)

    assert result["entry_points"] == [
        {
            **architecture["entry_points"][0],
            "contract_ids": ["SYM-001"],
        },
        {
            **architecture["entry_points"][0],
            "entry_point_id": "EP-002",
            "contract_ids": ["SYM-002"],
        },
    ]
    validate_architecture(result, SPECIFICATION)


def test_clean_architecture_keeps_class_members_with_their_provider_module():
    architecture = _architecture()
    architecture["contracts"][0].update(
        {
            "qualified_name": "greeting.Greeter",
            "kind": "class",
            "declaration": "Greeter()",
        }
    )
    architecture["contracts"].append(
        {
            "contract_id": "SYM-002",
            "component_id": "CMP-001",
            "qualified_name": "greeting.Greeter.greet",
            "kind": "function",
            "declaration": "greet(self, name: str) -> str",
            "visibility": "public",
            "requirement_ids": ["FR-001"],
        }
    )
    architecture["entry_points"][0]["contract_ids"].append("SYM-002")

    result = normalise_architecture(architecture)

    assert len(result["entry_points"]) == 1
    assert result["entry_points"][0]["contract_ids"] == ["SYM-001", "SYM-002"]
    validate_architecture(result, SPECIFICATION)


def test_clean_architecture_normalisation_aligns_entry_point_with_contract_owner():
    architecture = _architecture()
    architecture["components"].append(
        {
            "component_id": "CMP-002",
            "purpose": "Own the public greeting contract.",
            "kind": "interface",
            "requirement_ids": ["FR-001", "EH-001"],
            "depends_on": ["CMP-001"],
        }
    )
    architecture["capabilities"][0]["component_ids"].append("CMP-002")
    architecture["contracts"][0]["component_id"] = "CMP-002"

    result = normalise_architecture(architecture)

    assert result["entry_points"][0]["component_id"] == "CMP-002"
    assert architecture["entry_points"][0]["component_id"] == "CMP-001"
    validate_architecture(result, SPECIFICATION)


def test_clean_architect_agent_returns_schema_checked_architecture():
    response = json.dumps(_architecture())
    client = StubTextClient([response])
    agent = CleanArchitectAgent(model="stub/model", chat_client=client)

    result = agent.design(
        SPECIFICATION,
        compatibility_mode="renamed",
        runtime_policy={"adapter": "local-python", "tests": False},
    )

    assert result == _architecture()
    assert client.call_count == 1
    prompt = client.prompts[0]
    assert SPECIFICATION in prompt
    assert '"adapter": "local-python"' in prompt
    assert '<required_behavior_ids>\n["FR-001", "EH-001"]' in prompt
    assert '<required_scenario_ids>\n["TC-001"]' in prompt
    assert "Do not choose project files or generate tests" in prompt
    assert agent.source_reader is None
    assert agent.alias_map is None


def test_clean_architect_agent_revises_semantically_invalid_architecture():
    response = json.dumps(_architecture())
    client = StubTextClient([response])
    agent = CleanArchitectAgent(model="stub/model", chat_client=client)

    result = agent.revise(
        SPECIFICATION,
        _architecture(),
        "Architecture does not allocate requirements to capabilities: EH-001",
        compatibility_mode="renamed",
        runtime_policy={"adapter": "local-python"},
    )

    assert result == _architecture()
    prompt = client.prompts[0]
    assert "<clean_architecture_repair_request>" in prompt
    assert "EH-001" in prompt
    assert '"capability_id": "CAP-001"' in prompt


def test_clean_architecture_validation_does_not_mutate_the_artifact():
    architecture = _architecture()
    original = deepcopy(architecture)

    validate_architecture(architecture, SPECIFICATION)

    assert architecture == original

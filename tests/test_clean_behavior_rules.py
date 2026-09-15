import json
from copy import deepcopy

import pytest
from marshmallow import ValidationError

from packages.agents.clean_team.behavior_probe_agent import CleanBehaviorProbeAgent
from packages.modules.clean.architecture import validate_architecture, CleanArchitectureError
from packages.modules.clean.context import build_scoped_context
from packages.modules.clean.manifest import architecture_sha256
from packages.modules.clean.runner import _compatibility_plan
from packages.modules.supervising.schemas import CleanArchitectureSchema
from packages.modules.supervising.schemas.clean import CleanSymbolContractSchema
from test_clean_architecture import _architecture, SPECIFICATION
from test_clean_manifest import _manifest


def rule(**changes):
    return {"aspect": "results", "statement": "Return a greeting for the supplied name.",
            "requirement_ids": ["FR-001"], "source": "specification", **changes}


def architecture_with_rule(**changes):
    architecture = _architecture()
    architecture["contracts"][0]["behavior_rules"] = [rule(**changes)]
    return architecture


def validate(architecture):
    loaded = CleanArchitectureSchema().load(architecture)
    return validate_architecture(loaded, SPECIFICATION, compatibility_mode="renamed")


def test_legacy_contracts_remain_compatible():
    assert "behavior_rules" not in validate(_architecture())["contracts"][0]


def test_behavior_rules_accept_verbatim_requirement_with_whitespace_variation():
    architecture = architecture_with_rule(statement="Return a greeting\n for the supplied name.")
    assert validate(architecture)["contracts"][0]["behavior_rules"]


@pytest.mark.parametrize("changes, message", [
    ({"statement": "Return JSON."}, "verbatim excerpt"),
    ({"requirement_ids": ["FR-999"]}, "outside its contract"),
    ({"requirement_ids": ["FR-001", "EH-001"]}, "verbatim excerpt"),
    ({"requirement_ids": ["FR-001", "FR-001"]}, "repeats"),
    ({"proposal_id": "PROP-001"}, "cannot label a proposal"),
    ({"source": "clean-proposal"}, "existing clean-proposal"),
    ({"source": "clean-proposal", "proposal_id": "PROP-001"}, "existing clean-proposal"),
])
def test_behavior_rules_reject_unsupported_claims(changes, message):
    with pytest.raises(CleanArchitectureError, match=message):
        validate(architecture_with_rule(**changes))


def proposal_architecture():
    architecture = architecture_with_rule(
        aspect="input-format", source="clean-proposal", proposal_id="PROP-002",
        statement="Read configuration from a caller-supplied TOML file.",
    )
    architecture["proposals"].append({
        "proposal_id": "PROP-002", "field": "contracts.SYM-001.behavior_rules",
        "chosen_value": "Read configuration from a caller-supplied TOML file.",
        "reason": "Explicit Clean choice for an unspecified descriptor format.",
        "source": "clean-proposal", "affected_component_ids": ["CMP-001"],
        "affects_public_compatibility": True,
    })
    return architecture


def test_explicit_design_choice_is_linked_to_ledger_without_becoming_source_evidence():
    architecture = validate(proposal_architecture())
    assert architecture["contracts"][0]["behavior_rules"][0]["source"] == "clean-proposal"


@pytest.mark.parametrize("field,value,message", [
    ("chosen_value", "Read JSON instead.", "chosen_value"),
    ("affected_component_ids", [], "does not cover its component"),
])
def test_proposal_cannot_disagree_with_behavior_rule(field, value, message):
    architecture = proposal_architecture()
    architecture["proposals"][-1][field] = value
    with pytest.raises(CleanArchitectureError, match=message):
        validate(architecture)


@pytest.mark.parametrize("changes", [{"aspect": "anything"}, {"source": "guessed"},
                                      {"statement": ""}, {"requirement_ids": []}])
def test_behavior_rule_schema_rejects_invalid_fields(changes):
    with pytest.raises(ValidationError):
        CleanArchitectureSchema().load(architecture_with_rule(**changes))


def test_rules_survive_plan_schema_and_generation_repair_context_and_reach_probes():
    architecture = validate(architecture_with_rule())
    manifest = _manifest(architecture)
    plan = _compatibility_plan(architecture, manifest)
    plan["symbol_contracts"] = [CleanSymbolContractSchema().load(item) for item in plan["symbol_contracts"]]
    expected = architecture["contracts"][0]["behavior_rules"]
    assert plan["symbol_contracts"][0]["behavior_rules"] == expected
    scoped = build_scoped_context(SPECIFICATION, plan, ["src/greeting.py"])
    assert scoped.plan["symbol_contracts"][0]["behavior_rules"] == expected
    prompt = CleanBehaviorProbeAgent._prompt("request", SPECIFICATION, architecture, manifest)
    encoded = prompt.split('<validated_architecture>')[1].split('</validated_architecture>')[0]
    assert json.loads(encoded)["contracts"][0]["behavior_rules"] == expected
    plan["symbol_contracts"][0]["behavior_rules"][0]["statement"] = "changed"
    assert architecture["contracts"][0]["behavior_rules"] == expected
    assert expected[0]["statement"] == "Return a greeting for the supplied name."


def test_behavior_rule_change_invalidates_architecture_hash():
    architecture = architecture_with_rule()
    changed = deepcopy(architecture)
    changed["contracts"][0]["behavior_rules"][0]["aspect"] = "arguments"
    assert architecture_sha256(architecture) != architecture_sha256(changed)

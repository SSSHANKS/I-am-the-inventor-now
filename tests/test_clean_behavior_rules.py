import json
from copy import deepcopy

import pytest
from marshmallow import ValidationError

from packages.agents.clean_team.behavior_probe_agent import CleanBehaviorProbeAgent
from packages.agents.clean_team.prompts import BEHAVIOR_PROBE_INSTRUCTION
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


def test_behavior_rule_evidence_ignores_evidence_citation_label_only():
    specification = SPECIFICATION.replace(
        "Return a greeting for the supplied name.",
        "Return a greeting for the supplied name (Evidence: EV-001).",
    )
    architecture = architecture_with_rule(
        statement="Return a greeting for the supplied name.",
        evidence=[{
            "requirement_id": "FR-001",
            "excerpt": "Return a greeting for the supplied name (EV-001)",
        }],
    )

    loaded = CleanArchitectureSchema().load(architecture)
    assert validate_architecture(loaded, specification, compatibility_mode="renamed")


def test_behavior_rule_evidence_ignores_parenthesized_evidence_citations_only():
    specification = SPECIFICATION.replace(
        "Return a greeting for the supplied name.",
        "Return a greeting (EV-001, EV-002) for the supplied name.",
    )
    architecture = architecture_with_rule(
        statement="Return a greeting for the supplied name.",
        evidence=[{
            "requirement_id": "FR-001",
            "excerpt": "Return a greeting for the supplied name.",
        }],
    )

    loaded = CleanArchitectureSchema().load(architecture)
    assert validate_architecture(loaded, specification, compatibility_mode="renamed")


def test_behavior_rule_evidence_does_not_ignore_behavioral_parentheses():
    specification = SPECIFICATION.replace(
        "Return a greeting for the supplied name.",
        "Return a greeting (in uppercase) for the supplied name.",
    )
    architecture = architecture_with_rule(
        statement="Return a greeting for the supplied name.",
        evidence=[{
            "requirement_id": "FR-001",
            "excerpt": "Return a greeting for the supplied name.",
        }],
    )

    loaded = CleanArchitectureSchema().load(architecture)
    with pytest.raises(CleanArchitectureError, match="verbatim excerpt"):
        validate_architecture(loaded, specification, compatibility_mode="renamed")


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


def test_behavior_probe_instruction_isolates_phases_and_exercises_exports():
    instruction = " ".join(BEHAVIOR_PROBE_INSTRUCTION.split())
    assert "Use a fresh public object" in instruction
    assert "every earlier registration remains part of the setup" in instruction
    assert "import the symbol through the declared public package root" in instruction


def test_behavior_probe_revision_can_target_only_missing_requirements():
    architecture = validate(architecture_with_rule())
    prompt = CleanBehaviorProbeAgent._prompt(
        "repair",
        SPECIFICATION,
        architecture,
        _manifest(architecture),
        probes={"schema_version": 1, "probes": []},
        diagnostic="AC-001 is missing",
        target_capability=architecture["capabilities"][0],
        target_requirement_ids=["AC-001"],
    )

    assert '<target_requirement_ids>\n["AC-001"]' in prompt
    assert "do not repeat or rewrite them" in prompt


def test_behavior_rule_change_invalidates_architecture_hash():
    architecture = architecture_with_rule()
    changed = deepcopy(architecture)
    changed["contracts"][0]["behavior_rules"][0]["aspect"] = "arguments"
    assert architecture_sha256(architecture) != architecture_sha256(changed)


def combined_architecture():
    return architecture_with_rule(
        statement="Greet a supplied name, rejecting empty names.",
        requirement_ids=["FR-001", "EH-001"],
        evidence=[
            {"requirement_id": "FR-001", "excerpt": "Return a greeting for the supplied name."},
            {"requirement_id": "EH-001", "excerpt": "Reject an empty name."},
        ],
    )


def test_synthesis_accepts_distinct_evidence_per_requirement():
    architecture = validate(combined_architecture())
    rules = architecture['contracts'][0]['behavior_rules']
    plan = _compatibility_plan(architecture, _manifest(architecture))
    assert plan['symbol_contracts'][0]['behavior_rules'] == rules
    assert len(rules[0]['evidence']) == 2


@pytest.mark.parametrize('change, message', [
    ('missing', 'exactly once'), ('duplicate', 'exactly once'), ('foreign', 'exactly once'),
    ('wrong-quote', 'verbatim excerpt'), ('swapped', 'verbatim excerpt'),
])
def test_per_requirement_evidence_cannot_be_missing_or_misattributed(change, message):
    architecture = combined_architecture()
    evidence = architecture['contracts'][0]['behavior_rules'][0]['evidence']
    if change == 'missing': evidence.pop()
    elif change == 'duplicate': evidence.append(deepcopy(evidence[0]))
    elif change == 'foreign': evidence[0]['requirement_id'] = 'FR-999'
    elif change == 'wrong-quote': evidence[0]['excerpt'] = 'Return JSON.'
    else: evidence[0]['excerpt'], evidence[1]['excerpt'] = evidence[1]['excerpt'], evidence[0]['excerpt']
    with pytest.raises(CleanArchitectureError, match=message):
        validate(architecture)


def test_proposal_cannot_disguise_itself_as_quoted_evidence():
    architecture = proposal_architecture()
    architecture['contracts'][0]['behavior_rules'][0]['evidence'] = [
        {'requirement_id': 'FR-001', 'excerpt': 'Return a greeting for the supplied name.'}
    ]
    with pytest.raises(CleanArchitectureError, match='cannot label a proposal'):
        validate(architecture)

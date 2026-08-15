"""Border: scanners propose; LLM adjudicates soft findings; hard leaks auto-fail."""

import json
from dataclasses import replace

import pytest

from packages.agents.base_agent import StubTextClient
from packages.agents.border_team import BorderGateAgent
from packages.modules.border import (
    ADJUDICATED_POLICY,
    STRICT_POLICY,
    BorderGateError,
    evaluate_crossing_artifacts,
    is_soft_finding,
    review_text,
    strip_border_review_section,
)
from packages.modules.border.gate import DECISION_DISMISS
from packages.modules.boundary import (
    BORDER_REVIEW,
    AliasMap,
    annotate_border_review,
    find_residual_originals,
)
from packages.modules.storing import Storage
from packages.modules.supervising.schemas import BorderVerdictSchema

CLEAN_SPEC = """\
# PROJECT-X

## Project Purpose

Stores records on disk and returns them by name.

## Evidence References

- EV-001 supports the load behaviour.
"""

NEUTRAL_MANIFEST = {
    "project_name": "PROJECT-X",
    "summary": "PROJECT-X is to be implemented from behavioural specification alone.",
    "documentation_unit_count": 1,
    "code_unit_count": 1,
    "border_review": [],
}

EMPTY_CATALOGUE = {"entries": [], "border_review": []}


def _alias_map_with_widget() -> AliasMap:
    alias_map = AliasMap(project_name="PROJECT-X")
    alias_map.component_alias("WidgetStore", kind="class")
    alias_map.evidence_id("src/store.py", 1, 10)
    alias_map.register_project("https://example.invalid/owner/original.git")
    return alias_map


def _dismiss_all_stub(prompt: str) -> str:
    try:
        block = prompt.split("<border_findings>", 1)[1].split("</border_findings>", 1)[0]
        findings = json.loads(block)
    except (IndexError, json.JSONDecodeError):
        findings = []
    return json.dumps(
        {
            "decisions": [
                {
                    "finding_id": item.get("finding_id", "BF-000"),
                    "decision": "dismiss",
                    "rationale": "test stub dismisses soft findings",
                }
                for item in findings
                if isinstance(item, dict)
            ]
        }
    )


def _border_agent(reply=_dismiss_all_stub) -> BorderGateAgent:
    return BorderGateAgent(model="stub/border", chat_client=StubTextClient(reply))


def test_strip_removes_only_the_advisory_appendix():
    body = "# Title\n\nClean prose.\n"
    annotated = annotate_border_review(
        body,
        find_residual_originals("mentions WidgetStore", _alias_map_with_widget()),
    )
    assert f"## {BORDER_REVIEW}" in annotated
    stripped = strip_border_review_section(annotated)
    assert stripped == body.rstrip()
    assert "WidgetStore" not in stripped


def test_clean_specification_passes_border():
    alias_map = _alias_map_with_widget()
    verdict = evaluate_crossing_artifacts(
        alias_map=alias_map,
        specification=CLEAN_SPEC,
        evidence_catalogue=EMPTY_CATALOGUE,
        neutral_manifest=NEUTRAL_MANIFEST,
        source_texts=(),
    )
    assert verdict.passed
    assert verdict.finding_count == 0
    assert verdict.policy == STRICT_POLICY


def test_hard_identifier_leak_fails_even_if_adjudicator_would_dismiss():
    alias_map = _alias_map_with_widget()
    leaked = CLEAN_SPEC + "\nThen call WidgetStore() to continue.\n"

    def dismiss_all(soft):
        return [
            replace(r, decision=DECISION_DISMISS, rationale="should not see hard findings")
            for r in soft
        ]

    verdict = evaluate_crossing_artifacts(
        alias_map=alias_map,
        specification=leaked,
        adjudicate=dismiss_all,
    )
    assert not verdict.passed
    assert verdict.policy == ADJUDICATED_POLICY
    widget = [item for item in verdict.findings if item["original"] == "WidgetStore"]
    assert widget and all(item["decision"] == "fail" for item in widget)


def test_soft_finding_fails_under_strict_policy_without_adjudicator():
    alias_map = _alias_map_with_widget()
    prose = "The component performs a deep WidgetStore."
    records = review_text("specification.md", prose, alias_map)
    assert records
    if not any(is_soft_finding(r) for r in records):
        pytest.skip("phrasing classified as hard; soft path covered elsewhere")
    verdict = evaluate_crossing_artifacts(alias_map=alias_map, specification=prose)
    assert not verdict.passed
    assert verdict.policy == STRICT_POLICY


def test_llm_adjudicator_can_dismiss_soft_findings():
    alias_map = _alias_map_with_widget()
    prose = "The component performs a deep WidgetStore."
    records = review_text("specification.md", prose, alias_map)
    if not any(is_soft_finding(r) for r in records):
        pytest.skip("phrasing classified as hard; soft path covered elsewhere")

    def dismiss_all(soft):
        return [
            replace(r, decision=DECISION_DISMISS, rationale="ordinary English, not a type name")
            for r in soft
        ]

    verdict = evaluate_crossing_artifacts(
        alias_map=alias_map,
        specification=prose,
        adjudicate=dismiss_all,
    )
    assert verdict.passed
    assert verdict.finding_count == 0
    assert any(item["decision"] == "dismiss" for item in verdict.findings)


def test_command_content_leak_fails_border():
    alias_map = AliasMap()
    prose = "Install with `pip install original-project` then run the service."
    verdict = evaluate_crossing_artifacts(
        alias_map=alias_map,
        specification=prose,
        adjudicate=lambda soft: [
            replace(r, decision=DECISION_DISMISS, rationale="nope") for r in soft
        ],
    )
    assert not verdict.passed
    assert any(item["kind"] == "command-shaped text" for item in verdict.findings)


def test_lifted_prose_from_dirty_corpus_fails_border():
    alias_map = AliasMap()
    source = "Rejects malformed records during validation of incoming payloads carefully."
    prose = f"Behaviour: {source}"
    verdict = evaluate_crossing_artifacts(
        alias_map=alias_map,
        specification=prose,
        source_texts=(source,),
        adjudicate=lambda soft: [
            replace(r, decision=DECISION_DISMISS, rationale="nope") for r in soft
        ],
    )
    assert not verdict.passed
    assert any(item["kind"] == "verbatim source-document prose" for item in verdict.findings)


def test_advisory_appendix_does_not_double_count():
    alias_map = _alias_map_with_widget()
    leaked_body = CLEAN_SPEC + "\nUses WidgetStore for storage.\n"
    annotated = annotate_border_review(
        leaked_body,
        find_residual_originals(leaked_body, alias_map),
    )
    body_findings = review_text(
        "specification.md",
        strip_border_review_section(annotated),
        alias_map,
    )
    full_findings = review_text("specification.md", annotated, alias_map)
    assert len(body_findings) >= 1
    assert len(full_findings) > len(body_findings)


def test_plan_content_rules_run_at_border():
    alias_map = AliasMap()
    plan = (
        '{"stage":"documentation","summary":"ok","mini_tasks":['
        '{"task_id":"DOC-001","task_type":"extract","output_field":"features",'
        '"input_refs":[],"requirements":["run pip install leaked-tool"],"min_items":1}]}'
    )
    verdict = evaluate_crossing_artifacts(
        alias_map=alias_map,
        plans={"doc_plan.json": plan},
        adjudicate=lambda soft: [
            replace(r, decision=DECISION_DISMISS, rationale="nope") for r in soft
        ],
    )
    assert not verdict.passed
    assert "doc_plan.json" in verdict.failed_artifacts


def test_gate_agent_calls_model_and_still_fails_hard_leak(tmp_path):
    alias_map = _alias_map_with_widget()
    storage = Storage(artifacts_dir=tmp_path / "artifacts", run_name="border-run")
    agent = _border_agent(_dismiss_all_stub)

    with pytest.raises(BorderGateError) as raised:
        agent.enforce(
            storage=storage,
            alias_map=alias_map,
            specification=CLEAN_SPEC + "\nThen call WidgetStore() to continue.\n",
            evidence_catalogue=EMPTY_CATALOGUE,
            neutral_manifest=NEUTRAL_MANIFEST,
        )

    assert raised.value.verdict.finding_count >= 1
    payload = storage.read_json("border_verdict.json")
    BorderVerdictSchema().load(payload)
    assert payload["status"] == "fail"
    assert payload["policy"] == ADJUDICATED_POLICY


def test_gate_agent_llm_dismisses_soft_and_passes(tmp_path):
    alias_map = _alias_map_with_widget()
    prose = CLEAN_SPEC + "\nThe component performs a deep WidgetStore.\n"
    records = review_text("specification.md", prose, alias_map)
    if not any(is_soft_finding(r) for r in records):
        pytest.skip("phrasing classified as hard; soft path covered elsewhere")

    storage = Storage(artifacts_dir=tmp_path / "artifacts", run_name="border-soft")
    verdict = _border_agent(_dismiss_all_stub).enforce(
        storage=storage,
        alias_map=alias_map,
        specification=prose,
        evidence_catalogue=EMPTY_CATALOGUE,
        neutral_manifest=NEUTRAL_MANIFEST,
    )
    assert verdict.passed
    assert any(item["decision"] == "dismiss" for item in verdict.findings)
    assert storage.read_json("border_verdict.json")["status"] == "pass"


def test_gate_agent_pass_writes_verdict(tmp_path):
    alias_map = _alias_map_with_widget()
    storage = Storage(artifacts_dir=tmp_path / "artifacts", run_name="border-pass")
    verdict = _border_agent().enforce(
        storage=storage,
        alias_map=alias_map,
        specification=CLEAN_SPEC,
        evidence_catalogue=EMPTY_CATALOGUE,
        neutral_manifest=NEUTRAL_MANIFEST,
    )
    assert verdict.passed
    payload = storage.read_json("border_verdict.json")
    assert payload["status"] == "pass"
    assert payload["finding_count"] == 0

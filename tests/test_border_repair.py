"""Border repair loop: refuse → Dirty rewrite → re-gate."""

import json

from packages.agents.base_agent import StubTextClient
from packages.agents.border_team import gate_with_repairs, scrub_failed_originals
from packages.agents.border_team.repair_loop import failing_findings
from packages.agents.dirt_team.spec_synthesizer_agent import SpecSynthesizerAgent
from packages.modules.border import BorderGateError, evaluate_crossing_artifacts
from packages.modules.boundary import AliasMap
from packages.modules.storing import Storage
from tests.test_border import (
    CLEAN_SPEC,
    EMPTY_CATALOGUE,
    NEUTRAL_MANIFEST,
    _alias_map_with_widget,
    _border_agent,
    _dismiss_all_stub,
)


def test_scrub_failed_originals_removes_makefile_style_leaks():
    alias_map = AliasMap()
    text = "See detailed Makefile target variables for build flags."
    findings = [
        {
            "original": "Makefile",
            "alias": None,
            "kind": "original reference",
            "decision": "fail",
        }
    ]
    scrubbed = scrub_failed_originals(text, findings, alias_map)
    assert "Makefile" not in scrubbed
    assert "build configuration" in scrubbed


def test_scrub_failed_originals_preserves_identifiers_border_dismissed():
    alias_map = AliasMap()
    alias_map.component_alias("WidgetStore")
    alias_map.component_alias("connect", kind="function")
    text = "Applications connect to WidgetStore through a stable interface."
    findings = [
        {
            "original": "WidgetStore",
            "alias": alias_map.alias_for("WidgetStore"),
            "kind": "identifier",
            "decision": "fail",
        }
    ]

    scrubbed = scrub_failed_originals(text, findings, alias_map)

    assert "WidgetStore" not in scrubbed
    assert "connect" in scrubbed
    assert alias_map.alias_for("connect") not in scrubbed


def test_scrub_failed_originals_removes_inline_dirty_advisories():
    text = (
        "# Specification\n\nUseful behavior.\n\n"
        "BORDER-REVIEW: names were omitted from this paragraph.\n"
    )

    scrubbed = scrub_failed_originals(text, [], AliasMap())

    assert "Useful behavior." in scrubbed
    assert "BORDER-REVIEW" not in scrubbed


def test_repair_loop_passes_after_deterministic_scrub(tmp_path):
    """Hard leak in the body is scrubbed on repair; Border then passes."""
    alias_map = _alias_map_with_widget()
    storage = Storage(artifacts_dir=tmp_path / "artifacts", run_name="repair-pass")
    leaked = CLEAN_SPEC + "\nThen call WidgetStore() to continue.\n"

    # First Border pass fails (hard). Repair scrub removes WidgetStore via alias.
    # Second pass: dismiss any soft leftovers.
    agent = _border_agent(_dismiss_all_stub)
    synthesizer = SpecSynthesizerAgent(
        model="stub/spec",
        chat_client=StubTextClient(
            lambda prompt: prompt.split("<specification_under_repair>", 1)[1]
            .split("</specification_under_repair>", 1)[0]
            .strip()
            if "<specification_under_repair>" in prompt
            else CLEAN_SPEC
        ),
        alias_map=alias_map,
    )

    def repair(spec: str, failing: list) -> str:
        return synthesizer.repair(spec, failing, alias_map)

    final_spec, verdict = gate_with_repairs(
        border=agent,
        repair=repair,
        storage=storage,
        alias_map=alias_map,
        specification=leaked,
        max_repairs=2,
        evidence_catalogue=EMPTY_CATALOGUE,
        neutral_manifest=NEUTRAL_MANIFEST,
    )
    assert verdict.passed
    assert "WidgetStore" not in final_spec
    assert storage.exists("specification.border-fail-1.md")
    assert storage.exists("specification.border-repair-1.md")
    assert storage.exists("border_verdict.round-1.json")
    assert storage.read_json("border_verdict.json")["status"] == "pass"


def test_repair_loop_raises_when_repairs_exhausted(tmp_path):
    alias_map = _alias_map_with_widget()
    storage = Storage(artifacts_dir=tmp_path / "artifacts", run_name="repair-fail")
    leaked = CLEAN_SPEC + "\nThen call WidgetStore() to continue.\n"
    agent = _border_agent(_dismiss_all_stub)

    def noop_repair(spec: str, failing: list) -> str:
        # Keep the leak so Border keeps failing.
        return spec

    try:
        gate_with_repairs(
            border=agent,
            repair=noop_repair,
            storage=storage,
            alias_map=alias_map,
            specification=leaked,
            max_repairs=1,
            evidence_catalogue=EMPTY_CATALOGUE,
            neutral_manifest=NEUTRAL_MANIFEST,
        )
        raised = False
    except BorderGateError as error:
        raised = True
        assert error.verdict.finding_count >= 1

    assert raised
    assert storage.exists("specification.border-fail-1.md")
    assert storage.exists("specification.border-fail-2.md")


def test_repair_loop_does_not_spend_calls_on_non_spec_findings(tmp_path):
    storage = Storage(artifacts_dir=tmp_path / "artifacts", run_name="unrepairable-plan")
    calls = 0

    def repair(spec: str, failing: list) -> str:
        nonlocal calls
        calls += 1
        return spec

    try:
        gate_with_repairs(
            border=_border_agent(_dismiss_all_stub),
            repair=repair,
            storage=storage,
            alias_map=AliasMap(),
            specification=CLEAN_SPEC,
            plans={"code_facts_plan.json": '{"summary":"run python -bb"}'},
            max_repairs=3,
        )
        raised = False
    except BorderGateError as error:
        raised = True
        assert error.verdict.failed_artifacts == ["code_facts_plan.json"]

    assert raised
    assert calls == 0
    assert storage.exists("border_verdict.round-1.json")
    assert not storage.exists("border_verdict.round-2.json")


def test_spec_repair_unwraps_document_json_envelope():
    alias_map = AliasMap()
    synthesizer = SpecSynthesizerAgent(
        model="stub/spec",
        chat_client=StubTextClient(
            lambda prompt: json.dumps({"document": CLEAN_SPEC})
        ),
        alias_map=alias_map,
    )

    repaired = synthesizer.repair(CLEAN_SPEC, [], alias_map)

    assert repaired == CLEAN_SPEC.strip()


def test_spec_repair_uses_scrubbed_fallback_for_unknown_json_envelope():
    alias_map = _alias_map_with_widget()
    synthesizer = SpecSynthesizerAgent(
        model="stub/spec",
        chat_client=StubTextClient(lambda prompt: '{"items":[]}'),
        alias_map=alias_map,
    )
    leaked = CLEAN_SPEC + "\nThen call WidgetStore() to continue.\n"
    findings = failing_findings(
        evaluate_crossing_artifacts(alias_map=alias_map, specification=leaked)
    )

    repaired = synthesizer.repair(leaked, findings, alias_map)

    assert repaired.startswith("# ")
    assert "WidgetStore" not in repaired
    assert "Component A" in repaired


def test_failing_findings_only_lists_fails():
    alias_map = _alias_map_with_widget()
    verdict = evaluate_crossing_artifacts(
        alias_map=alias_map,
        specification=CLEAN_SPEC + "\nThen call WidgetStore() to continue.\n",
    )
    failing = failing_findings(verdict)
    assert failing
    assert all(item["decision"] == "fail" for item in failing)


def test_spec_repair_falls_back_when_model_drops_sections_and_evidence():
    alias_map = _alias_map_with_widget()
    leaked = CLEAN_SPEC + "\nThen call WidgetStore() to continue. Evidence: EV-999.\n"
    findings = failing_findings(
        evaluate_crossing_artifacts(alias_map=alias_map, specification=leaked)
    )
    synthesizer = SpecSynthesizerAgent(
        model="stub/spec",
        chat_client=StubTextClient(
            ['{"document":"# Specification\\n\\nShort replacement."}']
        ),
        alias_map=alias_map,
    )

    repaired = synthesizer.repair(leaked, findings, alias_map)

    assert "WidgetStore" not in repaired
    assert "EV-999" in repaired
    assert "## Project Purpose" in repaired


def test_spec_repair_falls_back_when_model_writes_inline_border_marker():
    alias_map = _alias_map_with_widget()
    leaked = CLEAN_SPEC + "\nThen call WidgetStore() to continue.\n"
    findings = failing_findings(
        evaluate_crossing_artifacts(alias_map=alias_map, specification=leaked)
    )
    contaminated = CLEAN_SPEC + "\nBORDER-REVIEW: revisit wording.\n"
    synthesizer = SpecSynthesizerAgent(
        model="stub/spec",
        chat_client=StubTextClient([json.dumps({"document": contaminated})]),
        alias_map=alias_map,
    )

    repaired = synthesizer.repair(leaked, findings, alias_map)

    assert "BORDER-REVIEW" not in repaired

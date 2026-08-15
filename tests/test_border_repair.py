"""Border repair loop: refuse → Dirty rewrite → re-gate."""

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


def test_failing_findings_only_lists_fails():
    alias_map = _alias_map_with_widget()
    verdict = evaluate_crossing_artifacts(
        alias_map=alias_map,
        specification=CLEAN_SPEC + "\nThen call WidgetStore() to continue.\n",
    )
    failing = failing_findings(verdict)
    assert failing
    assert all(item["decision"] == "fail" for item in failing)

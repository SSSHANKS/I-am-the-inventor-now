"""Step 1.5: EV-id driven plans, the neutrality gate, and the planner/judge loop."""

import json

import pytest

from packages.agents.dirt_team.plan_judge_agent import SCORED_PILLARS, total_score
from packages.agents.planning.loop import (
    MAX_ROUNDS,
    PlanAttempt,
    enforce_neutrality,
    run_plan_loop,
    select_best,
)
from packages.modules.boundary import (
    AliasMap,
    evidence_catalogue,
    mint_evidence_ids,
    register_code_identifiers,
)
from packages.modules.supervising.schemas import PlanningSchema, get_validation_errors
from packages.modules.supervising.verifiers.planning import PlanVerifier

CODE_INDEX = {
    "classes": [
        {
            "name": "Calculadora",
            "methods": ["start"],
            "evidence": {"file": "app/ui.py", "line_start": 4, "line_end": 9, "excerpt": "class x"},
        }
    ],
    "functions": [
        {
            "name": "calculation",
            "qualified_name": "Calculador.calculation",
            "evidence": {
                "file": "app/calc.py",
                "line_start": 12,
                "line_end": 20,
                "excerpt": "def x",
            },
        }
    ],
    "analysis_targets": [
        {
            "target": "main.py",
            "target_type": "file",
            "reason": "file contains a Python main guard",
            "evidence": {"file": "main.py", "line_start": 1, "line_end": 1, "excerpt": "import x"},
        }
    ],
    "imports": [],
    "configs": [],
    "entrypoints": [],
}
DOC_INDEX = {
    "sections": [
        {
            "title": "Setup",
            "evidence": {"file": "README.md", "line_start": 3, "line_end": 8, "excerpt": "# Setup"},
        }
    ],
    "commands": [],
    "code_blocks": [],
    "headings": [],
}


@pytest.fixture
def mapped():
    alias_map = AliasMap()
    register_code_identifiers(CODE_INDEX, alias_map)
    mint_evidence_ids(alias_map, CODE_INDEX, DOC_INDEX)
    return alias_map


# --- evidence ids exist before planning -------------------------------------


def test_evidence_ids_are_minted_for_the_whole_index(mapped):
    """They used to be minted during spec neutralisation, long after every planner ran."""
    assert len(mapped._by_alias) >= 4
    assert mapped.location_for("EV-001") is not None


def test_an_evidence_id_round_trips_to_a_real_location(mapped):
    catalogue = evidence_catalogue(mapped, CODE_INDEX, DOC_INDEX)
    for entry in catalogue:
        file, start, end = mapped.location_for(entry["evidence_id"])
        assert isinstance(file, str) and file
        assert isinstance(start, int) and isinstance(end, int)


def test_the_catalogue_a_planner_sees_carries_no_originals(mapped):
    """The planner cannot leak what it was never shown."""
    from packages.modules.boundary import find_residual_originals

    blob = json.dumps(evidence_catalogue(mapped, CODE_INDEX, DOC_INDEX))
    assert find_residual_originals(blob, mapped) == []
    for original in ("Calculadora", "main.py", "app/calc.py", "README.md"):
        assert original not in blob


# --- the plan schema is EV-id only ------------------------------------------


def _plan(**overrides):
    payload = {
        "stage": "documentation",
        "summary": "concentrated on the non-obvious parts",
        "mini_tasks": [
            {
                "task_id": "DOC-001",
                "task_type": "extract_validation_rules",
                "output_field": "summary",
                "input_refs": [{"source": "evidence_catalogue", "evidence_id": "EV-001"}],
                "requirements": ["state the exact conditions"],
                "min_items": 1,
            }
        ],
    }
    return payload | overrides


def test_a_plan_citing_only_evidence_ids_validates():
    assert get_validation_errors(json.dumps(_plan()), PlanningSchema()) is None


def test_an_input_ref_without_an_evidence_id_is_rejected():
    plan = _plan()
    plan["mini_tasks"][0]["input_refs"] = [{"source": "evidence_catalogue"}]
    assert get_validation_errors(json.dumps(plan), PlanningSchema()) is not None


# --- structural verification -------------------------------------------------


def test_unresolvable_evidence_is_an_error(mapped):
    plan = _plan()
    plan["mini_tasks"][0]["input_refs"] = [
        {"source": "evidence_catalogue", "evidence_id": "EV-999"}
    ]
    result = PlanVerifier().verify(plan, "documentation", alias_map=mapped)
    assert result["valid"] is False
    assert any("EV-999" in i["message"] for i in result["issues"])


def test_a_ref_carrying_a_file_path_is_an_error(mapped):
    """A plan reaching around the boundary must not pass quietly."""
    plan = _plan()
    plan["mini_tasks"][0]["input_refs"] = [
        {"source": "evidence_catalogue", "evidence_id": "EV-001", "file": "app/ui.py"}
    ]
    result = PlanVerifier().verify(plan, "documentation", alias_map=mapped)
    assert result["valid"] is False
    assert any("section 2" in i["message"] for i in result["issues"])


def test_duplicate_task_ids_are_rejected(mapped):
    plan = _plan()
    plan["mini_tasks"].append(dict(plan["mini_tasks"][0]))
    result = PlanVerifier().verify(plan, "documentation", alias_map=mapped)
    assert any("Duplicate task_id" in i["message"] for i in result["issues"])


def test_an_output_field_outside_the_stage_is_rejected(mapped):
    plan = _plan()
    plan["mini_tasks"][0]["output_field"] = "behaviors"
    result = PlanVerifier().verify(plan, "documentation", alias_map=mapped)
    assert result["valid"] is False


def _covering_plan(stage="documentation", **overrides):
    """A plan that satisfies PRESENCE: every allowed field planned or excused."""
    from packages.modules.supervising.verifiers.planning import OUTPUT_FIELDS_BY_STAGE

    fields = sorted(OUTPUT_FIELDS_BY_STAGE[stage])
    payload = {
        "stage": stage,
        "summary": "covered everything, dug into the hard parts",
        "mini_tasks": [
            {
                "task_id": f"T-{index:03d}",
                "task_type": "extract",
                "output_field": field,
                "input_refs": [{"source": "evidence_catalogue", "evidence_id": "EV-001"}],
                "requirements": ["state the exact conditions"],
                "min_items": 1,
            }
            for index, field in enumerate(fields, start=1)
        ],
    }
    return payload | overrides


def test_a_plan_covering_every_field_passes(mapped):
    assert (
        PlanVerifier().verify(_covering_plan(), "documentation", alias_map=mapped)["valid"] is True
    )


# --- presence: coverage is the floor ----------------------------------------


def test_a_field_with_no_task_and_no_reason_is_rejected(mapped):
    """The first live run left 11 of 13 sections unplanned and nothing objected."""
    plan = _covering_plan()
    plan["mini_tasks"] = plan["mini_tasks"][:1]
    result = PlanVerifier().verify(plan, "documentation", alias_map=mapped)
    assert result["valid"] is False
    assert any("no task and was not declared" in i["message"] for i in result["issues"])


def test_a_core_section_can_never_be_excused(mapped):
    plan = _covering_plan("specification")
    plan["mini_tasks"] = [t for t in plan["mini_tasks"] if t["output_field"] != "error_handling"]
    plan["not_applicable"] = [
        {
            "output_field": "error_handling",
            "justification": "This project is small and has no meaningful error paths at all.",
        }
    ]
    result = PlanVerifier().verify(plan, "specification", alias_map=mapped)
    assert result["valid"] is False
    assert any("core section" in i["message"] for i in result["issues"])


def test_a_non_core_section_may_be_excused_with_a_specific_reason(mapped):
    plan = _covering_plan("specification")
    plan["mini_tasks"] = [t for t in plan["mini_tasks"] if t["output_field"] != "configuration"]
    plan["not_applicable"] = [
        {
            "output_field": "configuration",
            "justification": (
                "This project reads no settings of any kind at start-up and exposes no "
                "tunable values, so there is no configuration surface to specify."
            ),
        }
    ]
    assert PlanVerifier().verify(plan, "specification", alias_map=mapped)["valid"] is True


def test_a_stub_justification_is_rejected(mapped):
    plan = _covering_plan("specification")
    plan["mini_tasks"] = [t for t in plan["mini_tasks"] if t["output_field"] != "configuration"]
    plan["not_applicable"] = [{"output_field": "configuration", "justification": "N/A"}]
    result = PlanVerifier().verify(plan, "specification", alias_map=mapped)
    assert result["valid"] is False
    assert any("specific to this section" in i["message"] for i in result["issues"])


def test_the_same_justification_twice_is_templating(mapped):
    """Exactly the abuse the first live run produced: one sentence, two sections."""
    plan = _covering_plan("specification")
    dropped = {"configuration", "test_candidates"}
    plan["mini_tasks"] = [t for t in plan["mini_tasks"] if t["output_field"] not in dropped]
    shared = "This area is not relevant to a project of this size and shape at all."
    plan["not_applicable"] = [
        {"output_field": "configuration", "justification": shared},
        {"output_field": "test_candidates", "justification": shared},
    ]
    result = PlanVerifier().verify(plan, "specification", alias_map=mapped)
    assert result["valid"] is False
    assert any("templated" in i["message"] for i in result["issues"])


# --- the neutrality gate -----------------------------------------------------


def test_a_clean_plan_passes_the_gate_untouched(mapped):
    plan = json.dumps(_plan())
    result, neutral, leaks, scrubbed = enforce_neutrality(plan, mapped)
    assert neutral and not leaks and not scrubbed
    assert result == plan


def test_a_leaking_plan_is_scrubbed_then_passes(mapped):
    """Scrub-then-recheck: a first-pass leak is not fatal if scrubbing fixes it."""
    plan = json.dumps(_plan(summary="focus on the Calculadora component"))
    result, neutral, leaks, scrubbed = enforce_neutrality(plan, mapped)
    assert scrubbed and neutral and not leaks
    assert "Calculadora" not in result


def test_neutrality_is_a_gate_and_never_outranked_by_score():
    """A leaking plan loses to a lower-scoring neutral one - the whole point of pillar 4."""
    leaking = PlanAttempt(round_number=1, plan="{}", neutral=False, score=15)
    clean = PlanAttempt(round_number=2, plan="{}", neutral=True, score=3)
    assert select_best([leaking, clean]) is clean


def test_ties_go_to_the_earlier_round():
    first = PlanAttempt(round_number=1, plan="a", neutral=True, score=9)
    later = PlanAttempt(round_number=3, plan="b", neutral=True, score=9)
    assert select_best([first, later]) is first


def test_no_neutral_version_degrades_visibly_instead_of_hard_failing(mapped):
    """Decided in Q20: ship the scrubbed best plus a loud marker, do not kill the stage."""

    def draft(_feedback):
        return "cite app/secret_module.py directly"

    outcome = run_plan_loop(
        draft=draft, judge=lambda p: {}, alias_map=mapped, stage="documentation", max_rounds=2
    )
    assert outcome.degraded
    assert outcome.plan
    assert any("BORDER-REVIEW" in note for note in outcome.border_review)


# --- the loop ----------------------------------------------------------------


def test_the_loop_is_bounded_even_when_the_judge_keeps_complaining(mapped):
    calls = {"drafts": 0}

    def draft(_feedback):
        calls["drafts"] += 1
        return json.dumps(_plan())

    def judge(_plan):
        return {"strongest_objection": "more", "actions": ["do more"], "_total_score": 5}

    outcome = run_plan_loop(
        draft=draft, judge=judge, alias_map=mapped, stage="documentation", max_rounds=MAX_ROUNDS
    )
    assert calls["drafts"] == MAX_ROUNDS
    assert outcome.rounds_used == MAX_ROUNDS


def test_the_loop_stops_early_when_the_judge_has_nothing_actionable(mapped):
    calls = {"drafts": 0}

    def draft(_feedback):
        calls["drafts"] += 1
        return json.dumps(_plan())

    outcome = run_plan_loop(
        draft=draft,
        judge=lambda p: {"strongest_objection": "", "actions": [], "_total_score": 12},
        alias_map=mapped,
        stage="documentation",
    )
    assert calls["drafts"] == 1
    assert outcome.score == 12


def test_judge_feedback_is_scrubbed_before_it_reaches_the_planner(mapped):
    """Required, not optional: unscrubbed feedback makes the loop manufacture a leak."""
    seen: list[list[str]] = []

    def draft(feedback):
        seen.append(feedback)
        return json.dumps(_plan())

    def judge(_plan):
        return {
            "strongest_objection": "you ignored the Calculadora component",
            "actions": ["add a task for Calculador.calculation"],
            "_total_score": 6,
        }

    run_plan_loop(draft=draft, judge=judge, alias_map=mapped, stage="documentation", max_rounds=2)

    assert len(seen) == 2, "the second round should have received feedback"
    returned = " ".join(seen[1])
    assert "Calculadora" not in returned
    assert "Calculador.calculation" not in returned
    assert returned.strip(), "feedback should survive scrubbing, not be emptied"


def test_the_best_scoring_neutral_version_wins(mapped):
    scores = iter([4, 13, 7])

    def judge(_plan):
        return {"strongest_objection": "x", "actions": ["y"], "_total_score": next(scores)}

    plans = iter(["a", "b", "c"])
    outcome = run_plan_loop(
        draft=lambda f: json.dumps(_plan(summary=next(plans))),
        judge=judge,
        alias_map=mapped,
        stage="documentation",
        max_rounds=3,
    )
    assert outcome.score == 13


# --- scoring -----------------------------------------------------------------


def test_the_score_sums_only_the_quality_pillars():
    judgement = {"scores": {p: 5 for p in SCORED_PILLARS} | {"neutrality": 5}}
    assert total_score(judgement) == 15, "neutrality must never contribute to the sum"


def test_missing_or_absurd_scores_are_clamped():
    assert total_score({"scores": {}}) == 0
    assert total_score({"scores": {p: 99 for p in SCORED_PILLARS}}) == 15
    assert total_score({"scores": {p: -4 for p in SCORED_PILLARS}}) == 0


# --- deliberate omission vs failure ------------------------------------------


def test_the_three_empty_section_states_render_differently():
    from packages.agents.dirt_team.spec_synthesizer_agent import (
        _initial_payload,
        _make_finalizer,
        _serialize_spec_payload,
    )

    payload = _initial_payload()
    payload["fragments"].append(
        {
            "task_id": "SPEC-1",
            "output_field": "scope",
            "heading": "Scope",
            "markdown": "The system validates records.",
        }
    )
    plan = [{"task_id": "SPEC-1", "output_field": "scope"}]
    excused = {
        "configuration": "This project reads no settings at start-up, so there is nothing to specify."
    }
    markdown = _serialize_spec_payload(_make_finalizer(plan, excused)(payload, set()))

    # excused with a reason -> the reason travels with it
    assert "Configuration — not applicable" in markdown
    assert "reads no settings at start-up" in markdown
    # absent with no reason -> a defect, not a decision
    assert "MISSING: no task planned and no justification given" in markdown
    # a real generation failure stays distinct from both
    assert "Configuration — TODO: generation failed" not in markdown
    assert "The system validates records." in markdown


# --- the controller actually executes an EV-id plan --------------------------


def test_a_mini_task_plan_runs_end_to_end_and_pre_reads_the_right_source(snapshot, manifest):
    """Exercises execute_mini_tasks for real.

    This is the gap that let a stale keyword argument reach a live run: every other test
    checked a piece in isolation, and nothing drove the controller from plan to output.
    """
    from packages.agents.base_agent import StubTextClient
    from packages.agents.dirt_team import DocumentationAgent
    from packages.modules.skills.reading import Reader

    reader = Reader(snapshot)
    alias_map = AliasMap()
    evidence = {"file": "README.md", "line_start": 1, "line_end": 3}
    ev_id = alias_map.evidence_id(evidence["file"], evidence["line_start"], evidence["line_end"])

    seen_prompts: list[str] = []

    def reply(prompt: str) -> str:
        seen_prompts.append(prompt)
        return json.dumps(
            {"label": "documented", "value": "It stores widgets and reads them back."}
        )

    agent = DocumentationAgent(
        model="stub/model",
        chat_client=StubTextClient(reply),
        source_reader=reader,
        alias_map=alias_map,
    )
    plan = {
        "stage": "documentation",
        "summary": "one task",
        "mini_tasks": [
            {
                "task_id": "DOC-001",
                "task_type": "extract_project_purpose",
                "output_field": "project_purpose",
                "input_refs": [{"source": "evidence_catalogue", "evidence_id": ev_id}],
                "requirements": ["state the documented purpose"],
                "min_items": 1,
            }
        ],
    }

    report = json.loads(agent.analyze(source_manifest=manifest, mini_tasks=plan))

    assert report["project_purpose"], "the task produced no finding"
    assert seen_prompts, "no model call was made"
    # the opaque id was resolved and the real documentation text pre-read into the prompt
    assert "Sample Project" in seen_prompts[0]


def test_an_unresolvable_evidence_id_is_skipped_not_guessed(snapshot, manifest):
    from packages.agents.base_agent import StubTextClient
    from packages.agents.dirt_team import DocumentationAgent
    from packages.modules.skills.reading import Reader

    agent = DocumentationAgent(
        model="stub/model",
        chat_client=StubTextClient(lambda p: json.dumps({"label": "missing", "value": "none"})),
        source_reader=Reader(snapshot),
        alias_map=AliasMap(),
    )
    assert agent._resolve_ref({"source": "evidence_catalogue", "evidence_id": "EV-404"}) is None
    assert agent._resolve_ref({"source": "evidence_catalogue"}) is None


# --- assembled spec headings -------------------------------------------------


def test_no_section_heading_is_emitted_twice():
    """Regression: the assembler writes the section heading, and a fragment whose own
    markdown opened with the same heading produced it a second time."""
    import re

    from packages.agents.dirt_team.spec_synthesizer_agent import (
        _initial_payload,
        _serialize_spec_payload,
    )

    payload = _initial_payload()
    payload["fragments"] += [
        {
            "task_id": "S-1",
            "output_field": "gaps_and_open_questions",
            "heading": "Gaps And Open Questions",
            "markdown": "## Gaps And Open Questions\n\n- One unresolved question.",
        },
        {
            "task_id": "S-2",
            "output_field": "scope",
            "heading": "Scope",
            "markdown": "### scope\n\nThe scope covers evaluation only.",
        },
        {
            "task_id": "S-3",
            "output_field": "error_handling",
            "heading": "Invalid Input Handling",
            "markdown": "Bad input is rejected without crashing.",
        },
    ]
    markdown = _serialize_spec_payload(payload)

    headings = re.findall(r"^#{1,6}\s+(.*)$", markdown, re.MULTILINE)
    normalised = ["".join(c for c in h.lower() if c.isalnum()) for h in headings]
    duplicates = {h for h in normalised if normalised.count(h) > 1}
    assert not duplicates, f"heading emitted more than once: {duplicates}"

    # the content survived the de-duplication
    assert "One unresolved question." in markdown
    assert "The scope covers evaluation only." in markdown
    # a genuine sub-heading is still kept
    assert "### Invalid Input Handling" in markdown


def test_a_fragment_subheading_that_differs_is_preserved():
    from packages.agents.dirt_team.spec_synthesizer_agent import (
        _initial_payload,
        _serialize_spec_payload,
    )

    payload = _initial_payload()
    payload["fragments"].append(
        {
            "task_id": "S-1",
            "output_field": "error_handling",
            "heading": "Division By Zero",
            "markdown": "## Division By Zero\n\nReturns an error indicator.",
        }
    )
    markdown = _serialize_spec_payload(payload)
    assert "## Error Handling" in markdown
    assert "Division By Zero" in markdown
    assert "Returns an error indicator." in markdown

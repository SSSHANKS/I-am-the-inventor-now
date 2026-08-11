"""The thick base / thin children contract (CLAUDE.md section 5).

These tests exist because adding one constructor argument used to mean editing five
near-identical child constructors, and missing one was a silent runtime failure.
"""

import inspect

import pytest

from packages.agents.base_agent import BaseAgent, StubTextClient
from packages.agents.dirt_team import (
    BehaviorAnalyzerAgent,
    CodeFactsAgent,
    DocumentationAgent,
    SpecSynthesizerAgent,
)
from packages.agents.planning import PlanningAgent

ALL_AGENTS = [
    PlanningAgent,
    DocumentationAgent,
    CodeFactsAgent,
    BehaviorAnalyzerAgent,
    SpecSynthesizerAgent,
]
READER_AGENTS = [DocumentationAgent, CodeFactsAgent, BehaviorAnalyzerAgent]


@pytest.mark.parametrize("agent_class", ALL_AGENTS)
def test_no_agent_defines_its_own_constructor(agent_class):
    """The whole point of the lift: construction is declared once, in the base."""
    assert "__init__" not in vars(agent_class), (
        f"{agent_class.__name__} redeclares __init__; a new shared field would then have "
        "to be added here too, which is exactly the duplication that was removed."
    )
    assert agent_class.__init__ is BaseAgent.__init__


@pytest.mark.parametrize("agent_class", ALL_AGENTS)
def test_a_field_added_to_the_base_reaches_every_agent(agent_class, reader):
    """Adding a parameter to BaseAgent.__init__ reaches all agents by construction.

    They share one function object, so the base signature *is* every agent's signature -
    there is no second place a field could fail to arrive.
    """
    base_params = set(inspect.signature(BaseAgent.__init__).parameters)
    agent_params = set(inspect.signature(agent_class.__init__).parameters)
    assert base_params == agent_params

    for field in ("model", "profile_path", "retry_profile_path", "max_validation_retries"):
        assert field in agent_params, f"{field} must be declared once, in the base"


@pytest.mark.parametrize("agent_class", ALL_AGENTS)
def test_every_agent_builds_from_the_same_shared_arguments(agent_class, reader):
    agent = agent_class(
        model="stub/model",
        max_validation_retries=4,
        chat_client=StubTextClient([]),
        source_reader=reader,
    )
    assert agent.config.model == "stub/model"
    assert agent.max_validation_retries == 4
    assert agent.agent_name and agent.instruction


@pytest.mark.parametrize("agent_class", READER_AGENTS)
def test_source_reading_agents_get_a_verifier_from_the_base(agent_class, reader):
    agent = agent_class(model="stub/model", chat_client=StubTextClient([]), source_reader=reader)
    assert agent.source_reader is reader
    assert agent.artifact_verifier is not None


@pytest.mark.parametrize("agent_class", [PlanningAgent, SpecSynthesizerAgent])
def test_artifact_only_agents_get_no_reader_and_no_verifier(agent_class):
    """They reshape artifacts and never touch source, so both stay None rather than
    forcing every caller to supply a reader they will not use."""
    agent = agent_class(model="stub/model", chat_client=StubTextClient([]))
    assert agent.source_reader is None
    assert agent.artifact_verifier is None


def test_the_tool_surface_is_gone():
    """AgentProvider is prompt to text; there is nothing to call tools with."""
    assert not hasattr(BaseAgent, "source_reader_tools")
    run_params = set(inspect.signature(BaseAgent.run).parameters)
    assert not run_params & {"tool_provider", "enable_tools", "require_tool_use", "allowed_files"}

    with pytest.raises(ImportError):
        import packages.agents.base_agent.tools  # noqa: F401


@pytest.mark.parametrize("agent_class", READER_AGENTS)
def test_agents_refuse_to_run_without_a_plan(agent_class, reader, manifest):
    """The single-shot fallback needed source-reading tools, which no longer exist.

    Behaviour change on a path the pipeline never took: it always supplies a plan.
    """
    agent = agent_class(model="stub/model", chat_client=StubTextClient([]), source_reader=reader)
    extra = {"code_facts_report": {}} if agent_class is BehaviorAnalyzerAgent else {}
    with pytest.raises(ValueError, match="requires a plan"):
        agent.analyze(source_manifest=manifest, mini_tasks=None, **extra)


# --- call_options typo safety ------------------------------------------------


def test_a_misspelled_call_option_is_reported_with_a_suggestion(tmp_path):
    """litellm drops an unknown option silently, so the agent would run at settings
    nobody chose with no signal anywhere."""
    import json as _json

    from config.settings import check_call_options

    profile = tmp_path / "typo.json"
    profile.write_text(
        _json.dumps({"default_model": "x", "call_options": {"tempreture": 0.0}}),
        encoding="utf-8",
    )
    warnings = check_call_options(profile)
    assert len(warnings) == 1
    assert "tempreture" in warnings[0] and "temperature" in warnings[0]


def test_an_unfamiliar_but_possibly_valid_option_warns_without_a_guess(tmp_path):
    import json as _json

    from config.settings import check_call_options

    profile = tmp_path / "odd.json"
    profile.write_text(
        _json.dumps({"default_model": "x", "call_options": {"vertex_project": "p"}}),
        encoding="utf-8",
    )
    warnings = check_call_options(profile)
    assert len(warnings) == 1
    assert "did you mean" not in warnings[0], "no confident suggestion should be invented"


def test_recognised_options_are_silent(tmp_path):
    import json as _json

    from config.settings import check_call_options

    profile = tmp_path / "good.json"
    profile.write_text(
        _json.dumps(
            {
                "default_model": "x",
                "call_options": {"temperature": 0.0, "timeout": 120, "num_retries": 2},
            }
        ),
        encoding="utf-8",
    )
    assert check_call_options(profile) == []


def test_the_shipped_profiles_have_no_option_typos():
    from config import load_settings
    from config.settings import check_call_options

    settings = load_settings()
    for agent in settings.agents.values():
        assert check_call_options(agent.profile_path(settings.model_profiles_dir)) == []
        retry = agent.retry_profile_path(settings.model_profiles_dir)
        if retry:
            assert check_call_options(retry) == []


# --- layer 1: the doc analyzer must describe, not quote -----------------------
# The live leak was not a model mistake - the agent was TOLD to do it: "If documentation
# contains commands, include the exact commands." These pin the corrected contract, in
# both prompts, because the narrow prompt is the one that actually produced the leak.


def _doc_prompts():
    from packages.agents.dirt_team import documentation_agent as doc

    section = {"file": "d.md", "line_start": 1, "line_end": 1, "text": "3. Crie sua branch"}
    narrow = doc._build_narrow_user_prompt({}, "setup_and_run", "T1", [section])
    return doc.instruction, doc.mini_task_instruction, narrow


def test_the_doc_analyzer_is_no_longer_told_to_copy_commands():
    """The exact directive that caused the leak, in the aggregate instruction."""
    instruction, _, _ = _doc_prompts()
    assert "include the exact commands" not in instruction
    assert "Prefer exact commands" not in instruction
    assert "never reproduce the command itself" in instruction


def test_every_doc_prompt_forbids_reproducing_command_form():
    for prompt in _doc_prompts():
        lowered = prompt.lower()
        assert "command" in lowered
        assert "never reproduce" in lowered or "do not reproduce" in lowered


def test_the_proper_noun_escape_hatch_is_closed():
    """'Proper nouns may stay as-is' is how a Portuguese branch name survived a
    sentence that was otherwise correctly translated."""
    _, mini_task_instruction, _ = _doc_prompts()
    assert "may stay as-is" not in mini_task_instruction
    assert "branch name" in mini_task_instruction.lower()


def test_the_narrow_prompt_carries_the_clean_room_rule():
    _, _, narrow = _doc_prompts()
    lowered = narrow.lower()
    assert "describes behaviour, never form" in lowered
    for banned in ("branch name", "file path", "url"):
        assert banned in lowered


def test_evidence_excerpts_are_still_required_to_be_verbatim():
    """Layer 1 neutralises `value` only. The excerpt stays exact - it is dirty-side and
    `neutral_report` drops it before anything crosses."""
    instruction, _, _ = _doc_prompts()
    assert "verbatim" in instruction.lower()
    assert "Do not paraphrase, translate, summarize" in instruction


def test_the_closing_summary_counts_every_rendered_finding():
    """It used to re-scan with the identifier rules only, reporting 2 where the document
    showed 3 - it missed content findings and would double-count identifiers quoted in
    the notes. Reading the rendered block cannot drift from what was written."""
    from main import _rendered_findings

    spec = "\n".join(
        [
            "# Specification",
            "Body text mentioning calculation.",
            "",
            "## BORDER-REVIEW",
            "",
            "- BORDER-REVIEW: original identifier present -> 'calculation' - 2 occurrence(s)",
            "    - [DESCRIPTIVE] ...an invalid calculation or...",
            "- BORDER-REVIEW: original identifier present -> 'copy' - 4 occurrence(s)",
            "- BORDER-REVIEW: verbatim source-document prose present -> 'x' - 1 occurrence(s)",
        ]
    )
    assert _rendered_findings(spec) == 3, "indented examples must not be counted as findings"
    assert _rendered_findings("# Specification\n\nNo findings.\n") == 0

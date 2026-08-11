"""Supervising: marshmallow 4 behaviour, and the repair loop driven by a stub model.

The legacy schemas were written against marshmallow 3. Q14 asked for validation to be
exercised at runtime rather than assumed, which is what most of this file does.
"""

import json

import pytest

from packages.agents.base_agent.client import StubTextClient
from packages.modules.supervising import ArtifactPolicy, Supervisor
from packages.modules.supervising.schemas import (
    DocAnalyzerSchema,
    PlanningSchema,
    get_validation_errors,
)
from packages.modules.supervising.schemas.validator import SchemaValidationError


def _doc_report(**overrides):
    payload = {
        "summary": {
            "label": "documented",
            "value": "The project stores widgets and reads them back.",
            "evidence": {"file": "README.md", "line_start": 1, "line_end": 2, "excerpt": "x"},
        },
        "documentation_files_read": ["README.md"],
        "project_purpose": [],
        "setup_and_run": [],
        "api_surface": [],
        "features": [],
        "warnings": [],
        "open_questions": [],
    }
    return payload | overrides


# --- marshmallow 4 runtime behaviour ---------------------------------------


def test_a_valid_artifact_passes():
    assert get_validation_errors(json.dumps(_doc_report()), DocAnalyzerSchema()) is None


def test_enum_values_are_enforced():
    bad = _doc_report(summary={**_doc_report()["summary"], "label": "guessed"})
    errors = get_validation_errors(json.dumps(bad), DocAnalyzerSchema())
    assert "summary" in errors


def test_required_fields_are_enforced():
    payload = _doc_report()
    del payload["features"]
    assert "features" in get_validation_errors(json.dumps(payload), DocAnalyzerSchema())


def test_unknown_top_level_fields_are_rejected():
    errors = get_validation_errors(json.dumps(_doc_report(surprise=1)), DocAnalyzerSchema())
    assert "surprise" in errors


def test_non_json_is_reported_as_a_root_error():
    errors = get_validation_errors("not json at all", DocAnalyzerSchema())
    assert "_root_" in errors


def test_empty_output_is_reported():
    assert "_root_" in get_validation_errors("   ", DocAnalyzerSchema())


def test_a_json_array_is_not_an_artifact():
    assert "_root_" in get_validation_errors("[1, 2]", DocAnalyzerSchema())


def test_plan_schema_accepts_a_well_formed_plan():
    plan = {
        "stage": "documentation",
        "summary": "one task",
        "mini_tasks": [
            {
                "task_id": "DOC-001",
                "task_type": "extract_project_purpose",
                "output_field": "project_purpose",
                "input_refs": [],
                "requirements": ["state the documented purpose"],
                "min_items": 1,
            }
        ],
    }
    assert get_validation_errors(json.dumps(plan), PlanningSchema()) is None


# --- the repair loop --------------------------------------------------------


def test_supervisor_accepts_a_valid_first_answer():
    client = StubTextClient([])
    supervisor = Supervisor(repair_callback=client.chat, max_validation_retries=2)
    policy = ArtifactPolicy(name="Test", schema=DocAnalyzerSchema())
    content = json.dumps(_doc_report())
    assert supervisor.supervise(content=content, messages=[], policy=policy) == content
    assert client.call_count == 0, "a valid answer must not trigger a model call"


def test_supervisor_repairs_an_invalid_answer():
    good = json.dumps(_doc_report())
    client = StubTextClient([good])
    supervisor = Supervisor(
        repair_callback=lambda messages: client.chat(messages).content, max_validation_retries=3
    )
    policy = ArtifactPolicy(name="Test", schema=DocAnalyzerSchema())

    result = supervisor.supervise(
        content='{"summary": "wrong shape"}',
        messages=[{"role": "system", "content": "instruction"}],
        policy=policy,
    )
    assert json.loads(result)["summary"]["label"] == "documented"
    assert client.call_count == 1


def test_supervisor_gives_up_after_its_retry_budget():
    client = StubTextClient(lambda prompt: '{"still": "wrong"}')
    supervisor = Supervisor(
        repair_callback=lambda messages: client.chat(messages).content, max_validation_retries=2
    )
    policy = ArtifactPolicy(name="Test", schema=DocAnalyzerSchema())

    with pytest.raises(SchemaValidationError):
        supervisor.supervise(
            content='{"still": "wrong"}',
            messages=[{"role": "system", "content": "instruction"}],
            policy=policy,
        )
    assert client.call_count == 2


def test_the_repair_prompt_shows_the_model_its_own_rejected_answer():
    good = json.dumps(_doc_report())
    client = StubTextClient([good])
    supervisor = Supervisor(
        repair_callback=lambda messages: client.chat(messages).content, max_validation_retries=2
    )
    policy = ArtifactPolicy(name="Test", schema=DocAnalyzerSchema())
    supervisor.supervise(
        content='{"summary": "wrong shape"}',
        messages=[{"role": "system", "content": "instruction"}],
        policy=policy,
    )
    prompt = client.prompts[0]
    assert "wrong shape" in prompt
    assert "YOUR PREVIOUS ANSWER" in prompt


def test_retries_are_told_which_attempt_they_are():
    """Regression: at temperature 0 every repair turn reproduced the same invalid
    output 7 times. The client needs the attempt number to warm the temperature."""
    good = json.dumps(_doc_report())
    replies = iter(['{"still": "wrong"}', good])
    client = StubTextClient(lambda prompt: next(replies))
    supervisor = Supervisor(
        repair_callback=lambda messages, attempt=0: client.chat(messages, attempt=attempt).content,
        max_validation_retries=3,
    )
    policy = ArtifactPolicy(name="Test", schema=DocAnalyzerSchema())
    supervisor.supervise(
        content='{"first": "wrong"}',
        messages=[{"role": "system", "content": "instruction"}],
        policy=policy,
    )
    assert client.attempts == [1, 2], "each repair turn must report its attempt number"


def test_a_callback_without_the_attempt_argument_still_works():
    good = json.dumps(_doc_report())
    client = StubTextClient([good])
    supervisor = Supervisor(
        repair_callback=lambda messages: client.chat(messages).content,
        max_validation_retries=2,
    )
    policy = ArtifactPolicy(name="Test", schema=DocAnalyzerSchema())
    result = supervisor.supervise(
        content='{"wrong": 1}',
        messages=[{"role": "system", "content": "i"}],
        policy=policy,
    )
    assert json.loads(result)["summary"]["label"] == "documented"


def test_the_system_instruction_is_separated_from_the_prompt():
    """AgentProvider takes system= directly now, so the standing instruction no longer
    has to be flattened into the user prompt."""
    client = StubTextClient(["ok"])
    client.chat(
        [
            {"role": "system", "content": "You are the Documentation Agent."},
            {"role": "user", "content": "Summarise this section."},
        ]
    )
    assert client.systems[0] == "You are the Documentation Agent."
    assert "Summarise this section." in client.prompts[0]
    assert "You are the Documentation Agent." not in client.prompts[0]


def test_fatal_model_errors_are_marked_so_the_retry_budget_is_not_spent():
    from packages.agents.base_agent.client import ModelCallError

    assert ModelCallError("bad key", kind="auth", status_code=401).fatal is True
    assert ModelCallError("no model", kind="not_found", status_code=404).fatal is True
    assert ModelCallError("429", kind="rate_limit", status_code=429).fatal is False
    assert ModelCallError("boom", kind="server", status_code=500).fatal is False
    assert ModelCallError("unknown").kind == "other"


def test_a_rate_limit_is_not_retried_a_second_time_by_iatin():
    """There is exactly one retry layer, and it is AgentProvider's.

    IATIN used to run its own (5, 15) backoff ladder on top. That ladder retried
    underneath AgentProvider's own accounting, putting requests on the wire that its
    limiter never saw - the fault that made rate limiting fail in the first place. A 429
    reaching us has already been waited out for as long as the server asked, so the only
    correct move is to surface it.
    """
    from unittest.mock import patch

    from agent_provider import AgentProviderError

    from packages.agents.base_agent.client import AgentProviderClient, ModelCallError

    calls = []

    class ThrottledProvider:
        def generate(self, prompt, system=None):
            calls.append(prompt)
            raise AgentProviderError("429 exhausted", status_code=429, kind="rate_limit")

    client = AgentProviderClient.__new__(AgentProviderClient)
    client.model = "stub/model"
    client.retry_profile_path = None

    with (
        patch.object(
            AgentProviderClient, "_provider_for", lambda self, attempt: ThrottledProvider()
        ),
        pytest.raises(ModelCallError) as raised,
    ):
        client.generate("prompt")

    assert len(calls) == 1, "IATIN must not retry; AgentProvider already did"
    assert raised.value.kind == "rate_limit"
    assert raised.value.fatal is False


def test_no_second_backoff_ladder_survives_in_the_client():
    """Guards the deletion itself: a reintroduced ladder would sleep here."""
    from packages.agents.base_agent import client as client_module

    assert not hasattr(client_module, "RATE_LIMIT_BACKOFF_SECONDS")
    assert not hasattr(client_module, "time"), "no sleeping in the client any more"

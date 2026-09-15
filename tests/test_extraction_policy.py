import json

import pytest

from packages.agents.base_agent import BaseAgent, StubTextClient
from packages.modules.supervising import ExtractionPolicy, SupervisorVerificationError
from packages.modules.supervising.schemas import CodeFactsNarrowImportsSchema


def _policy() -> ExtractionPolicy:
    return ExtractionPolicy(
        "Code Facts Agent [CF-001 -> imports]",
        CodeFactsNarrowImportsSchema(),
    )


def _item(source_ref: int) -> dict:
    return {"source_ref": source_ref, "label": "observed", "import": "dependency"}


def test_extraction_requires_planned_item_count_and_priority_sources():
    result = _policy().verify(
        json.dumps({"items": [_item(1)]}),
        {
            "minimum_items": 2,
            "valid_source_refs": [1, 2],
            "required_source_refs": [1, 2],
        },
    )

    assert result["valid"] is False
    assert any("at least 2" in issue["message"] for issue in result["issues"])
    assert any("[2]" in issue["message"] for issue in result["issues"])


def test_extraction_rejects_invented_source_refs():
    result = _policy().verify(
        json.dumps({"items": [_item(9)]}),
        {"minimum_items": 1, "valid_source_refs": [1], "required_source_refs": []},
    )

    assert result["valid"] is False
    assert any("unavailable source_ref" in issue["message"] for issue in result["issues"])


def test_artifact_only_extraction_must_preserve_required_evidence_ids():
    result = _policy().verify(
        json.dumps({"items": [_item(1)]}),
        {
            "minimum_items": 1,
            "valid_source_refs": [1],
            "required_source_refs": [],
            "required_evidence_ids": ["EV-042"],
        },
    )

    assert result["valid"] is False
    assert any("EV-042" in issue["message"] for issue in result["issues"])


def test_supervisor_repairs_an_incomplete_extraction_once():
    replies = [
        json.dumps({"items": [_item(1)]}),
        json.dumps({"items": [_item(1), _item(2)]}),
    ]
    agent = BaseAgent(
        model="stub/model",
        chat_client=StubTextClient(replies),
        max_validation_retries=1,
    )

    output = agent.run(
        task_instruction="extract imports",
        supervisor_policy=_policy(),
        supervisor_context={
            "minimum_items": 2,
            "valid_source_refs": [1, 2],
            "required_source_refs": [1, 2],
        },
    )

    assert len(json.loads(output)["items"]) == 2


def test_supervisor_stops_after_the_configured_extraction_budget():
    agent = BaseAgent(
        model="stub/model",
        chat_client=StubTextClient([json.dumps({"items": []})] * 2),
        max_validation_retries=1,
    )

    with pytest.raises(SupervisorVerificationError, match="failed verification"):
        agent.run(
            task_instruction="extract imports",
            supervisor_policy=_policy(),
            supervisor_context={
                "minimum_items": 1,
                "valid_source_refs": [1],
                "required_source_refs": [1],
            },
        )


def test_narrow_extraction_can_cap_repairs_below_the_agent_default():
    client = StubTextClient([json.dumps({"items": []})] * 3)
    agent = BaseAgent(
        model="stub/model",
        chat_client=client,
        max_validation_retries=6,
    )

    with pytest.raises(SupervisorVerificationError):
        agent.run(
            task_instruction="extract imports",
            supervisor_policy=_policy(),
            supervisor_context={
                "minimum_items": 1,
                "valid_source_refs": [1],
                "required_source_refs": [1],
            },
            validation_retry_limit=2,
        )

    assert client.call_count == 3, "one initial call plus two bounded repair calls"


def test_extraction_context_tracks_unresolved_required_evidence():
    context = BaseAgent._extraction_context(
        {
            "min_items": 2,
            "input_refs": [
                {"source": "reconstruction_priority", "evidence_id": "EV-001"},
                {"source": "reconstruction_priority", "evidence_id": "EV-002"},
            ],
        },
        [{"source_ref": 1, "evidence_id": "EV-001"}],
    )

    assert context["required_source_refs"] == [1]
    assert context["unresolved_required_evidence_ids"] == ["EV-002"]

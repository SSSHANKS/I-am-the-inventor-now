"""One end-to-end run of the real controller, on a stub model.

Every other test mocks a piece and checks it in isolation. Three separate bugs reached a
live run anyway, because nothing drove the pipeline from a plan through to a specification:

- `allowed_files=[]` was still passed to `run()` after the tool loop removed it;
- the spec stage discarded good fragments when one mini task failed;
- a stale import broke the package outright.

Each was a wiring fault between components that were individually fine. This test exists to
catch that class: it uses the real agents, the real controller, the real boundary layer and
the real supervisor, and swaps out only the model.
"""

import json

import pytest

from packages.agents.base_agent import StubTextClient
from packages.agents.border_team import BorderGateAgent
from packages.agents.dirt_team import (
    BehaviorAnalyzerAgent,
    CodeFactsAgent,
    DocumentationAgent,
    PlanJudgeAgent,
    SpecSynthesizerAgent,
)
from packages.agents.planning import PlanningAgent
from packages.modules.boundary import (
    AliasMap,
    build_evidence_catalogue,
    evidence_catalogue,
    find_residual_originals,
    mint_evidence_ids,
    neutral_manifest,
    register_code_identifiers,
    scan_content_leaks,
)
from packages.modules.indexing import SourceCodeIndexer, SourceDocIndexer
from packages.modules.skills.reading import Reader
from packages.modules.storing import Storage
from packages.modules.supervising.schemas import ManifestSchema, NeutralManifestSchema
from packages.modules.supervising.verifiers.planning import OUTPUT_FIELDS_BY_STAGE


class ScriptedModel:
    """Answers like a competent model would, by recognising which prompt it was given.

    Not a recording: it builds each reply from the prompt, so it keeps working when a
    prompt changes and fails loudly when a *contract* changes - which is the point.
    """

    def __init__(self, catalogue_ids: list[str]):
        self.ids = catalogue_ids or ["EV-001"]
        self.calls: list[str] = []

    def generate(self, prompt: str, system: str | None = None, attempt: int = 0) -> str:
        self.calls.append(prompt)

        if "<plan_under_review>" in prompt:
            return json.dumps(
                {
                    "strongest_objection": "the conventional areas got as much depth as the crux",
                    "scores": {
                        "crux_coverage": 4,
                        "proportional_decomposition": 4,
                        "completeness": 5,
                    },
                    "actions": ["spend a second task on the validation rule"],
                }
            )

        if "<border_findings>" in prompt:
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
                            "rationale": "integration stub dismisses soft findings",
                        }
                        for item in findings
                        if isinstance(item, dict)
                    ]
                }
            )

        if "<evidence_catalogue>" in prompt:
            return json.dumps(self._plan(prompt))

        if '"items"' in (system or "") or "target_section" in prompt:
            return json.dumps(
                {"items": [{"source_ref": None, "heading": "Section", "markdown": "A statement."}]}
            )

        # The documentation stage asks for one {label, value} entry and names its kind;
        # the code-facts and behaviour stages ask for an items list. Routing on the kind
        # the prompt declares keeps the stub honest about each schema instead of guessing.
        if "doc_open_question" in prompt:
            return json.dumps({"label": "missing", "value": "An unresolved question."})
        if "doc_labeled" in prompt:
            return json.dumps({"label": "documented", "value": "A concrete finding."})

        if "[output_field]" in prompt:
            field = self._output_field(prompt)
            if field.endswith("open_questions"):
                return json.dumps(
                    {
                        "items": [
                            {
                                "source_ref": None,
                                "label": "missing",
                                "value": "An unresolved question.",
                            }
                        ]
                    }
                )
            return json.dumps({"items": []})

        return json.dumps({"items": []})

    @staticmethod
    def _output_field(prompt: str) -> str:
        """The field name only.

        The prompt renders it with a trailing parenthetical ("... (only metadata ...)"),
        so the first whitespace-delimited token is the name.
        """
        marker = "[output_field]"
        if marker not in prompt:
            return ""
        line = prompt.split(marker, 1)[1].strip().splitlines()[0].strip()
        return line.split()[0] if line.split() else ""

    def _plan(self, prompt: str) -> dict:
        stage = prompt.split("<stage>", 1)[1].split("</stage>", 1)[0].strip()
        fields = sorted(OUTPUT_FIELDS_BY_STAGE[stage])
        return {
            "stage": stage,
            "summary": "covered every field; extra depth on the non-obvious rule",
            "mini_tasks": [
                {
                    "task_id": f"{stage[:4].upper()}-{index:03d}",
                    "task_type": f"extract_{field}",
                    "output_field": field,
                    "input_refs": [
                        {
                            "source": "evidence_catalogue",
                            "evidence_id": self.ids[index % len(self.ids)],
                        }
                    ],
                    "requirements": ["state the exact conditions and what happens otherwise"],
                    "min_items": 1,
                }
                for index, field in enumerate(fields, start=1)
            ],
        }


@pytest.fixture
def pipeline(snapshot, manifest, tmp_path):
    """Everything the real pipeline builds, wired the way the notebook wires it."""
    reader = Reader(snapshot)
    code_index = SourceCodeIndexer(reader).index(manifest)
    doc_index = SourceDocIndexer(reader).index(manifest)

    alias_map = AliasMap(project_name="PROJECT-X")
    register_code_identifiers(code_index, alias_map)
    mint_evidence_ids(alias_map, code_index, doc_index)
    catalogue = evidence_catalogue(alias_map, code_index, doc_index)

    model = ScriptedModel([entry["evidence_id"] for entry in catalogue])

    def agent(cls, **extra):
        return cls(
            model="stub/model",
            chat_client=StubTextClient(model.generate),
            alias_map=alias_map,
            **extra,
        )

    return {
        "manifest": manifest,
        "reader": reader,
        "code_index": code_index,
        "doc_index": doc_index,
        "alias_map": alias_map,
        "catalogue": catalogue,
        "model": model,
        "agent": agent,
        "storage": Storage(artifacts_dir=tmp_path / "artifacts", run_name="integration"),
    }


def test_the_pipeline_runs_end_to_end_and_produces_a_specification(pipeline):
    """Plan -> execute -> synthesise, through the real controller."""
    p = pipeline
    storage, alias_map = p["storage"], p["alias_map"]

    storage.save_artifact("manifest.json", p["manifest"], ManifestSchema())
    storage.save_artifact(
        "neutral_manifest.json", neutral_manifest(p["manifest"], alias_map), NeutralManifestSchema()
    )

    planner = p["agent"](PlanningAgent)
    judge = p["agent"](PlanJudgeAgent)

    def plan_for(stage):
        return planner.plan(
            stage=stage,
            source_manifest=p["manifest"],
            evidence_catalogue=p["catalogue"],
            judge=judge,
            code_index=p["code_index"],
            doc_index=p["doc_index"],
        )

    doc_plan = plan_for("documentation")
    documentation_report = p["agent"](DocumentationAgent, source_reader=p["reader"]).analyze(
        source_manifest=p["manifest"], source_doc_index=p["doc_index"], mini_tasks=doc_plan
    )

    code_plan = plan_for("code_facts")
    code_facts_report = p["agent"](CodeFactsAgent, source_reader=p["reader"]).analyze(
        source_manifest=p["manifest"], source_code_index=p["code_index"], mini_tasks=code_plan
    )

    behavior_plan = plan_for("behavior")
    behavior_report = p["agent"](BehaviorAnalyzerAgent, source_reader=p["reader"]).analyze(
        source_manifest=p["manifest"],
        code_facts_report=code_facts_report,
        documentation_report=documentation_report,
        source_code_index=p["code_index"],
        mini_tasks=behavior_plan,
    )

    spec_plan = plan_for("specification")
    specification = p["agent"](SpecSynthesizerAgent).synthesize(
        source_manifest=p["manifest"],
        alias_map=alias_map,
        documentation_report=documentation_report,
        code_facts_report=code_facts_report,
        behavior_report=behavior_report,
        mini_tasks=spec_plan,
    )
    storage.save_text("specification.md", specification)

    catalogue_artifact = build_evidence_catalogue(
        alias_map, p["code_index"], p["doc_index"]
    )
    p["agent"](BorderGateAgent).enforce(
        storage=storage,
        alias_map=alias_map,
        specification=specification,
        documentation_report=documentation_report,
        code_facts_report=code_facts_report,
        behavior_report=behavior_report,
        evidence_catalogue=catalogue_artifact,
        neutral_manifest=neutral_manifest(p["manifest"], alias_map),
    )

    # the pipeline completed and produced a real document
    assert storage.exists("specification.md")
    assert storage.exists("border_verdict.json")
    assert storage.read_json("border_verdict.json")["status"] == "pass"
    assert specification.startswith("# Specification")
    assert len(specification.splitlines()) > 20

    # every stage actually reached the model rather than short-circuiting
    assert len(p["model"].calls) >= 8

    # presence: every specification section is accounted for
    for title in ("Scope", "Error Handling", "Behavioral Requirements"):
        assert f"## {title}" in specification

    # and nothing from the original crossed - both layers, because the documentation
    # command leak was invisible to the first one and obvious to the second
    assert find_residual_originals(specification, alias_map) == []
    assert scan_content_leaks(specification, _all_excerpts(p)) == []


def _all_excerpts(p):
    """Every verbatim source line the fixture's indexes hold, as the lifted-prose corpus."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "excerpt" and isinstance(value, str):
                    found.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(p["code_index"])
    walk(p["doc_index"])
    return tuple(found)


def test_the_controller_pre_reads_source_through_evidence_ids(pipeline):
    """The wiring the `allowed_files=[]` bug broke.

    That bug lived between execute_mini_tasks and run(); both were individually correct
    and every unit test passed. Driving the controller for real is what surfaces it.
    """
    p = pipeline
    planner = p["agent"](PlanningAgent)
    plan = planner.plan(
        stage="documentation",
        source_manifest=p["manifest"],
        evidence_catalogue=p["catalogue"],
        judge=p["agent"](PlanJudgeAgent),
        code_index=p["code_index"],
        doc_index=p["doc_index"],
    )

    report = json.loads(
        p["agent"](DocumentationAgent, source_reader=p["reader"]).analyze(
            source_manifest=p["manifest"], source_doc_index=p["doc_index"], mini_tasks=plan
        )
    )
    assert any(report.get(field) for field in ("project_purpose", "features", "setup_and_run"))

    # real snapshot text reached a prompt, so an opaque id was resolved to a location
    assert any("Sample Project" in call for call in p["model"].calls)


def test_the_plan_itself_never_names_the_original(pipeline):
    """The plan crosses the boundary too, so it gets the same scrutiny as the spec."""
    p = pipeline
    planner = p["agent"](PlanningAgent)
    for stage in ("documentation", "code_facts", "behavior", "specification"):
        plan = planner.plan(
            stage=stage,
            source_manifest=p["manifest"],
            evidence_catalogue=p["catalogue"],
            judge=p["agent"](PlanJudgeAgent),
            code_index=p["code_index"],
            doc_index=p["doc_index"],
        )
        assert find_residual_originals(plan, p["alias_map"]) == [], f"{stage} plan leaked"


def test_the_catalogue_the_planner_reads_is_itself_clean(pipeline):
    """The catalogue is an input, and it was the third place the boundary leaked: the
    planner imitated verbatim commands it had been shown in a label. Guarding the outputs
    is not enough if the prompt is contaminated."""
    from packages.modules.supervising.schemas import EvidenceCatalogueSchema

    p = pipeline
    artifact = build_evidence_catalogue(p["alias_map"], p["code_index"], p["doc_index"])

    assert EvidenceCatalogueSchema().validate(artifact) == {}
    for entry in artifact["entries"]:
        assert find_residual_originals(entry["about"], p["alias_map"]) == []
        assert scan_content_leaks(entry["about"]) == []

    # and the notes describing any substitution never carry what was substituted out
    notes = " ".join(artifact["border_review"])
    assert scan_content_leaks(notes) == []

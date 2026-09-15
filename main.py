"""Run the IATIN Dirty → Border pipeline against a git repository.

    python main.py https://github.com/owner/project

Clones the repository, indexes it, plans and runs four analysis stages, writes a
behavioural specification, then Border judges whether anything original leaked.
A failed Border verdict triggers Dirty repairs (rewrite the leaking passages) and
re-gates until it passes or ``--border-max-repairs`` is exhausted. A pass exports a
minimal verified handoff for the separate Clean process; a failure exports nothing.

The specification is a CROSSING artifact: it describes what the project does, never how
the original expressed it (CLAUDE.md section 2). Use `--stub` to exercise the whole
pipeline without spending model credits. Use `--skip-border` for Dirty-only runs.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import ConfigError, load_environment, load_settings, setup_logging
from packages.agents.base_agent import StubTextClient
from packages.agents.border_team import (
    DEFAULT_MAX_REPAIRS,
    BorderGateAgent,
    gate_with_repairs,
    load_plan_artifacts,
)
from packages.agents.dirt_team import (
    BehaviorAnalyzerAgent,
    CodeFactsAgent,
    DocumentationAgent,
    PlanJudgeAgent,
    SpecSynthesizerAgent,
)
from packages.agents.planning import PlanningAgent
from packages.modules.border import BorderGateError
from packages.modules.boundary import (
    BORDER_REVIEW,
    AliasMap,
    build_evidence_catalogue,
    mint_evidence_ids,
    neutral_manifest,
    register_code_identifiers,
)
from packages.modules.handoff import HandoffError, export_clean_handoff
from packages.modules.indexing import SourceCodeIndexer, SourceDocIndexer
from packages.modules.ingesting import provide_source_ingestor
from packages.modules.reconstruction_ir import build_dirty_inventory
from packages.modules.skills.reading import Reader
from packages.modules.storing import Storage
from packages.modules.supervising.schemas import (
    EvidenceCatalogueSchema,
    ManifestSchema,
    NeutralManifestSchema,
    ReconstructionInventorySchema,
)
from packages.modules.supervising.verifiers.planning import OUTPUT_FIELDS_BY_STAGE

log = logging.getLogger("iatin")

STAGES = ("documentation", "code_facts", "behavior", "specification")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description=(
            "Read a repository, produce a clean-room behavioural specification, "
            "and gate it through Border."
        ),
    )
    parser.add_argument("repository", help="git URL of the repository to analyse")
    parser.add_argument(
        "--artifacts-dir",
        type=Path,
        default=None,
        help="where to write the run directory (default: from config)",
    )
    parser.add_argument(
        "--stub",
        action="store_true",
        help="run without calling a model - exercises the pipeline, produces a hollow spec",
    )
    parser.add_argument(
        "--keep-clone",
        action="store_true",
        help="keep the cloned repository under temp/ instead of deleting it",
    )
    parser.add_argument(
        "--skip-border",
        action="store_true",
        help="run Dirty only - record BORDER-REVIEW notes but do not enforce a verdict",
    )
    parser.add_argument(
        "--border-max-repairs",
        type=int,
        default=DEFAULT_MAX_REPAIRS,
        help=(
            "how many times Dirty may rewrite the specification after Border refuses "
            f"(default: {DEFAULT_MAX_REPAIRS}; 0 means gate once with no repair)"
        ),
    )
    parser.add_argument(
        "--compatibility-mode",
        choices=("drop-in", "renamed"),
        default="renamed",
        help=(
            "public API policy recorded in the Clean handoff: drop-in preserves approved "
            "documented names; renamed creates a behavior-equivalent API (default: renamed)"
        ),
    )
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> Path:
    """The whole pipeline. Returns the path of the specification it wrote."""
    log_file = setup_logging()
    load_environment()
    settings = load_settings()
    log.info("logging to %s", log_file)

    # --- ingest ---------------------------------------------------------------
    ingestor = provide_source_ingestor(source=args.repository, config=settings)
    manifest = ingestor.ingest(source=args.repository)

    storage = Storage(
        artifacts_dir=args.artifacts_dir or settings.artifacts_dir,
        run_name=Path(manifest.repo_local_path).name,
    )
    storage.save_artifact("manifest.json", manifest, ManifestSchema())
    log.info(
        "ingested %s at %s - %d doc / %d code file(s)",
        args.repository,
        manifest.commit_hash,
        len(manifest.documentation),
        len(manifest.code),
    )

    # --- index ----------------------------------------------------------------
    reader = Reader(manifest.repo_local_path)
    code_index = SourceCodeIndexer(reader).index(manifest)
    doc_index = SourceDocIndexer(reader).index(manifest)
    storage.save_json("code_index.json", code_index)
    storage.save_json("doc_index.json", doc_index)

    # --- open the boundary ----------------------------------------------------
    # Identifiers must be registered and evidence ids minted BEFORE any planning:
    # prose can only be scrubbed of names the map knows, and a plan can only stay
    # neutral if there are opaque ids for it to cite instead of paths.
    alias_map = AliasMap(project_name="PROJECT-X")
    registered = register_code_identifiers(code_index, alias_map)
    minted = mint_evidence_ids(alias_map, code_index, doc_index)

    planning_inventory = build_dirty_inventory(code_index, alias_map)
    required_catalogue_ids = {
        item["evidence_id"]
        for item in planning_inventory["items"]
        if item.get("reconstruction_target")
        and isinstance(item.get("evidence_id"), str)
    }
    catalogue_artifact = build_evidence_catalogue(
        alias_map,
        code_index,
        doc_index,
        required_evidence_ids=required_catalogue_ids,
    )
    catalogue = catalogue_artifact["entries"]
    storage.save_artifact("evidence_catalogue.json", catalogue_artifact, EvidenceCatalogueSchema())
    neutral_manifest_artifact = neutral_manifest(manifest, alias_map)
    storage.save_artifact(
        "neutral_manifest.json", neutral_manifest_artifact, NeutralManifestSchema()
    )
    log.info(
        "registered %d identifier(s), minted %d evidence id(s), %d catalogue entries (%d note(s))",
        registered,
        minted,
        len(catalogue),
        len(catalogue_artifact["border_review"]),
    )

    # --- agents ---------------------------------------------------------------
    def agent_kwargs(name: str) -> dict:
        agent = settings.agent(name)
        retry_path = agent.retry_profile_path(settings.model_profiles_dir)
        kwargs = {
            "model": agent.model,
            "profile_path": str(agent.profile_path(settings.model_profiles_dir)),
            "retry_profile_path": str(retry_path) if retry_path else None,
            "max_validation_retries": agent.max_validation_retries,
            "alias_map": alias_map,
        }
        if args.stub:
            kwargs["chat_client"] = StubTextClient(_stubbed_reply)
        return kwargs

    planner = PlanningAgent(**agent_kwargs("planner"))
    plan_judge = PlanJudgeAgent(**agent_kwargs("plan_judge"))
    plan_files: list[str] = []

    def plan_for(stage: str) -> str:
        plan = planner.plan(
            stage=stage,
            source_manifest=manifest,
            evidence_catalogue=catalogue,
            judge=plan_judge,
            code_index=code_index,
            doc_index=doc_index,
            reconstruction_inventory=planning_inventory,
        )
        plan_name = f"{_plan_name(stage)}.json"
        storage.save_json(plan_name, plan)
        plan_files.append(plan_name)
        return plan

    documentation_report = DocumentationAgent(
        source_reader=reader, **agent_kwargs("doc_analyzer")
    ).analyze(
        source_manifest=manifest,
        source_doc_index=doc_index,
        mini_tasks=plan_for("documentation"),
    )
    storage.save_json("documentation_report.json", documentation_report)
    log.info("stage complete: documentation")

    code_facts_report = CodeFactsAgent(
        source_reader=reader, **agent_kwargs("code_facts_analyzer")
    ).analyze(
        source_manifest=manifest,
        source_code_index=code_index,
        mini_tasks=plan_for("code_facts"),
    )
    storage.save_json("code_facts_report.json", code_facts_report)
    log.info("stage complete: code facts")

    behavior_report = BehaviorAnalyzerAgent(
        source_reader=reader, **agent_kwargs("behavior_analyzer")
    ).analyze(
        source_manifest=manifest,
        code_facts_report=code_facts_report,
        documentation_report=documentation_report,
        source_code_index=code_index,
        mini_tasks=plan_for("behavior"),
    )
    storage.save_json("behavior_report.json", behavior_report)
    log.info("stage complete: behaviour")

    inventory = build_dirty_inventory(
        code_index,
        alias_map,
        documentation_report=documentation_report,
        code_facts_report=code_facts_report,
        behavior_report=behavior_report,
    )
    storage.save_artifact(
        "reconstruction_inventory.json", inventory, ReconstructionInventorySchema()
    )
    coverage = inventory["coverage"]
    log.info(
        "inventory coverage: %d/%d unit(s) represented in analysis reports (%.2f%%); "
        "%d/%d reconstruction target(s) covered (%.2f%%); %d indexing issue(s)",
        coverage["covered_count"],
        coverage["total_count"],
        coverage["coverage_percent"],
        coverage["covered_reconstruction_target_count"],
        coverage["reconstruction_target_count"],
        coverage["reconstruction_target_coverage_percent"],
        coverage["issue_count"],
    )

    specification = SpecSynthesizerAgent(**agent_kwargs("spec_synthesizer")).synthesize(
        source_manifest=manifest,
        alias_map=alias_map,
        documentation_report=documentation_report,
        code_facts_report=code_facts_report,
        behavior_report=behavior_report,
        mini_tasks=plan_for("specification"),
    )
    spec_path = storage.save_text("specification.md", specification)
    storage.save_private("alias_map.json", alias_map.to_private_dict())
    log.info("stage complete: specification")

    # --- Border (+ repair loop) -----------------------------------------------
    leaks = _rendered_findings(specification)
    if leaks:
        log.warning("specification carries %d Dirty BORDER-REVIEW finding(s)", leaks)

    if args.skip_border:
        log.warning("Border skipped (--skip-border); crossing artifacts were not gated")
    else:
        plans = load_plan_artifacts(storage, plan_files)
        synthesizer = SpecSynthesizerAgent(**agent_kwargs("spec_synthesizer"))
        border = BorderGateAgent(**agent_kwargs("border_gate"))

        def repair(spec_text: str, failing: list) -> str:
            return synthesizer.repair(
                spec_text,
                failing,
                alias_map,
                documentation_report=documentation_report,
                code_facts_report=code_facts_report,
                behavior_report=behavior_report,
                repo_local_path=manifest.repo_local_path,
            )

        specification, _verdict = gate_with_repairs(
            border=border,
            repair=repair,
            storage=storage,
            alias_map=alias_map,
            specification=specification,
            max_repairs=args.border_max_repairs,
            documentation_report=documentation_report,
            code_facts_report=code_facts_report,
            behavior_report=behavior_report,
            plans=plans,
            evidence_catalogue=catalogue_artifact,
            neutral_manifest=neutral_manifest_artifact,
            repo_local_path=manifest.repo_local_path,
        )
        spec_path = storage.save_text("specification.md", specification)
        log.info("stage complete: border")
        handoff_path = export_clean_handoff(
            spec_path.parent / "clean_handoff",
            specification,
            _verdict,
            compatibility_mode=args.compatibility_mode,
        )
        log.info("stage complete: clean handoff -> %s", handoff_path)

    if not args.keep_clone:
        _discard_clone(manifest.repo_local_path)

    return spec_path


def _stubbed_reply(prompt: str) -> str:
    """A schema-shaped answer for `--stub`, chosen from what the prompt asks for.

    Every stage validates its output, so one canned string cannot serve them all - a
    single reply fails the planner immediately. This recognises the request instead, which
    is enough to prove the wiring end to end without spending anything. The content is
    deliberately hollow; only the shape is real.
    """
    if "<plan_under_review>" in prompt:
        return json.dumps(
            {
                "strongest_objection": "stubbed review",
                "scores": {
                    "crux_coverage": 3,
                    "proportional_decomposition": 3,
                    "completeness": 3,
                },
                "actions": ["stubbed run - no real critique"],
            }
        )

    if "<failing_findings>" in prompt and "<specification_under_repair>" in prompt:
        # Deterministic stub repair: drop each failed original from the body.
        try:
            findings = json.loads(
                prompt.split("<failing_findings>", 1)[1].split("</failing_findings>", 1)[0]
            )
            body = prompt.split("<specification_under_repair>", 1)[1].split(
                "</specification_under_repair>", 1
            )[0].strip()
        except (IndexError, json.JSONDecodeError):
            return "# Specification\n\nStubbed repaired specification.\n"
        for item in findings:
            if not isinstance(item, dict):
                continue
            original = item.get("original")
            if isinstance(original, str) and original:
                body = body.replace(original, item.get("alias") or "the referenced element")
        return body

    if "<border_findings>" in prompt:
        # Soft findings only reach this prompt. Stub dismisses them so hollow pipeline
        # runs can still exercise Border wiring; hard leaks never ask the model.
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
                        "rationale": "stubbed adjudication - soft finding dismissed for pipeline wiring",
                    }
                    for item in findings
                    if isinstance(item, dict)
                ]
            }
        )

    if "<evidence_catalogue>" in prompt:
        stage = prompt.split("<stage>", 1)[1].split("</stage>", 1)[0].strip()
        fields = sorted(OUTPUT_FIELDS_BY_STAGE[stage])
        try:
            priority_block = prompt.split("<reconstruction_priorities>", 1)[1].split(
                "</reconstruction_priorities>", 1
            )[0]
            priorities = json.loads(priority_block)
        except (IndexError, json.JSONDecodeError):
            priorities = []
        tasks = [
            {
                "task_id": f"{stage[:4].upper()}-{index:03d}",
                "task_type": f"extract_{field}",
                "output_field": field,
                "input_refs": [],
                "requirements": ["stubbed run"],
                "min_items": 1,
            }
            for index, field in enumerate(fields, start=1)
        ]
        for index, priority in enumerate(priorities):
            evidence_id = priority.get("evidence_id") if isinstance(priority, dict) else None
            if isinstance(evidence_id, str) and tasks:
                allowed_fields = priority.get("required_output_fields") or []
                eligible = [
                    task for task in tasks if task["output_field"] in allowed_fields
                ] or tasks
                task = eligible[index % len(eligible)]
                task["input_refs"].append(
                    {"source": "reconstruction_priority", "evidence_id": evidence_id}
                )
                task["min_items"] = max(task["min_items"], len(task["input_refs"]))
        return json.dumps(
            {
                "stage": stage,
                "summary": "stubbed plan covering every allowed field once",
                "mini_tasks": tasks,
            }
        )

    if "doc_open_question" in prompt:
        return json.dumps({"label": "missing", "value": "A stubbed unresolved question."})
    if "doc_labeled" in prompt:
        return json.dumps({"label": "documented", "value": "A stubbed finding."})
    if "target_section" in prompt:
        count, _refs, evidence_ids = _stubbed_extraction_contract(prompt)
        return json.dumps(
            {
                "items": [
                    {
                        "source_ref": None,
                        "heading": f"Section {index + 1}",
                        "markdown": (
                            f"Stubbed requirement {index + 1}. Evidence: "
                            f"{evidence_ids[index] if index < len(evidence_ids) else 'not available'}"
                        ),
                    }
                    for index in range(count)
                ]
            }
        )
    # The requested field, not merely a mention of it: every prompt lists all allowed
    # fields, so matching on the whole text answers the wrong schema.
    if "[output_field]" in prompt:
        tail = prompt.split("[output_field]", 1)[1].strip().splitlines()
        field = tail[0].split()[0] if tail and tail[0].split() else ""
        return json.dumps({"items": _stubbed_narrow_items(field, prompt)})
    return json.dumps({"items": []})


def _stubbed_extraction_contract(prompt: str) -> tuple[int, list[int], list[str]]:
    def value(name: str, fallback: str) -> str:
        marker = f"{name}:"
        if marker not in prompt:
            return fallback
        return prompt.split(marker, 1)[1].splitlines()[0].strip()

    try:
        count = max(1, int(value("minimum_distinct_items", "1")))
    except ValueError:
        count = 1
    try:
        refs = json.loads(value("required_source_refs", "[]"))
    except json.JSONDecodeError:
        refs = []
    try:
        evidence_ids = json.loads(value("required_evidence_ids", "[]"))
    except json.JSONDecodeError:
        evidence_ids = []
    return count, refs, evidence_ids


def _stubbed_narrow_items(field: str, prompt: str) -> list[dict]:
    count, required_refs, _evidence_ids = _stubbed_extraction_contract(prompt)
    refs = [*required_refs, *([1] * count)][:count]

    def base(index: int) -> dict:
        return {"source_ref": refs[index] if refs else None, "label": "observed"}

    items = []
    for index in range(count):
        item = base(index)
        suffix = index + 1
        if field == "symbols":
            item.update(
                symbol=f"stub_symbol_{suffix}", kind="function", signature="()",
                inputs=[], returns="object", description=f"Stubbed symbol {suffix}.",
            )
        elif field == "imports":
            item["import"] = f"stub_dependency_{suffix}"
        elif field == "calls":
            item.update(caller=f"caller_{suffix}", callee=f"callee_{suffix}")
        elif field == "state_and_side_effects":
            item.update(
                symbol=f"stub_symbol_{suffix}", state_read=[], state_written=[],
                side_effects=[], value=f"Stubbed state fact {suffix}.",
            )
        elif field == "errors_and_exceptions":
            item.update(
                symbol=f"stub_symbol_{suffix}", error="stub error",
                handling="propagates", value=f"Stubbed error fact {suffix}.",
            )
        elif field == "behaviors":
            item.update(
                name=f"behavior_{suffix}", description=f"Stubbed behavior {suffix}.",
                inputs=[], outputs=[], preconditions=[], postconditions=[], testability="unit",
            )
        elif field == "edge_cases":
            item.update(
                behavior=f"behavior_{suffix}", case=f"case_{suffix}", current_result=None,
                expected_result=None, expected_source="unknown",
            )
        elif field == "error_handling":
            item.update(
                behavior=f"behavior_{suffix}", error="stub error", handling="propagates"
            )
        elif field == "test_candidates":
            item.update(
                name=f"scenario_{suffix}", test_type="unit", given=[], when=[], then=[],
                assertions=[f"observe result {suffix}"],
            )
        elif field == "specification_requirements":
            item.update(requirement=f"Stubbed requirement {suffix}.", source="code")
        else:
            item = {
                "source_ref": refs[index] if refs else None,
                "label": "missing",
                "value": f"Stubbed question {suffix}.",
            }
        items.append(item)
    return items


def _rendered_findings(specification: str) -> int:
    """How many BORDER-REVIEW findings the finished document actually shows."""
    return sum(1 for line in specification.splitlines() if line.startswith(f"- {BORDER_REVIEW}:"))


def _plan_name(stage: str) -> str:
    return {"documentation": "doc_plan", "specification": "spec_plan"}.get(stage, f"{stage}_plan")


def _discard_clone(path: str | None) -> None:
    if not path:
        return
    try:
        from packages.modules.ingesting import remove_tree

        remove_tree(Path(path))
    except Exception as error:
        log.warning("could not remove the clone at %s: %s", path, error)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        spec_path = run(args)
    except ConfigError as error:
        print(f"configuration problem: {error}", file=sys.stderr)
        return 2
    except HandoffError as error:
        print(f"clean handoff problem: {error}", file=sys.stderr)
        return 2
    except BorderGateError as error:
        print(f"border refused: {error}", file=sys.stderr)
        print(f"verdict: {error.verdict.finding_count} finding(s)", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130

    print(f"\nspecification: {spec_path}")
    print(f"artifacts:     {spec_path.parent}")
    if not args.skip_border:
        print(f"border:        {spec_path.parent / 'border_verdict.json'}")
        print(f"clean handoff: {spec_path.parent / 'clean_handoff'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Run the IATIN dirty-team pipeline against a git repository.

    python main.py https://github.com/owner/project

Clones the repository, indexes it, plans and runs four analysis stages, and writes a
behavioural specification plus every intermediate artifact to `artifacts/<run>/`.

The specification is a CROSSING artifact: it describes what the project does, never how
the original expressed it (CLAUDE.md section 2). Use `--stub` to exercise the whole
pipeline without spending model credits.
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
from packages.agents.dirt_team import (
    BehaviorAnalyzerAgent,
    CodeFactsAgent,
    DocumentationAgent,
    PlanJudgeAgent,
    SpecSynthesizerAgent,
)
from packages.agents.planning import PlanningAgent
from packages.modules.boundary import (
    BORDER_REVIEW,
    AliasMap,
    build_evidence_catalogue,
    mint_evidence_ids,
    neutral_manifest,
    register_code_identifiers,
)
from packages.modules.indexing import SourceCodeIndexer, SourceDocIndexer
from packages.modules.ingesting import provide_source_ingestor
from packages.modules.skills.reading import Reader
from packages.modules.storing import Storage
from packages.modules.supervising.schemas import (
    EvidenceCatalogueSchema,
    ManifestSchema,
    NeutralManifestSchema,
)
from packages.modules.supervising.verifiers.planning import OUTPUT_FIELDS_BY_STAGE

log = logging.getLogger("iatin")

STAGES = ("documentation", "code_facts", "behavior", "specification")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Read a repository and produce a clean-room behavioural specification.",
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

    catalogue_artifact = build_evidence_catalogue(alias_map, code_index, doc_index)
    catalogue = catalogue_artifact["entries"]
    storage.save_artifact("evidence_catalogue.json", catalogue_artifact, EvidenceCatalogueSchema())
    storage.save_artifact(
        "neutral_manifest.json", neutral_manifest(manifest, alias_map), NeutralManifestSchema()
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
        kwargs = {
            "model": agent.model,
            "profile_path": str(agent.profile_path(settings.model_profiles_dir)),
            "max_validation_retries": agent.max_validation_retries,
            "alias_map": alias_map,
        }
        if args.stub:
            kwargs["chat_client"] = StubTextClient(_stubbed_reply)
        return kwargs

    planner = PlanningAgent(**agent_kwargs("planner"))
    plan_judge = PlanJudgeAgent(**agent_kwargs("plan_judge"))

    def plan_for(stage: str) -> str:
        plan = planner.plan(
            stage=stage,
            source_manifest=manifest,
            evidence_catalogue=catalogue,
            judge=plan_judge,
            code_index=code_index,
            doc_index=doc_index,
        )
        storage.save_json(f"{_plan_name(stage)}.json", plan)
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

    # --- report ---------------------------------------------------------------
    # Advisory only. Step 1 records leaks, Border enforces later (CLAUDE.md section 1).
    #
    # Counted from the rendered block rather than by scanning again. Re-scanning here
    # would report a different number than the document shows: it would miss the content
    # findings (which need the dirty-side corpus) and double-count identifiers quoted in
    # the notes themselves. Reading what was written cannot drift from it.
    leaks = _rendered_findings(specification)
    if leaks:
        log.warning("specification carries %d BORDER-REVIEW finding(s)", leaks)

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

    if "<evidence_catalogue>" in prompt:
        stage = prompt.split("<stage>", 1)[1].split("</stage>", 1)[0].strip()
        fields = sorted(OUTPUT_FIELDS_BY_STAGE[stage])
        return json.dumps(
            {
                "stage": stage,
                "summary": "stubbed plan covering every allowed field once",
                "mini_tasks": [
                    {
                        "task_id": f"{stage[:4].upper()}-{index:03d}",
                        "task_type": f"extract_{field}",
                        "output_field": field,
                        "input_refs": [],
                        "requirements": ["stubbed run"],
                        "min_items": 1,
                    }
                    for index, field in enumerate(fields, start=1)
                ],
            }
        )

    if "doc_open_question" in prompt:
        return json.dumps({"label": "missing", "value": "A stubbed unresolved question."})
    if "doc_labeled" in prompt:
        return json.dumps({"label": "documented", "value": "A stubbed finding."})
    if "target_section" in prompt:
        return json.dumps(
            {"items": [{"source_ref": None, "heading": "Section", "markdown": "Stubbed."}]}
        )
    # The requested field, not merely a mention of it: every prompt lists all allowed
    # fields, so matching on the whole text answers the wrong schema.
    if "[output_field]" in prompt:
        tail = prompt.split("[output_field]", 1)[1].strip().splitlines()
        field = tail[0].split()[0] if tail and tail[0].split() else ""
        if field.endswith("open_questions"):
            return json.dumps(
                {"items": [{"source_ref": None, "label": "missing", "value": "Stubbed question."}]}
            )
    return json.dumps({"items": []})


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
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130

    print(f"\nspecification: {spec_path}")
    print(f"artifacts:     {spec_path.parent}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

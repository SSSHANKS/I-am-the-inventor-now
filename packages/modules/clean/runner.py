from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packages.agents.base_agent import ModelCallError
from packages.agents.clean_team import (
    CleanArchitectAgent,
    CleanBehaviorProbeAgent,
    CleanBuilderAgent,
    CleanManifestAgent,
    CleanPlannerAgent,
    CleanRepairAgent,
)
from packages.modules.clean.adapters import (
    ReadinessExecutor,
    RuntimeAdapter,
    project_first_planning_policy,
    select_runtime_adapter,
)
from packages.modules.clean.architecture import (
    normalise_architecture,
    normalise_behavior_rule_evidence,
    salvage_ungrounded_behavior_rules,
    validate_architecture,
)
from packages.modules.clean.behavior import (
    BehaviorProbeExecutor,
    validate_behavior_probe_suite,
)
from packages.modules.clean.context import build_scoped_context
from packages.modules.clean.diagnostics import (
    CleanDiagnostic,
    content_hashes,
    failure_fingerprint,
)
from packages.modules.clean.execution import ValidationExecutor, is_python_test_path
from packages.modules.clean.manifest import validate_manifest
from packages.modules.clean.python_contracts import python_contract_declaration_error
from packages.modules.clean.repair_candidate import (
    evaluate_repair_candidate,
    select_non_regressing_repair_subset,
)
from packages.modules.clean.requirements import prepare_specification, requirement_statements
from packages.modules.clean.validation import checks_passed, failed_paths, validate_project
from packages.modules.clean.workspace import (
    MAX_FILES,
    CleanWorkspace,
    WorkspaceError,
    validate_relative_path,
)
from packages.modules.handoff import CleanHandoff, load_clean_handoff
from packages.modules.supervising.schemas import (
    CleanArchitectureSchema,
    CleanBehaviorProbeSuiteSchema,
    CleanBuildReportSchema,
    CleanManifestSchema,
    CleanPlanSchema,
)

log = logging.getLogger(__name__)


class CleanBuildError(Exception):
    """Clean could not safely plan or construct the requested project."""


class _ManifestPlanningFailure(ValueError):
    def __init__(self, message, architecture, manifest, attempts):
        super().__init__(message)
        self.architecture = deepcopy(architecture)
        self.manifest = deepcopy(manifest)
        self.attempts = deepcopy(attempts)


class _ArchitecturePlanningFailure(ValueError):
    def __init__(self, message, architecture, attempts):
        super().__init__(message)
        self.architecture = deepcopy(architecture)
        self.attempts = deepcopy(attempts)


MAX_DEPENDENCY_CONTEXT_BYTES = 512 * 1024


@dataclass(frozen=True)
class CleanBuildResult:
    status: str
    output_root: Path
    project_root: Path
    report_path: Path
    report: dict[str, Any]

    @property
    def succeeded(self) -> bool:
        return self.status == "success"


class CleanRunner:
    def __init__(
        self,
        planner: CleanPlannerAgent | None,
        builder: CleanBuilderAgent,
        repairer: CleanRepairAgent,
        *,
        architect: CleanArchitectAgent | None = None,
        manifest_designer: CleanManifestAgent | None = None,
        behavior_prober: CleanBehaviorProbeAgent | None = None,
        behavior_executor: BehaviorProbeExecutor | None = None,
        readiness_executor: ReadinessExecutor | None = None,
        execution_configurator: Callable[[RuntimeAdapter], tuple[Any, Any]] | None = None,
        max_repairs: int = 2,
        syntax_checks: bool = True,
        validation_executor: ValidationExecutor | None = None,
    ) -> None:
        if max_repairs < 0:
            raise ValueError("max_repairs cannot be negative")
        if (architect is None) != (manifest_designer is None):
            raise ValueError(
                "Clean project-first planning requires both architect and manifest designer"
            )
        if planner is None and architect is None:
            raise ValueError("Clean requires either a legacy planner or project-first agents")
        if (behavior_prober is None) != (behavior_executor is None):
            raise ValueError(
                "Clean behavioral validation requires both a probe designer and executor"
            )
        if behavior_prober is not None and architect is None:
            raise ValueError("Clean behavioral validation requires project-first planning")
        if execution_configurator is not None:
            if architect is None:
                raise ValueError("Adapter execution configuration requires project-first planning")
            if any(item is not None for item in (behavior_prober, behavior_executor, readiness_executor)):
                raise ValueError("Use adapter execution configuration or injected executors, not both")
        agents = (planner, architect, manifest_designer, behavior_prober, builder, repairer)
        for agent in (item for item in agents if item is not None):
            if agent.source_reader is not None or agent.alias_map is not None:
                raise CleanBuildError("Clean agents cannot receive a source reader or alias map")
            if agent.artifact_verifier is not None:
                raise CleanBuildError("Clean agents cannot receive a source artifact verifier")
        self.planner = planner
        self.architect = architect
        self.manifest_designer = manifest_designer
        self.behavior_prober = behavior_prober
        self.behavior_executor = behavior_executor
        self.builder = builder
        self.repairer = repairer
        self.max_repairs = max_repairs
        self.syntax_checks = syntax_checks
        self.validation_executor = validation_executor
        self.readiness_executor = readiness_executor
        self.execution_configurator = execution_configurator

    def run(self, handoff_dir: str | Path, output_root: str | Path) -> CleanBuildResult:
        handoff = load_clean_handoff(handoff_dir)
        CleanWorkspace.require_available(output_root)
        project_first = self.architect is not None
        architecture: dict[str, Any] | None = None
        manifest: dict[str, Any] | None = None
        runtime_adapter: RuntimeAdapter | None = None
        behavior_suite: dict[str, Any] | None = None
        behavior_probe_design_failure: str | None = None
        self._behavior_probe_audit: list[dict[str, Any]] = []
        adapter_generated_files: list[dict[str, Any]] = []
        validation_policy: dict[str, Any]
        try:
            prepared_specification = prepare_specification(handoff.specification)
            if project_first:
                assert self.architect is not None
                assert self.manifest_designer is not None
                validation_policy = project_first_planning_policy(
                    syntax_checks=self.syntax_checks,
                )
                architecture = self._design_architecture(
                    prepared_specification.text,
                    compatibility_mode=handoff.compatibility_mode,
                    validation_policy=validation_policy,
                )
                (architecture, manifest, runtime_adapter, behavior_suite,
                 behavior_probe_design_failure) = self._design_probe_consistent_project(
                    prepared_specification.text, architecture,
                    compatibility_mode=handoff.compatibility_mode,
                    validation_policy=validation_policy,
                )
                plan = _compatibility_plan(architecture, manifest)
                adapter_generated_files = runtime_adapter.materialise_behavior_tests(
                    architecture, manifest, behavior_suite
                )
                _append_adapter_generated_tests(plan, adapter_generated_files)
                self._validate_plan(
                    plan,
                    prepared_specification.text,
                    project_first=True,
                )
            else:
                assert self.planner is not None
                validation_policy = (
                    self.validation_executor.planning_policy().to_prompt_dict()
                    if self.validation_executor is not None
                    else {"enabled": False}
                )
                plan = self.planner.plan(
                    prepared_specification.text,
                    compatibility_mode=handoff.compatibility_mode,
                    executable_validation_policy=validation_policy,
                )
                plan = CleanPlanSchema().load(plan)
                plan = _normalise_plan(
                    plan,
                    specification=prepared_specification.text,
                    ensure_python_test_coverage=(
                        validation_policy.get("adapter") == "local-python"
                    ),
                )
                self._validate_plan(plan, prepared_specification.text)
        except Exception as exc:
            if not project_first:
                raise CleanBuildError(f"Clean planning failed: {exc}") from exc
            diagnostic_location = ""
            try:
                failed_workspace = CleanWorkspace(output_root)
                details = {
                    "status": "planning-failed",
                    "stage": ("manifest" if isinstance(exc, _ManifestPlanningFailure) else
                              "architecture" if isinstance(exc, _ArchitecturePlanningFailure) else "planning"),
                    "error": _safe_error(exc),
                    "architecture": getattr(exc, "architecture", architecture),
                    "manifest": getattr(exc, "manifest", manifest),
                    "attempts": getattr(exc, "attempts", []),
                }
                saved = failed_workspace.write_metadata_json("planning_failure.json", details)
                diagnostic_location = f"; planning diagnostics: {saved}"
            except Exception as save_error:
                log.warning("Could not save planning diagnostics: %s", _safe_error(save_error))
            raise CleanBuildError(f"Clean planning failed: {exc}{diagnostic_location}") from exc

        workspace = CleanWorkspace(output_root)
        workspace.write_metadata_json(
            "requirements.json", prepared_specification.audit_record()
        )
        if architecture is not None:
            workspace.write_metadata_json("architecture.json", architecture)
        if manifest is not None:
            workspace.write_metadata_json("manifest.json", manifest)
        if behavior_suite is not None:
            workspace.write_metadata_json("behavior_probes.json", behavior_suite)
        if self._behavior_probe_audit:
            workspace.write_metadata_json(
                "behavior_probe_design.json",
                {
                    "schema_version": 1,
                    "capabilities": self._behavior_probe_audit,
                },
            )
        if adapter_generated_files:
            workspace.write_metadata_json(
                "published_tests.json",
                {
                    "schema_version": 1,
                    "files": [
                        {
                            "path": item["path"],
                            "sha256": hashlib.sha256(
                                item["content"].encode("utf-8")
                            ).hexdigest(),
                            "requirement_ids": item["requirement_ids"],
                            "scenario_ids": item.get("scenario_ids", []),
                        }
                        for item in adapter_generated_files
                    ],
                },
            )
        if project_first:
            workspace.write_metadata_json("adapter_policy.json", validation_policy)
        workspace.write_metadata_json("clean_plan.json", plan)
        plan_hash = _json_hash(plan)
        generation_failures: list[dict[str, Any]] = []
        adapter_files_by_path = {
            item["path"]: item for item in adapter_generated_files
        }
        for task in sorted(plan["files"], key=lambda item: item["generation_order"]):
            path = task["path"]
            try:
                if path in adapter_files_by_path:
                    workspace.write_metadata_json(
                        f"generation/{task['generation_order']:03d}.json",
                        {
                            "path": path,
                            "dependency_paths": list(task["depends_on"]),
                            "generation_owner": "adapter",
                        },
                    )
                    workspace.write_generated_files(
                        [adapter_files_by_path[path]], allowed_paths={path}
                    )
                    continue
                scoped_context = build_scoped_context(
                    prepared_specification.text,
                    plan,
                    [path, *task["depends_on"]],
                )
                dependency_files = {
                    dependency: workspace.read_generated_file(dependency)
                    for dependency in task["depends_on"]
                }
                dependency_size = sum(
                    len(content.encode("utf-8")) for content in dependency_files.values()
                )
                if dependency_size > MAX_DEPENDENCY_CONTEXT_BYTES:
                    raise WorkspaceError(
                        f"Dependency context exceeds limit for generated path {path!r}"
                    )
                workspace.write_metadata_json(
                    f"generation/{task['generation_order']:03d}.json",
                    {
                        "path": path,
                        "dependency_paths": sorted(dependency_files),
                        **scoped_context.audit_record(),
                    },
                )
                batch = self.builder.build_file(
                    scoped_context.specification,
                    scoped_context.plan,
                    task,
                    compatibility_mode=handoff.compatibility_mode,
                    dependency_files=dependency_files,
                )
                returned_paths = [item["path"] for item in batch["files"]]
                if returned_paths != [path]:
                    raise WorkspaceError(
                        f"Builder must return exactly requested path {path!r}; got {returned_paths!r}"
                    )
                returned_requirements = set(batch["files"][0]["requirement_ids"])
                if returned_requirements != set(task["requirement_ids"]):
                    raise WorkspaceError(
                        f"Builder requirement IDs do not match the task for {path!r}"
                    )
                if runtime_adapter is not None:
                    batch["files"][0]["content"] = (
                        runtime_adapter.normalise_generated_content(
                            path,
                            batch["files"][0]["content"],
                            architecture,
                            manifest,
                        )
                    )
                workspace.write_generated_files(batch["files"], allowed_paths={path})
            except Exception as exc:
                generation_failures.append(
                    _failed_check(
                        "generation",
                        _safe_error(exc),
                        [path],
                        stage="generation",
                    )
                )

        checks = self._validate(
            workspace,
            plan,
            generation_failures,
            run_executable_validation=not project_first,
            runtime_adapter=runtime_adapter,
            architecture=architecture,
            manifest=manifest,
            behavior_suite=behavior_suite,
            behavior_probe_design_failure=behavior_probe_design_failure,
        )
        repairs_used = 0
        structural_repairs_used = 0
        behavior_repairs_used = 0
        repair_stop_reason: str | None = None
        rejected_feedback: dict[str, Any] | None = None
        while not checks_passed(checks):
            behavior_stage = _only_behavior_failures(checks)
            stage_repairs_used = (
                behavior_repairs_used if behavior_stage else structural_repairs_used
            )
            if stage_repairs_used >= self.max_repairs:
                repair_stop_reason = (
                    "behavior-repair-budget-exhausted"
                    if behavior_stage
                    else "repair-budget-exhausted"
                )
                break
            allowed = _repair_paths(checks, plan)
            if not allowed:
                repair_stop_reason = "no-repairable-paths"
                break
            before_failure_fingerprint = failure_fingerprint(checks)
            repairs_used += 1
            if behavior_stage:
                behavior_repairs_used += 1
            else:
                structural_repairs_used += 1
            failures = [check for check in checks if check["status"] == "fail"]
            if rejected_feedback is not None:
                failures.append(rejected_feedback)
            try:
                generated = set(workspace.list_generated_files())
                current = {
                    path: workspace.read_generated_file(path)
                    for path in sorted(allowed)
                    if path in generated
                }
                current_size = sum(
                    len(content.encode("utf-8")) for content in current.values()
                )
                if current_size > MAX_DEPENDENCY_CONTEXT_BYTES:
                    raise WorkspaceError("Repair file context exceeds the 512 KiB prompt limit")
                related, omitted_related = _repair_context(
                    workspace,
                    plan,
                    allowed,
                    max_bytes=MAX_DEPENDENCY_CONTEXT_BYTES - current_size,
                )
                scoped_context = build_scoped_context(
                    prepared_specification.text,
                    plan,
                    allowed | set(related),
                )
                repair = self.repairer.repair_files(
                    scoped_context.specification,
                    scoped_context.plan,
                    failures,
                    current,
                    allowed,
                    compatibility_mode=handoff.compatibility_mode,
                    attempt_number=stage_repairs_used + 1,
                    related_files=related,
                    omitted_related_paths=omitted_related,
                    failing_behavior_probes=_failing_behavior_probes(
                        checks, behavior_suite
                    ),
                )
                replacements = repair["replacements"]
                if runtime_adapter is not None:
                    for replacement in replacements:
                        replacement["content"] = runtime_adapter.normalise_generated_content(
                            replacement["path"], replacement["content"], architecture, manifest
                        )
                paths = [item["path"] for item in replacements]
                output_errors = []
                if not paths:
                    output_errors.append("Repair returned no replacement paths")
                elif len(paths) != len(set(paths)):
                    output_errors.append("Repair returned duplicate replacement paths")
                outside = sorted(set(paths) - allowed)
                if outside:
                    output_errors.append(
                        "Repair attempted to modify paths outside its failure task: "
                        + ", ".join(outside)
                    )
                if output_errors:
                    rationale = str(repair.get("rationale", "")).strip()
                    rejection_message = "\n".join(output_errors)
                    if rationale:
                        rejection_message += (
                            "\nRejected candidate rationale (the candidate was not published): "
                            + rationale
                        )
                    rejected_feedback = _failed_check(
                        "repair-candidate-rejected",
                        rejection_message,
                        sorted(allowed),
                        stage="repair",
                    )
                    before_content_hashes = content_hashes(current)
                    workspace.write_metadata_json(
                        f"iterations/repair-{repairs_used}.json",
                        {
                            "outcome": "rejected",
                            "stop_reason": None,
                            "rationale": repair["rationale"],
                            "allowed_paths": sorted(allowed),
                            "replaced_paths": sorted(paths),
                            "changed_paths": [],
                            "candidate_changed_paths": sorted(set(paths)),
                            "regressions": output_errors,
                            "candidate_replacements": replacements,
                            "before_content_hashes": before_content_hashes,
                            "after_content_hashes": before_content_hashes,
                            "before_failure_fingerprint": before_failure_fingerprint,
                            "after_failure_fingerprint": before_failure_fingerprint,
                            **scoped_context.audit_record(),
                        },
                    )
                    continue
                replacements_by_path = {
                    item["path"]: item["content"] for item in replacements
                }
                changed_paths = sorted(
                    path
                    for path, content in replacements_by_path.items()
                    if current.get(path) != content
                )
                before_content_hashes = content_hashes(current)
                if not changed_paths:
                    repair_stop_reason = "unchanged-replacement"
                    workspace.write_metadata_json(
                        f"iterations/repair-{repairs_used}.json",
                        {
                            "allowed_paths": sorted(allowed),
                            "related_paths": sorted(related),
                            "omitted_related_paths": omitted_related,
                            "replaced_paths": sorted(paths),
                            "changed_paths": [],
                            "rationale": repair["rationale"],
                            "outcome": "stalled",
                            "stop_reason": repair_stop_reason,
                            "before_failure_fingerprint": before_failure_fingerprint,
                            "after_failure_fingerprint": before_failure_fingerprint,
                            "before_content_hashes": before_content_hashes,
                            "after_content_hashes": before_content_hashes,
                            **scoped_context.audit_record(),
                        },
                    )
                    break
                current_snapshot = tuple(current.items())

                def validate_candidate(
                    candidate_workspace,
                    *,
                    allowed_paths=frozenset(allowed),
                    current_items=current_snapshot,
                    base_generation_failures=tuple(generation_failures),
                ):
                    current_files = dict(current_items)
                    candidate_files = set(candidate_workspace.list_generated_files())
                    candidate_changed_paths = {
                        path
                        for path in allowed_paths
                        if path in candidate_files
                        and candidate_workspace.read_generated_file(path)
                        != current_files.get(path)
                    }
                    candidate_generation_failures = [
                        check
                        for check in base_generation_failures
                        if not set(check["paths"]).intersection(candidate_changed_paths)
                    ]
                    return self._validate(
                        candidate_workspace,
                        plan,
                        candidate_generation_failures,
                        run_executable_validation=not project_first,
                        runtime_adapter=runtime_adapter,
                        architecture=architecture,
                        manifest=manifest,
                        behavior_suite=behavior_suite,
                        behavior_probe_design_failure=behavior_probe_design_failure,
                    )
                candidate_checks, regressions = evaluate_repair_candidate(
                    workspace, replacements, allowed, checks, validate_candidate,
                )
                rejected_candidate_paths: list[str] = []
                if regressions and len(replacements) > 1:
                    safe_replacements, safe_checks = select_non_regressing_repair_subset(
                        workspace,
                        replacements,
                        allowed,
                        checks,
                        validate_candidate,
                    )
                    if safe_replacements and safe_checks is not None:
                        accepted_paths = {
                            item["path"] for item in safe_replacements
                        }
                        rejected_candidate_paths = sorted(set(paths) - accepted_paths)
                        replacements = safe_replacements
                        paths = [item["path"] for item in replacements]
                        replacements_by_path = {
                            item["path"]: item["content"] for item in replacements
                        }
                        changed_paths = sorted(
                            path
                            for path, content in replacements_by_path.items()
                            if current.get(path) != content
                        )
                        candidate_checks = safe_checks
                        regressions = []
                if regressions:
                    rejection_message = "\n".join(regressions)
                    rationale = str(repair.get("rationale", "")).strip()
                    if rationale:
                        rejection_message += (
                            "\nRejected candidate rationale (the candidate was not published): "
                            + rationale
                        )
                    rejected_feedback = _failed_check(
                        'repair-candidate-rejected', rejection_message, sorted(paths), stage='repair',
                    )
                    workspace.write_metadata_json(f'iterations/repair-{repairs_used}.json', {
                        'outcome': 'rejected', 'stop_reason': None,
                        'rationale': repair['rationale'], 'allowed_paths': sorted(allowed),
                        'replaced_paths': sorted(paths), 'changed_paths': [],
                        'candidate_changed_paths': changed_paths, 'regressions': regressions,
                        'candidate_checks': candidate_checks, 'candidate_replacements': replacements,
                        'before_content_hashes': before_content_hashes,
                        'after_content_hashes': before_content_hashes,
                        'before_failure_fingerprint': before_failure_fingerprint,
                        'after_failure_fingerprint': before_failure_fingerprint,
                        **scoped_context.audit_record(),
                    })
                    continue
                workspace.write_generated_files(replacements, allowed_paths=allowed)
                accepted_paths = {item["path"] for item in replacements}
                generation_failures = [
                    check
                    for check in generation_failures
                    if not set(check["paths"]).intersection(accepted_paths)
                ]
                checks = candidate_checks
                rejected_feedback = None
                after_failure_fingerprint = failure_fingerprint(checks)
                after_contents = {
                    path: workspace.read_generated_file(path)
                    for path in sorted(paths)
                    if path in set(workspace.list_generated_files())
                }
                stalled = (
                    not checks_passed(checks)
                    and after_failure_fingerprint == before_failure_fingerprint
                )
                workspace.write_metadata_json(
                    f"iterations/repair-{repairs_used}.json",
                    {
                        "allowed_paths": sorted(allowed),
                        "related_paths": sorted(related),
                        "omitted_related_paths": omitted_related,
                        "replaced_paths": sorted(paths),
                        "changed_paths": changed_paths,
                        "rejected_candidate_paths": rejected_candidate_paths,
                        "rationale": repair["rationale"],
                        "outcome": (
                            "resolved"
                            if checks_passed(checks)
                            else "changed"
                        ),
                        "stop_reason": None,
                        "failure_fingerprint_repeated": stalled,
                        "before_failure_fingerprint": before_failure_fingerprint,
                        "after_failure_fingerprint": after_failure_fingerprint,
                        "before_content_hashes": before_content_hashes,
                        "after_content_hashes": content_hashes(after_contents),
                        **scoped_context.audit_record(),
                    },
                )
            except ModelCallError as exc:
                if exc.fatal:
                    raise
                before_content_hashes = content_hashes(current)
                workspace.write_metadata_json(
                    f"iterations/repair-{repairs_used}.json",
                    {
                        "outcome": "model-call-failed",
                        "stop_reason": None,
                        "error": _safe_error(exc),
                        "error_kind": exc.kind,
                        "allowed_paths": sorted(allowed),
                        "changed_paths": [],
                        "before_failure_fingerprint": before_failure_fingerprint,
                        "after_failure_fingerprint": before_failure_fingerprint,
                        "before_content_hashes": before_content_hashes,
                        "after_content_hashes": before_content_hashes,
                        **scoped_context.audit_record(),
                    },
                )
                # A transient provider failure produced no candidate. Preserve
                # the last validated project and its real failures; another
                # budgeted attempt may retry without poisoning validation.
                rejected_feedback = None
                continue
            except Exception as exc:
                generation_failures.append(
                    _failed_check(
                        "repair",
                        _safe_error(exc),
                        sorted(allowed),
                        stage="repair",
                    )
                )
                checks = self._validate(
                    workspace,
                    plan,
                    generation_failures,
                    run_executable_validation=not project_first,
                    runtime_adapter=runtime_adapter,
                    architecture=architecture,
                    manifest=manifest,
                    behavior_suite=behavior_suite,
                    behavior_probe_design_failure=behavior_probe_design_failure,
                )

        report = self._build_report(
            handoff=handoff,
            plan=plan,
            plan_hash=plan_hash,
            workspace=workspace,
            checks=checks,
            repairs_used=repairs_used,
            repair_stop_reason=repair_stop_reason,
            planning_mode="project-first" if project_first else "legacy",
            architecture=architecture,
            manifest=manifest,
            behavior_suite=behavior_suite,
            runtime_adapter_id=(
                runtime_adapter.adapter_id if runtime_adapter is not None else None
            ),
        )
        report = CleanBuildReportSchema().load(report)
        report_path = workspace.write_metadata_json("clean_build_report.json", report)
        return CleanBuildResult(
            status=report["status"],
            output_root=workspace.root,
            project_root=workspace.project_root,
            report_path=report_path,
            report=report,
        )

    def _design_probe_consistent_project(
        self, specification, architecture, *, compatibility_mode, validation_policy,
    ):
        """Resolve interface conflicts before files or frozen probes are generated.

        Probe mistakes go back to the prober first. Persistent interface conflicts
        are reviewed by the architect against the specification, never repaired by
        teaching production code to imitate an undeclared test hook.
        """
        from packages.modules.clean.probe_contracts import ProbeArchitectureError

        for attempt in range(self.max_repairs + 1):
            adapter = select_runtime_adapter(architecture, readiness_executor=self.readiness_executor)
            configurator = getattr(self, 'execution_configurator', None)
            if configurator is not None:
                self.behavior_prober, self.behavior_executor = configurator(adapter)
                if (self.behavior_prober is None) != (self.behavior_executor is None):
                    raise CleanBuildError('Adapter must supply both probe designer and executor, or neither')
                if self.behavior_prober is not None and any(
                    getattr(self.behavior_prober, field, None) is not None
                    for field in ('source_reader', 'alias_map', 'artifact_verifier')
                ):
                    raise CleanBuildError('Adapter probe designer violates the Clean-room boundary')
            manifest = self._design_manifest(
                architecture, runtime_adapter=adapter, validation_policy=validation_policy,
            )
            if self.behavior_prober is None:
                return architecture, manifest, adapter, None, None
            if not getattr(adapter, 'supports_behavior_probes', True):
                return architecture, manifest, adapter, None, (
                    f'Adapter {adapter.adapter_id} has no behavioral probe toolchain'
                )
            try:
                suite = self._design_behavior_probes(specification, architecture, manifest)
                return architecture, manifest, adapter, suite, None
            except ProbeArchitectureError as exc:
                if attempt < self.max_repairs:
                    diagnostic = (
                        f'Probe/contract conflict: {exc}. The approved specification is '
                        'authoritative. Correct missing or contradictory contracts only '
                        'when supported by it; do not copy invented probe interfaces.'
                    )
                    candidate = self.architect.revise(
                        specification, architecture, diagnostic,
                        compatibility_mode=compatibility_mode, runtime_policy=validation_policy,
                    )
                    architecture = self._design_architecture(
                        specification, compatibility_mode=compatibility_mode,
                        validation_policy=validation_policy, candidate=candidate,
                    )
                    continue
                failure = f'Architecture/probe consistency review exhausted: {exc}'
            except Exception as exc:
                failure = _safe_error(exc)
            log.warning('Behavior probe design unavailable; degraded validation: %s', failure)
            return architecture, manifest, adapter, None, failure

    def _design_architecture(
        self,
        specification: str,
        *,
        compatibility_mode: str,
        validation_policy: dict[str, Any],
        candidate: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Design, validate, and semantically repair the project architecture."""
        assert self.architect is not None
        architecture = candidate if candidate is not None else self.architect.design(
            specification,
            compatibility_mode=compatibility_mode,
            runtime_policy=validation_policy,
        )
        attempts = []
        for repair_number in range(self.max_repairs + 1):
            input_architecture = deepcopy(architecture)
            try:
                architecture = CleanArchitectureSchema().load(architecture)
                architecture = normalise_architecture(architecture)
                architecture = normalise_behavior_rule_evidence(
                    architecture, specification
                )
                return validate_architecture(
                    architecture,
                    specification,
                    compatibility_mode=compatibility_mode,
                )
            except Exception as exc:
                attempts.append({
                    "attempt": repair_number + 1,
                    "input_to_validation": input_architecture,
                    "architecture_under_review": deepcopy(architecture),
                    "diagnostic": _safe_error(exc),
                })
                if repair_number >= self.max_repairs:
                    recovered = self._recover_ungrounded_architecture(
                        architecture,
                        specification,
                        compatibility_mode=compatibility_mode,
                    )
                    if recovered is not None:
                        return recovered
                    raise _ArchitecturePlanningFailure(
                        _safe_error(exc), architecture, attempts,
                    ) from exc
                try:
                    architecture = self.architect.revise(
                        specification, architecture, _safe_error(exc),
                        compatibility_mode=compatibility_mode, runtime_policy=validation_policy,
                    )
                except Exception as revision_error:
                    recovered = self._recover_ungrounded_architecture(
                        architecture,
                        specification,
                        compatibility_mode=compatibility_mode,
                    )
                    if recovered is not None:
                        return recovered
                    raise _ArchitecturePlanningFailure(
                        f"Architecture revision failed: {_safe_error(revision_error)}",
                        architecture, attempts,
                    ) from revision_error

    @staticmethod
    def _recover_ungrounded_architecture(
        architecture: dict[str, Any],
        specification: str,
        *,
        compatibility_mode: str,
    ) -> dict[str, Any] | None:
        """Continue only when dropping false evidence links makes the design valid."""
        try:
            candidate = CleanArchitectureSchema().load(architecture)
            candidate = normalise_architecture(candidate)
            candidate = normalise_behavior_rule_evidence(candidate, specification)
            candidate, discarded = salvage_ungrounded_behavior_rules(
                candidate, specification
            )
            if not discarded:
                return None
            validated = validate_architecture(
                candidate,
                specification,
                compatibility_mode=compatibility_mode,
            )
        except Exception:
            return None
        log.warning(
            "Architecture recovery omitted %d ungrounded behavior rule(s); "
            "details remain in unresolved_gaps",
            len(discarded),
        )
        return validated

    def _design_manifest(
        self,
        architecture: dict[str, Any],
        *,
        runtime_adapter: RuntimeAdapter,
        validation_policy: dict[str, Any],
    ) -> dict[str, Any]:
        """Design, validate, and semantically repair the exact project manifest."""
        assert self.manifest_designer is not None
        manifest = self.manifest_designer.derive(
            architecture,
            adapter_policy=validation_policy,
        )
        attempts = []
        for repair_number in range(self.max_repairs + 1):
            input_manifest = deepcopy(manifest)
            try:
                manifest = CleanManifestSchema().load(manifest)
                manifest = runtime_adapter.normalise_manifest(architecture, manifest)
                validate_manifest(manifest, architecture)
                runtime_adapter.validate_manifest(
                    architecture,
                    manifest,
                    syntax_checks=self.syntax_checks,
                )
                return manifest
            except Exception as exc:
                attempts.append({
                    "attempt": repair_number + 1,
                    "input_to_adapter": input_manifest,
                    "manifest_under_review": deepcopy(manifest),
                    "diagnostic": _safe_error(exc),
                })
                if repair_number >= self.max_repairs:
                    raise _ManifestPlanningFailure(
                        _safe_error(exc), architecture, manifest, attempts,
                    ) from exc
                manifest = self.manifest_designer.revise(
                    architecture,
                    manifest,
                    _safe_error(exc),
                    adapter_policy=validation_policy,
                )

    def _design_behavior_probes(
        self,
        specification: str,
        architecture: dict[str, Any],
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        """Design a stable oracle once, then keep it fixed through file repairs."""
        assert self.behavior_prober is not None
        if not hasattr(self, "_behavior_probe_audit"):
            self._behavior_probe_audit = []
        combined: list[dict[str, Any]] = []
        for capability in architecture["capabilities"]:
            scoped_architecture = deepcopy(architecture)
            scoped_architecture["capabilities"] = [capability]
            suite = self.behavior_prober.design(
                specification,
                architecture,
                manifest,
                target_capability=capability,
            )
            accepted_by_requirement: dict[str, dict[str, Any]] = {}
            capability_attempts: list[dict[str, Any]] = []
            for repair_number in range(self.max_repairs + 1):
                rejected_messages: list[str] = []
                architecture_error = None
                try:
                    candidates = CleanBehaviorProbeSuiteSchema().load(suite)
                except Exception as exc:
                    candidates = {"schema_version": 1, "probes": []}
                    rejected_messages.append(f"{type(exc).__name__}: {exc}")
                for probe in candidates["probes"]:
                    requirement_ids = probe.get("requirement_ids", [])
                    requirement_id = (
                        str(requirement_ids[0]) if len(requirement_ids) == 1 else None
                    )
                    try:
                        validate_behavior_probe_suite(
                            {"schema_version": 1, "probes": [probe]},
                            scoped_architecture,
                            require_complete=False,
                        )
                    except Exception as exc:
                        from packages.modules.clean.probe_contracts import ProbeArchitectureError

                        if isinstance(exc, ProbeArchitectureError):
                            architecture_error = exc
                        rejected_messages.append(f"{type(exc).__name__}: {exc}")
                        log.warning("Discarding invalid behavior probe: %s", exc)
                        continue
                    assert requirement_id is not None
                    existing = accepted_by_requirement.get(requirement_id)
                    required_scenarios = set(capability["scenario_ids"])
                    if existing is None or (
                        set(probe["scenario_ids"]) & required_scenarios
                        and not set(existing["scenario_ids"]) & required_scenarios
                    ):
                        accepted_by_requirement[requirement_id] = probe

                accepted = [
                    deepcopy(probe)
                    for _, probe in sorted(accepted_by_requirement.items())
                ]
                for index, probe in enumerate(accepted, start=1):
                    probe["probe_id"] = f"PROBE-{index:03d}"
                partial_suite = {"schema_version": 1, "probes": accepted}
                try:
                    suite = validate_behavior_probe_suite(
                        partial_suite, scoped_architecture
                    )
                    capability_attempts.append({
                        "attempt": repair_number + 1,
                        "accepted_requirement_ids": sorted(accepted_by_requirement),
                        "missing_requirement_ids": [],
                        "diagnostics": list(dict.fromkeys(rejected_messages)),
                    })
                    break
                except Exception as exc:
                    rejected_messages.append(f"{type(exc).__name__}: {exc}")
                    missing_requirement_ids = sorted(
                        set(capability["requirement_ids"])
                        - set(accepted_by_requirement)
                    )
                    missing_scenario_ids = sorted(
                        set(capability["scenario_ids"])
                        - {
                            scenario_id
                            for probe in accepted
                            for scenario_id in probe["scenario_ids"]
                        }
                    )
                    capability_attempts.append({
                        "attempt": repair_number + 1,
                        "accepted_requirement_ids": sorted(accepted_by_requirement),
                        "missing_requirement_ids": missing_requirement_ids,
                        "missing_scenario_ids": missing_scenario_ids,
                        "diagnostics": list(dict.fromkeys(rejected_messages)),
                    })
                    if repair_number >= self.max_repairs:
                        if architecture_error is not None:
                            raise architecture_error from exc
                        suite = partial_suite
                        break
                    target_requirement_ids = missing_requirement_ids
                    if not target_requirement_ids and missing_scenario_ids:
                        target_requirement_ids = [
                            next(iter(sorted(accepted_by_requirement)))
                        ]
                    suite = self.behavior_prober.revise(
                        specification,
                        architecture,
                        manifest,
                        partial_suite,
                        "\n".join(dict.fromkeys(rejected_messages)),
                        target_capability=capability,
                        target_requirement_ids=target_requirement_ids,
                    )
            self._behavior_probe_audit.append({
                "capability_id": capability["capability_id"],
                "attempts": capability_attempts,
                "accepted_requirement_ids": sorted(accepted_by_requirement),
                "missing_requirement_ids": sorted(
                    set(capability["requirement_ids"])
                    - set(accepted_by_requirement)
                ),
            })
            combined.extend(suite["probes"])
        for index, probe in enumerate(combined, start=1):
            probe["probe_id"] = f"PROBE-{index:03d}"
        return validate_behavior_probe_suite(
            {"schema_version": 1, "probes": combined}, architecture,
            require_complete=False,
        )

    def _validate(
        self,
        workspace: CleanWorkspace,
        plan: dict[str, Any],
        generation_failures: list[dict[str, Any]],
        *,
        run_executable_validation: bool = True,
        runtime_adapter: RuntimeAdapter | None = None,
        architecture: dict[str, Any] | None = None,
        manifest: dict[str, Any] | None = None,
        behavior_suite: dict[str, Any] | None = None,
        behavior_probe_design_failure: str | None = None,
    ) -> list[dict[str, Any]]:
        static_checks = (
            runtime_adapter.validate_project(
                workspace,
                plan,
                syntax_checks=self.syntax_checks,
                architecture=architecture,
                manifest=manifest,
            )
            if runtime_adapter is not None
            else validate_project(workspace, plan, syntax_checks=self.syntax_checks)
        )
        checks = [
            *generation_failures,
            *static_checks,
        ]
        syntax = next((check for check in checks if check["name"] == "syntax"), None)
        if not checks_passed(checks) or syntax is None or syntax["status"] != "pass":
            checks.append(
                {
                    "name": "executable-validation",
                    "status": "skipped",
                    "message": "Blocked until generation and syntax checks pass",
                    "paths": [],
                }
            )
            return checks
        if runtime_adapter is not None:
            assert architecture is not None
            assert manifest is not None
            readiness_checks = runtime_adapter.validate_readiness(
                workspace,
                architecture,
                manifest,
            )
            checks.extend(readiness_checks)
            if self.behavior_executor is not None:
                if behavior_suite is None:
                    checks.append(
                        {
                            "name": "python-behavior-probes",
                            "status": "skipped",
                            "message": (
                                "Probe design unavailable: "
                                + (
                                    behavior_probe_design_failure
                                    or "no validated probe suite was produced"
                                )
                            ),
                            "paths": [],
                        }
                    )
                elif any(item["status"] == "fail" for item in readiness_checks):
                    checks.append(
                        {
                            "name": "python-behavior-probes",
                            "status": "skipped",
                            "message": "Blocked until executable readiness checks pass",
                            "paths": [],
                        }
                    )
                else:
                    try:
                        checks.extend(
                            self.behavior_executor.validate(
                                workspace.project_root,
                                architecture,
                                manifest,
                                behavior_suite,
                            )
                        )
                    except Exception as exc:
                        checks.append(
                            _failed_check(
                                "python-behavior-probes",
                                _safe_error(exc),
                                [],
                                stage="behavior-validation",
                            )
                        )
            return checks
        if not run_executable_validation:
            checks.append(
                {
                    "name": "executable-validation",
                    "status": "skipped",
                    "message": (
                        "Project-first execution awaits adapter-owned readiness gates; "
                        "generated project tests are deferred"
                    ),
                    "paths": [],
                }
            )
            return checks
        if self.validation_executor is None:
            checks.append(
                {
                    "name": "executable-validation",
                    "status": "skipped",
                    "message": "No executable validator is configured",
                    "paths": [],
                }
            )
            return checks

        try:
            result = self.validation_executor.validate(workspace.project_root, plan)
            planned = {item["path"] for item in plan["files"]}
            paths = sorted({validate_relative_path(path) for path in result.paths})
            if not set(paths).issubset(planned):
                raise WorkspaceError("Executable validation returned an unplanned path")
            checks.append(
                {
                    "name": "executable-validation",
                    "status": "pass",
                    "message": result.message,
                    "paths": paths,
                }
                if result.passed
                else _failed_check(
                    "executable-validation",
                    result.message,
                    paths,
                    stage="executable-validation",
                )
            )
        except Exception as exc:
            checks.append(
                _failed_check(
                    "executable-validation",
                    _safe_error(exc),
                    [],
                    stage="executable-validation",
                )
            )
        return checks

    def _validate_plan(
        self,
        plan: dict[str, Any],
        specification: str,
        *,
        project_first: bool = False,
    ) -> None:
        paths: list[str] = []
        orders: list[int] = []
        if len(plan["files"]) > MAX_FILES:
            raise WorkspaceError(f"Clean plan exceeds the {MAX_FILES}-file limit")
        for item in plan["files"]:
            canonical = validate_relative_path(item["path"])
            if canonical != item["path"]:
                raise WorkspaceError(f"Plan path is not canonical: {item['path']!r}")
            paths.append(canonical)
            orders.append(item["generation_order"])
        if len(paths) != len(set(paths)):
            raise WorkspaceError("Clean plan contains duplicate paths")
        if len(orders) != len(set(orders)):
            raise WorkspaceError("Clean plan contains duplicate generation orders")
        path_orders = {
            item["path"]: item["generation_order"] for item in plan["files"]
        }
        contracts = plan["symbol_contracts"]
        is_python = "python" in plan["runtime"]["language"].casefold()
        if is_python:
            for contract in contracts:
                declaration_error = python_contract_declaration_error(contract)
                if declaration_error:
                    raise WorkspaceError(
                        f"Invalid Python contract {contract['symbol_id']}: {declaration_error}"
                    )
        symbol_ids = [item["symbol_id"] for item in contracts]
        if len(symbol_ids) != len(set(symbol_ids)):
            raise WorkspaceError("Clean plan contains duplicate symbol IDs")
        qualified_names = [item["qualified_name"] for item in contracts]
        if len(qualified_names) != len(set(qualified_names)):
            raise WorkspaceError("Clean plan contains duplicate qualified symbol names")
        package_names = [item["import_name"] for item in plan["packages"]]
        if len(package_names) != len(set(package_names)):
            raise WorkspaceError("Clean plan contains duplicate package import names")

        declared_symbols = set(symbol_ids)
        providers: dict[str, list[str]] = {}
        for item in plan["files"]:
            path = item["path"]
            for field in ("provides", "requires", "depends_on"):
                values = item[field]
                if len(values) != len(set(values)):
                    raise WorkspaceError(f"Clean plan repeats {field} values for {path!r}")
            overlap = set(item["provides"]) & set(item["requires"])
            if overlap:
                raise WorkspaceError(
                    f"Clean plan file {path!r} both provides and requires: {', '.join(sorted(overlap))}"
                )
            undeclared = (set(item["provides"]) | set(item["requires"])) - declared_symbols
            if undeclared:
                raise WorkspaceError(
                    f"Clean plan file {path!r} references undeclared symbols: "
                    f"{', '.join(sorted(undeclared))}"
                )
            for symbol_id in item["provides"]:
                providers.setdefault(symbol_id, []).append(path)

        missing_providers = sorted(declared_symbols - set(providers))
        if missing_providers:
            raise WorkspaceError(
                f"Clean plan does not provide declared symbols: {', '.join(missing_providers)}"
            )
        duplicate_providers = {
            symbol_id: symbol_paths
            for symbol_id, symbol_paths in providers.items()
            if len(symbol_paths) > 1
        }
        if duplicate_providers:
            rendered = "; ".join(
                f"{symbol_id}: {', '.join(sorted(symbol_paths))}"
                for symbol_id, symbol_paths in sorted(duplicate_providers.items())
            )
            raise WorkspaceError(f"Clean plan provides symbols more than once: {rendered}")

        provider_by_symbol = {
            symbol_id: symbol_paths[0] for symbol_id, symbol_paths in providers.items()
        }
        contracts_by_id = {item["symbol_id"]: item for item in contracts}
        for item in plan["files"]:
            path = item["path"]
            dependencies = set(item["depends_on"])
            if path in dependencies:
                raise WorkspaceError(f"Clean plan file {path!r} depends on itself")
            missing_dependencies = dependencies - set(path_orders)
            if missing_dependencies:
                raise WorkspaceError(
                    f"Clean plan file {path!r} depends on unplanned files: "
                    f"{', '.join(sorted(missing_dependencies))}"
                )
            late_dependencies = sorted(
                dependency
                for dependency in dependencies
                if path_orders[dependency] >= item["generation_order"]
            )
            if late_dependencies:
                raise WorkspaceError(
                    f"Clean plan dependencies must be generated earlier than {path!r}: "
                    f"{', '.join(late_dependencies)}"
                )
            for symbol_id in item["requires"]:
                provider = provider_by_symbol[symbol_id]
                if provider not in dependencies:
                    raise WorkspaceError(
                        f"Clean plan file {path!r} requires {symbol_id} but does not "
                        f"depend on its provider {provider!r}"
                    )
            file_requirements = set(item["requirement_ids"])
            for symbol_id in item["provides"]:
                contract_requirements = set(contracts_by_id[symbol_id]["requirement_ids"])
                if not contract_requirements.issubset(file_requirements):
                    raise WorkspaceError(
                        f"Provider {path!r} does not allocate every requirement for {symbol_id}"
                    )
        allocated_requirements = {
            requirement_id
            for item in plan["files"]
            for requirement_id in item["requirement_ids"]
        }
        declared_requirements = set(
            re.findall(r"\b(?:FR|BR|EH|AC|TC)-\d{3,}\b", specification)
        )
        contract_requirements = {
            requirement_id
            for contract in contracts
            for requirement_id in contract["requirement_ids"]
        }
        unknown_contract_requirements = sorted(contract_requirements - declared_requirements)
        if unknown_contract_requirements:
            raise WorkspaceError(
                "Clean plan symbol contracts reference requirements absent from the "
                f"specification: {', '.join(unknown_contract_requirements)}"
            )
        allocatable_requirements = (
            {
                requirement_id
                for requirement_id in declared_requirements
                if not requirement_id.startswith("TC-")
            }
            if project_first
            else declared_requirements
        )
        omitted = sorted(allocatable_requirements - allocated_requirements)
        if omitted:
            raise WorkspaceError(
                f"Clean plan does not allocate specification requirements: {', '.join(omitted)}"
            )
        if not project_first:
            _validate_test_candidate_allocations(plan, specification)
        if self.validation_executor is not None and not project_first:
            adapter_issues = self.validation_executor.validate_plan(
                plan,
                declared_requirements,
            )
            if adapter_issues:
                raise WorkspaceError(
                    "Executable validation adapter rejected the plan: "
                    + "; ".join(adapter_issues)
                )
        planned = set(paths)
        for entry in plan["entry_points"]:
            path = validate_relative_path(entry["path"])
            if path not in planned:
                raise WorkspaceError(f"Entry point is not a planned file: {path!r}")

    @staticmethod
    def _build_report(
        *,
        handoff: CleanHandoff,
        plan: dict[str, Any],
        plan_hash: str,
        workspace: CleanWorkspace,
        checks: list[dict[str, Any]],
        repairs_used: int,
        repair_stop_reason: str | None,
        planning_mode: str,
        architecture: dict[str, Any] | None,
        manifest: dict[str, Any] | None,
        behavior_suite: dict[str, Any] | None,
        runtime_adapter_id: str | None,
    ) -> dict[str, Any]:
        validation_level = _validation_level(checks)
        # Static validation proves only shape. Success requires the configured runtime
        # gates, including the frozen behavior suite when executable checks are enabled.
        success = _build_succeeded(checks)
        generated = {
            path: workspace.content_hash(path) for path in workspace.list_generated_files()
        }
        requirements: dict[str, set[str]] = {}
        for item in plan["files"]:
            for requirement_id in item["requirement_ids"]:
                requirements.setdefault(requirement_id, set()).add(item["path"])
        failed_requirement_ids = {
            str(requirement_id)
            for check in checks
            if check["status"] == "fail"
            for diagnostic in check.get("diagnostics", [])
            for requirement_id in diagnostic.get("requirement_ids", [])
        }
        non_behavior_failures = [
            check
            for check in checks
            if check["status"] == "fail"
            and check["name"] not in {"python-behavior-probes", "python-behavior-coverage"}
        ]
        failed_project_paths = {
            str(path) for check in non_behavior_failures for path in check.get("paths", [])
        }
        has_global_failure = any(not check.get("paths") for check in non_behavior_failures)
        covered_requirement_ids = {
            str(requirement_id)
            for probe in (behavior_suite or {}).get("probes", [])
            for requirement_id in probe.get("requirement_ids", [])
        }
        requirement_status = [
            {
                "requirement_id": requirement_id,
                "status": _requirement_status(
                    paths,
                    generated=set(generated),
                    success=success,
                    requirement_id=requirement_id,
                    behavior_validation_configured=behavior_suite is not None,
                    covered_requirement_ids=covered_requirement_ids,
                    failed_requirement_ids=failed_requirement_ids,
                    failed_project_paths=failed_project_paths,
                    has_global_failure=has_global_failure,
                ),
                "paths": sorted(paths),
            }
            for requirement_id, paths in sorted(requirements.items())
        ]
        return {
            "schema_version": 4,
            "status": "success" if success else ("partial" if generated else "failed"),
            "compatibility_mode": handoff.compatibility_mode,
            "validation_level": validation_level,
            "handoff_sha256": handoff.specification_sha256,
            "plan_sha256": plan_hash,
            "generated_files": generated,
            "checks": checks,
            "repair_rounds": repairs_used,
            "repair_stop_reason": repair_stop_reason,
            "planning_mode": planning_mode,
            "architecture_sha256": (
                _json_hash(architecture) if architecture is not None else None
            ),
            "manifest_sha256": _json_hash(manifest) if manifest is not None else None,
            "behavior_probe_sha256": (
                _json_hash(behavior_suite) if behavior_suite is not None else None
            ),
            "runtime_adapter": runtime_adapter_id,
            "requirements": requirement_status,
            "unresolved_gaps": plan["open_questions"],
        }


def _repair_context(
    workspace: CleanWorkspace,
    plan: dict[str, Any],
    allowed_paths: set[str],
    *,
    max_bytes: int | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Return bounded, read-only context directly related to failed files."""
    tasks = {item["path"]: item for item in plan["files"]}
    candidates = {
        dependency
        for path in allowed_paths
        for dependency in tasks[path]["depends_on"]
    }
    candidates.update(
        path for path, task in tasks.items() if allowed_paths.intersection(task["depends_on"])
    )
    candidates.difference_update(allowed_paths)
    generated = set(workspace.list_generated_files())
    related: dict[str, str] = {}
    omitted: list[str] = []
    total_bytes = 0
    byte_limit = MAX_DEPENDENCY_CONTEXT_BYTES if max_bytes is None else max_bytes
    for path in sorted(candidates):
        if path not in generated:
            continue
        content = workspace.read_generated_file(path)
        size = len(content.encode("utf-8"))
        if total_bytes + size > byte_limit:
            omitted.append(path)
            continue
        related[path] = content
        total_bytes += size
    return related, omitted


def _repair_paths(
    checks: list[dict[str, Any]],
    plan: dict[str, Any],
) -> set[str]:
    """Select repairable paths while preserving tests as behavioral oracles."""
    planned = {item["path"] for item in plan["files"]}
    allowed = failed_paths(checks) & planned
    suite_failed = any(
        check["name"] == "executable-validation"
        and check["status"] == "fail"
        and str(check["message"]).startswith("pytest-suite failed")
        for check in checks
    )
    if not suite_failed:
        return allowed

    tasks = {item["path"]: item for item in plan["files"]}
    failed_tests = {path for path in allowed if is_python_test_path(path)}
    implementation_paths = {path for path in allowed if not is_python_test_path(path)}
    implementation_paths.update(
        dependency
        for test_path in failed_tests
        for dependency in tasks[test_path]["depends_on"]
        if dependency in planned and not is_python_test_path(dependency)
    )
    return implementation_paths


def _only_behavior_failures(checks: list[dict[str, Any]]) -> bool:
    failures = [check for check in checks if check["status"] == "fail"]
    return bool(failures) and all(
        check["name"] == "python-behavior-probes" for check in failures
    )


def _failing_behavior_probes(
    checks: list[dict[str, Any]],
    behavior_suite: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Select frozen probe sources named by current structured diagnostics."""
    if behavior_suite is None:
        return []
    failing_ids = {
        str(diagnostic.get("check_id", "")).rsplit(".", 1)[-1]
        for check in checks
        if check["status"] == "fail" and check["name"].endswith("behavior-probes")
        for diagnostic in check.get("diagnostics", [])
    }
    return [
        deepcopy(probe)
        for probe in behavior_suite.get("probes", [])
        if probe.get("probe_id") in failing_ids
    ]


def _compatibility_plan(
    architecture: dict[str, Any],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Adapt validated project-first artifacts to the current build-loop interface."""
    profile = architecture["project_profile"]
    architecture_entries = {
        item["entry_point_id"]: item for item in architecture["entry_points"]
    }
    entry_points = [
        {
            "path": item["path"],
            "description": architecture_entries[item["entry_point_id"]]["description"],
        }
        for item in manifest["entry_points"]
    ]
    package_names = sorted(
        {
            str(contract["qualified_name"]).split(".", 1)[0]
            for contract in architecture["contracts"]
            if "." in str(contract["qualified_name"])
        }
    )
    project_kind = profile["kind"]
    if project_kind not in {"library", "application", "mixed"}:
        project_kind = "application"
    component_scenarios: dict[str, set[str]] = {}
    for capability in architecture["capabilities"]:
        for component_id in capability["component_ids"]:
            component_scenarios.setdefault(component_id, set()).update(
                capability["scenario_ids"]
            )
    files = []
    for item in manifest["files"]:
        task = deepcopy(item)
        task["scenario_ids"] = (
            sorted(component_scenarios.get(item["component_id"], set()))
            if item["category"] in {"source", "configuration"}
            else []
        )
        files.append(task)
    return {
        "schema_version": 2,
        "summary": " ".join(
            capability["purpose"] for capability in architecture["capabilities"]
        ),
        "project_kind": project_kind,
        "runtime": {
            "language": profile["language"],
            "minimum_version": profile["runtime_version"],
        },
        "dependencies": [
            {
                "name": item["name"],
                "purpose": item["purpose"],
                "required": item["required"],
                **({"scope": item["scope"]} if "scope" in item else {}),
            }
            for item in architecture["dependency_decisions"]
        ],
        "packages": [
            {
                "import_name": name,
                "purpose": f"Import root for {name} public contracts.",
            }
            for name in package_names
        ],
        "entry_points": entry_points,
        "symbol_contracts": [
            {
                "symbol_id": item["contract_id"],
                "qualified_name": item["qualified_name"],
                "kind": item["kind"],
                "signature": item["declaration"],
                **({"behavior_rules": deepcopy(item["behavior_rules"])} if "behavior_rules" in item else {}),
                "visibility": item["visibility"],
                "requirement_ids": list(item["requirement_ids"]),
            }
            for item in architecture["contracts"]
        ],
        "files": files,
        "validation_strategy": [
            item["description"] for item in manifest["readiness_obligations"]
        ],
        "open_questions": list(architecture["unresolved_gaps"]),
    }


def _append_adapter_generated_tests(
    plan: dict[str, Any],
    generated_files: list[dict[str, Any]],
) -> None:
    """Append deterministic runtime tests after all model-owned project files."""
    if not generated_files:
        return
    existing_paths = [item["path"] for item in plan["files"]]
    existing = set(existing_paths)
    next_order = max(
        (item["generation_order"] for item in plan["files"]), default=0
    ) + 1
    for generated in generated_files:
        path = validate_relative_path(str(generated["path"]))
        if path in existing:
            raise WorkspaceError(
                f"Adapter-generated test collides with planned path {path!r}"
            )
        requirement_ids = list(dict.fromkeys(generated["requirement_ids"]))
        plan["files"].append(
            {
                "path": path,
                "purpose": (
                    "Execute validated clean-room behavior probes as project tests."
                ),
                "requirement_ids": requirement_ids,
                "provides": [],
                "requires": [],
                "depends_on": list(existing_paths),
                "generation_order": next_order,
                "scenario_ids": list(
                    dict.fromkeys(generated.get("scenario_ids", []))
                ),
            }
        )
        existing.add(path)
        existing_paths.append(path)
        next_order += 1


def _normalise_plan(
    plan: dict[str, Any],
    *,
    specification: str | None = None,
    ensure_python_test_coverage: bool = False,
) -> dict[str, Any]:
    """Canonicalize safe planner formatting and derive dependency-first order."""
    normalized = deepcopy(plan)
    _add_python_package_scaffold(normalized)
    original_to_normalized: dict[str, str] = {}
    for item in normalized["files"]:
        original = item["path"]
        path = _normalise_plan_path(original)
        original_to_normalized[original] = path
        item["path"] = path

    for item in normalized["files"]:
        item["depends_on"] = [
            original_to_normalized.get(dependency, _normalise_plan_path(dependency))
            for dependency in item["depends_on"]
        ]
    for entry in normalized["entry_points"]:
        original = entry["path"]
        entry["path"] = original_to_normalized.get(
            original,
            _normalise_plan_path(original),
        )

    tasks = {item["path"]: item for item in normalized["files"]}
    if len(tasks) != len(normalized["files"]):
        raise WorkspaceError("Clean plan contains duplicate paths after normalization")
    _infer_symbol_dependencies(tasks)
    _inherit_test_requirements(normalized, tasks)
    if specification is not None:
        if ensure_python_test_coverage:
            _add_supplemental_python_tests(normalized, tasks, specification)
        _allocate_test_candidates(tasks, specification)
    planned_paths = set(tasks)
    for path, task in tasks.items():
        missing = set(task["depends_on"]) - planned_paths
        if missing:
            raise WorkspaceError(
                f"Clean plan file {path!r} depends on unplanned files: "
                f"{', '.join(sorted(missing))}"
            )
    is_python = "python" in str(
        normalized.get("runtime", {}).get("language", "")
    ).casefold()
    if is_python:
        for path, task in tasks.items():
            if is_python_test_path(path):
                continue
            test_dependencies = sorted(
                dependency
                for dependency in task["depends_on"]
                if is_python_test_path(dependency)
            )
            if test_dependencies:
                raise WorkspaceError(
                    f"Clean implementation file {path!r} depends on test files: "
                    + ", ".join(test_dependencies)
                )

    original_orders = {
        item["path"]: item["generation_order"] for item in normalized["files"]
    }
    remaining_dependencies = {
        path: set(task["depends_on"]) for path, task in tasks.items()
    }
    ordered_paths: list[str] = []
    while remaining_dependencies:
        ready = [
            path
            for path, dependencies in remaining_dependencies.items()
            if not dependencies
        ]
        if not ready:
            cycle_paths = ", ".join(sorted(remaining_dependencies))
            raise WorkspaceError(f"Clean plan contains a dependency cycle: {cycle_paths}")
        path = min(
            ready,
            key=lambda candidate: (
                is_python and is_python_test_path(candidate),
                original_orders[candidate],
                candidate,
            ),
        )
        ordered_paths.append(path)
        del remaining_dependencies[path]
        for dependencies in remaining_dependencies.values():
            dependencies.discard(path)

    for generation_order, path in enumerate(ordered_paths, start=1):
        tasks[path]["generation_order"] = generation_order
    return normalized


def _infer_symbol_dependencies(tasks: dict[str, dict[str, Any]]) -> None:
    """Derive unambiguous file dependencies from declared symbol requirements."""
    providers: dict[str, list[str]] = {}
    for path, task in tasks.items():
        for symbol_id in task["provides"]:
            providers.setdefault(symbol_id, []).append(path)
    for path, task in tasks.items():
        inferred = {
            symbol_paths[0]
            for symbol_id in task["requires"]
            if len(symbol_paths := providers.get(symbol_id, [])) == 1
            and symbol_paths[0] != path
        }
        task["depends_on"] = [
            *task["depends_on"],
            *sorted(inferred - set(task["depends_on"])),
        ]


def _inherit_test_requirements(
    plan: dict[str, Any],
    tasks: dict[str, dict[str, Any]],
) -> None:
    """Make test coverage explicit from its declared implementation dependencies."""
    if "python" not in str(plan.get("runtime", {}).get("language", "")).casefold():
        return
    for task in tasks.values():
        if not is_python_test_path(task["path"]):
            continue
        inherited = {
            requirement_id
            for dependency in task["depends_on"]
            if dependency in tasks and not is_python_test_path(dependency)
            for requirement_id in tasks[dependency]["requirement_ids"]
            if not requirement_id.startswith("TC-")
        }
        task["requirement_ids"] = sorted(set(task["requirement_ids"]) | inherited)


def _add_supplemental_python_tests(
    plan: dict[str, Any],
    tasks: dict[str, dict[str, Any]],
    specification: str,
) -> None:
    """Plan tests for implementation behavior left uncovered by the model plan."""
    if "python" not in str(plan.get("runtime", {}).get("language", "")).casefold():
        return
    statements = requirement_statements(specification)
    behavior_ids = {
        requirement_id
        for requirement_id in statements
        if not requirement_id.startswith("TC-")
    }
    covered = {
        requirement_id
        for task in tasks.values()
        if is_python_test_path(task["path"])
        for dependency in task["depends_on"]
        if dependency in tasks and not is_python_test_path(dependency)
        for requirement_id in (
            set(task["requirement_ids"])
            & set(tasks[dependency]["requirement_ids"])
        )
    }
    contract_requirements = {
        contract["symbol_id"]: set(contract["requirement_ids"])
        for contract in plan["symbol_contracts"]
    }
    allocations: dict[str, set[str]] = {}
    for requirement_id in sorted(behavior_ids - covered):
        candidates = [
            task
            for task in tasks.values()
            if not is_python_test_path(task["path"])
            and task["path"].casefold().endswith(".py")
            and requirement_id in task["requirement_ids"]
        ]
        if not candidates:
            continue
        provider = min(
            candidates,
            key=lambda task: (
                not any(
                    requirement_id in contract_requirements.get(symbol_id, set())
                    for symbol_id in task["provides"]
                ),
                not bool(task["provides"]),
                task["path"].rsplit("/", 1)[-1] == "__init__.py",
                task["generation_order"],
                task["path"],
            ),
        )
        allocations.setdefault(provider["path"], set()).add(requirement_id)

    next_order = max(
        (item["generation_order"] for item in plan["files"]),
        default=0,
    ) + 1
    for path in sorted(
        allocations,
        key=lambda item: (tasks[item]["generation_order"], item),
    ):
        implementation = tasks[path]
        uncovered = sorted(allocations[path])
        if len(plan["files"]) >= MAX_FILES:
            raise WorkspaceError(
                "Clean plan cannot add required Python coverage without exceeding "
                f"the {MAX_FILES}-file workspace limit"
            )
        test_path = _supplemental_test_path(path, set(tasks))
        statement_summary = " ".join(statements[item] for item in uncovered)
        relevant_symbols = [
            symbol_id
            for symbol_id in implementation["provides"]
            if contract_requirements.get(symbol_id, set()).intersection(uncovered)
        ]
        test_task = {
            "path": test_path,
            "purpose": (
                f"Verify {', '.join(uncovered)} implemented by {path}. "
                f"Required behavior: {statement_summary}"
            ),
            "requirement_ids": uncovered,
            "provides": [],
            "requires": relevant_symbols or list(implementation["provides"]),
            "depends_on": [path],
            "generation_order": next_order,
        }
        plan["files"].append(test_task)
        tasks[test_path] = test_task
        next_order += 1


def _supplemental_test_path(source_path: str, planned_paths: set[str]) -> str:
    source_stem = source_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    safe_stem = re.sub(r"[^A-Za-z0-9_]+", "_", source_stem).strip("_") or "module"
    base = f"tests/test_{safe_stem}_requirements"
    candidate = f"{base}.py"
    suffix = 2
    while candidate in planned_paths:
        candidate = f"{base}_{suffix}.py"
        suffix += 1
    return candidate


def _allocate_test_candidates(
    tasks: dict[str, dict[str, Any]],
    specification: str,
) -> None:
    """Recover uniquely implied TC allocations omitted by the language model.

    The allocator is deliberately conservative: a candidate and a test must be each
    other's unique best semantic match. Existing allocations are authoritative and
    reserve both the candidate and the test. Validation reports anything that cannot
    be recovered without guessing.
    """
    statements = requirement_statements(specification)
    candidate_ids = {
        requirement_id
        for requirement_id in statements
        if requirement_id.startswith("TC-")
    }
    test_tasks = {
        path: task for path, task in tasks.items() if is_python_test_path(path)
    }
    assigned_candidates = {
        requirement_id
        for task in test_tasks.values()
        for requirement_id in task["requirement_ids"]
        if requirement_id.startswith("TC-")
    }
    available_tests = {
        path
        for path, task in test_tasks.items()
        if not any(
            requirement_id.startswith("TC-")
            for requirement_id in task["requirement_ids"]
        )
    }
    unassigned_candidates = candidate_ids - assigned_candidates

    scores = {
        (requirement_id, path): len(
            _semantic_tokens(statements[requirement_id])
            & _semantic_tokens(_test_semantic_text(test_tasks[path], tasks))
        )
        for requirement_id in unassigned_candidates
        for path in available_tests
    }
    while unassigned_candidates and available_tests:
        candidate_choices: dict[str, tuple[str, int]] = {}
        for requirement_id in sorted(unassigned_candidates):
            ranked = sorted(
                (
                    (scores[(requirement_id, path)], path)
                    for path in available_tests
                    if scores[(requirement_id, path)] > 0
                ),
                reverse=True,
            )
            if ranked and (len(ranked) == 1 or ranked[0][0] > ranked[1][0]):
                candidate_choices[requirement_id] = (ranked[0][1], ranked[0][0])

        test_choices: dict[str, list[tuple[int, str]]] = {}
        for requirement_id, (path, score) in candidate_choices.items():
            test_choices.setdefault(path, []).append((score, requirement_id))

        allocations: list[tuple[str, str]] = []
        for path, proposals in test_choices.items():
            proposals.sort(reverse=True)
            if len(proposals) == 1 or proposals[0][0] > proposals[1][0]:
                allocations.append((proposals[0][1], path))
        if not allocations:
            break
        for requirement_id, path in allocations:
            task = test_tasks[path]
            task["requirement_ids"] = sorted(
                set(task["requirement_ids"]) | {requirement_id}
            )
            unassigned_candidates.remove(requirement_id)
            available_tests.remove(path)


def _add_python_package_scaffold(plan: dict[str, Any]) -> None:
    """Ensure a declared Python package has install and usage metadata."""
    if "python" not in str(plan.get("runtime", {}).get("language", "")).casefold():
        return
    if not plan.get("packages"):
        return
    paths = {str(item["path"]).casefold() for item in plan["files"]}
    next_order = max((item["generation_order"] for item in plan["files"]), default=0) + 1
    additions: list[dict[str, Any]] = []
    if "pyproject.toml" not in paths:
        additions.append(
            {
                "path": "pyproject.toml",
                "purpose": (
                    "Define installable Python project metadata, supported runtime, "
                    "package discovery, and declared dependencies."
                ),
                "requirement_ids": [],
                "provides": [],
                "requires": [],
                "depends_on": [],
                "generation_order": next_order,
            }
        )
        next_order += 1
    if not any(path.rsplit("/", 1)[-1] in {"readme.md", "readme.rst"} for path in paths):
        additions.append(
            {
                "path": "README.md",
                "purpose": (
                    "Document installation, public capabilities, supported runtime, "
                    "and executable usage derived from the approved plan."
                ),
                "requirement_ids": [],
                "provides": [],
                "requires": [],
                "depends_on": [],
                "generation_order": next_order,
            }
        )
    plan["files"].extend(additions)


_SEMANTIC_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "as",
        "be",
        "candidate",
        "code",
        "evidence",
        "expected",
        "for",
        "from",
        "given",
        "is",
        "it",
        "of",
        "operation",
        "or",
        "project",
        "requirement",
        "result",
        "scenario",
        "test",
        "tests",
        "that",
        "the",
        "then",
        "to",
        "unit",
        "valid",
        "verify",
        "verifies",
        "when",
        "with",
    }
)


def _validate_test_candidate_allocations(
    plan: dict[str, Any],
    specification: str,
) -> None:
    """Reject ID-only coverage unrelated to the stated test-candidate behavior."""
    statements = requirement_statements(specification)
    tasks = {item["path"]: item for item in plan["files"]}
    test_tasks = [item for item in plan["files"] if is_python_test_path(item["path"])]
    candidate_ids = sorted(
        requirement_id
        for requirement_id in statements
        if requirement_id.startswith("TC-")
    )
    for task in test_tasks:
        assigned = sorted(
            requirement_id
            for requirement_id in task["requirement_ids"]
            if requirement_id.startswith("TC-")
        )
        if len(assigned) > 1:
            raise WorkspaceError(
                f"Clean plan test {task['path']!r} combines distinct test candidates: "
                + ", ".join(assigned)
            )
    for requirement_id in candidate_ids:
        assigned = [
            task for task in test_tasks if requirement_id in task["requirement_ids"]
        ]
        if len(assigned) != 1:
            raise WorkspaceError(
                f"Clean plan must allocate {requirement_id} to exactly one test file"
            )
        task = assigned[0]
        related_text = _test_semantic_text(task, tasks)
        expected_tokens = _semantic_tokens(statements[requirement_id])
        related_tokens = _semantic_tokens(related_text)
        if expected_tokens and not expected_tokens.intersection(related_tokens):
            raise WorkspaceError(
                f"Clean plan allocation for {requirement_id} does not correspond to "
                f"its stated behavior in {task['path']!r}"
            )


def _test_semantic_text(
    task: dict[str, Any],
    tasks: dict[str, dict[str, Any]],
) -> str:
    return " ".join(
        [task["path"], task["purpose"]]
        + [
            f"{dependency} {tasks[dependency]['purpose']}"
            for dependency in task["depends_on"]
            if dependency in tasks and not is_python_test_path(dependency)
        ]
    )


def _semantic_tokens(value: str) -> set[str]:
    tokens = set(re.findall(r"[a-z][a-z0-9]+", value.casefold()))
    normalized = {
        token[:-1] if token.endswith("s") and len(token) > 4 else token
        for token in tokens
        if token not in _SEMANTIC_STOP_WORDS
    }
    return normalized - _SEMANTIC_STOP_WORDS


def _normalise_plan_path(path: str) -> str:
    normalized = re.sub(r"\*\*([A-Za-z][A-Za-z0-9_]*)\*\*", r"__\1__", path)
    return re.sub(r"(?<=\w)\\_(?=\w)", "_", normalized)


def _json_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _safe_error(exc: Exception) -> str:
    """Keep actionable Clean diagnostics while withholding host filesystem paths."""
    message = str(exc).strip()
    if not message:
        return f"{type(exc).__name__}: Clean operation failed"
    message = re.sub(
        r"(?i)(?:[a-z]:[\\/]|\\\\)[^\r\n\"'<>]*",
        "<host-path>",
        message,
    )
    message = re.sub(r"[\r\n\t]+", " ", message)
    message = re.sub(r"\s{2,}", " ", message).strip()
    if len(message) > 2000:
        message = message[:1997].rstrip() + "..."
    return f"{type(exc).__name__}: {message}"


def _failed_check(
    name: str,
    message: str,
    paths: list[str],
    *,
    stage: str,
) -> dict[str, Any]:
    diagnostic = CleanDiagnostic(
        stage=stage,
        check_id=f"clean.{name}",
        message=message,
        paths=tuple(paths),
    )
    return {
        "name": name,
        "status": "fail",
        "message": message,
        "paths": paths,
        "diagnostics": [diagnostic.to_dict()],
    }


def _requirement_status(
    paths: set[str],
    *,
    generated: set[str],
    success: bool,
    requirement_id: str | None = None,
    behavior_validation_configured: bool = False,
    covered_requirement_ids: set[str] | None = None,
    failed_requirement_ids: set[str] | None = None,
    failed_project_paths: set[str] | None = None,
    has_global_failure: bool = False,
) -> str:
    if not paths.issubset(generated):
        return "partial" if paths.intersection(generated) else "blocked"
    if success:
        return "satisfied"
    if behavior_validation_configured:
        if (
            requirement_id in (failed_requirement_ids or set())
            or requirement_id not in (covered_requirement_ids or set())
            or paths.intersection(failed_project_paths or set())
            or has_global_failure
        ):
            return "partial"
        return "satisfied"
    return "partial"


def _validation_level(checks: list[dict[str, Any]]) -> str:
    """Return the highest deterministic validation level actually completed."""
    structural = {
        "planned-files-present",
        "no-unplanned-files",
        "entry-points-present",
        "utf8-text",
    }
    by_name = {check["name"]: check["status"] for check in checks}
    if any(by_name.get(name) != "pass" for name in structural):
        return "none"
    if by_name.get("syntax") == "pass":
        if by_name.get("executable-validation") == "pass":
            return "executable"
        return "syntax"
    return "structure"


def _build_succeeded(checks: list[dict[str, Any]]) -> bool:
    """Require executable readiness and completed probes when probes were configured."""
    behavior_validation_complete = not any(
        check["name"] in {"python-behavior-probes", "python-behavior-coverage"}
        and check["status"] != "pass"
        for check in checks
    )
    return (
        checks_passed(checks)
        and _validation_level(checks) == "executable"
        and behavior_validation_complete
    )

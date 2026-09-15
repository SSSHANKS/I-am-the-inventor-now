from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packages.agents.clean_team import CleanBuilderAgent, CleanPlannerAgent, CleanRepairAgent
from packages.modules.clean.context import build_scoped_context
from packages.modules.clean.execution import ValidationExecutor
from packages.modules.clean.python_contracts import python_contract_declaration_error
from packages.modules.clean.requirements import prepare_specification
from packages.modules.clean.validation import checks_passed, failed_paths, validate_project
from packages.modules.clean.workspace import (
    MAX_FILES,
    CleanWorkspace,
    WorkspaceError,
    validate_relative_path,
)
from packages.modules.handoff import CleanHandoff, load_clean_handoff
from packages.modules.supervising.schemas import CleanBuildReportSchema, CleanPlanSchema


class CleanBuildError(Exception):
    """Clean could not safely plan or construct the requested project."""


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
        planner: CleanPlannerAgent,
        builder: CleanBuilderAgent,
        repairer: CleanRepairAgent,
        *,
        max_repairs: int = 2,
        syntax_checks: bool = True,
        validation_executor: ValidationExecutor | None = None,
    ) -> None:
        if max_repairs < 0:
            raise ValueError("max_repairs cannot be negative")
        for agent in (planner, builder, repairer):
            if agent.source_reader is not None or agent.alias_map is not None:
                raise CleanBuildError("Clean agents cannot receive a source reader or alias map")
            if agent.artifact_verifier is not None:
                raise CleanBuildError("Clean agents cannot receive a source artifact verifier")
        self.planner = planner
        self.builder = builder
        self.repairer = repairer
        self.max_repairs = max_repairs
        self.syntax_checks = syntax_checks
        self.validation_executor = validation_executor

    def run(self, handoff_dir: str | Path, output_root: str | Path) -> CleanBuildResult:
        handoff = load_clean_handoff(handoff_dir)
        CleanWorkspace.require_available(output_root)
        try:
            prepared_specification = prepare_specification(handoff.specification)
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
            plan = _normalise_plan(plan)
            self._validate_plan(plan, prepared_specification.text)
        except Exception as exc:
            raise CleanBuildError(f"Clean planning failed: {exc}") from exc

        workspace = CleanWorkspace(output_root)
        workspace.write_metadata_json(
            "requirements.json", prepared_specification.audit_record()
        )
        workspace.write_metadata_json("clean_plan.json", plan)
        plan_hash = _json_hash(plan)
        generation_failures: list[dict[str, Any]] = []
        for task in sorted(plan["files"], key=lambda item: item["generation_order"]):
            path = task["path"]
            try:
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
                workspace.write_generated_files(batch["files"], allowed_paths={path})
            except Exception as exc:
                generation_failures.append(
                    {
                        "name": "generation",
                        "status": "fail",
                        "message": _safe_error(exc),
                        "paths": [path],
                    }
                )

        checks = self._validate(workspace, plan, generation_failures)
        repairs_used = 0
        while not checks_passed(checks) and repairs_used < self.max_repairs:
            allowed = failed_paths(checks) & {item["path"] for item in plan["files"]}
            if not allowed:
                break
            repairs_used += 1
            failures = [check for check in checks if check["status"] == "fail"]
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
                    related_files=related,
                    omitted_related_paths=omitted_related,
                )
                replacements = repair["replacements"]
                paths = [item["path"] for item in replacements]
                if len(paths) != len(set(paths)) or not paths:
                    raise WorkspaceError("Repair must return unique replacement paths")
                if not set(paths).issubset(allowed):
                    raise WorkspaceError("Repair attempted to modify a path outside its failure task")
                workspace.write_generated_files(replacements, allowed_paths=allowed)
                workspace.write_metadata_json(
                    f"iterations/repair-{repairs_used}.json",
                    {
                        "allowed_paths": sorted(allowed),
                        "related_paths": sorted(related),
                        "omitted_related_paths": omitted_related,
                        "replaced_paths": sorted(paths),
                        "rationale": repair["rationale"],
                        **scoped_context.audit_record(),
                    },
                )
                generation_failures = [
                    check
                    for check in generation_failures
                    if not set(check["paths"]).intersection(paths)
                ]
            except Exception as exc:
                generation_failures.append(
                    {
                        "name": "repair",
                        "status": "fail",
                        "message": _safe_error(exc),
                        "paths": sorted(allowed),
                    }
                )
            checks = self._validate(workspace, plan, generation_failures)

        report = self._build_report(
            handoff=handoff,
            plan=plan,
            plan_hash=plan_hash,
            workspace=workspace,
            checks=checks,
            repairs_used=repairs_used,
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

    def _validate(
        self,
        workspace: CleanWorkspace,
        plan: dict[str, Any],
        generation_failures: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        checks = [
            *generation_failures,
            *validate_project(workspace, plan, syntax_checks=self.syntax_checks),
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
                    "status": "pass" if result.passed else "fail",
                    "message": result.message,
                    "paths": paths,
                }
            )
        except Exception as exc:
            checks.append(
                {
                    "name": "executable-validation",
                    "status": "fail",
                    "message": _safe_error(exc),
                    "paths": [],
                }
            )
        return checks

    def _validate_plan(self, plan: dict[str, Any], specification: str) -> None:
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
        omitted = sorted(declared_requirements - allocated_requirements)
        if omitted:
            raise WorkspaceError(
                f"Clean plan does not allocate specification requirements: {', '.join(omitted)}"
            )
        if self.validation_executor is not None:
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
    ) -> dict[str, Any]:
        static_checks_passed = checks_passed(checks)
        validation_level = _validation_level(checks)
        # Static validation proves that generated text has the expected shape, not that
        # the reconstructed project works. Executable validation will promote a future
        # report to success after import, build, and test gates pass.
        success = static_checks_passed and validation_level == "executable"
        generated = {
            path: workspace.content_hash(path) for path in workspace.list_generated_files()
        }
        requirements: dict[str, set[str]] = {}
        for item in plan["files"]:
            for requirement_id in item["requirement_ids"]:
                requirements.setdefault(requirement_id, set()).add(item["path"])
        requirement_status = [
            {
                "requirement_id": requirement_id,
                "status": _requirement_status(
                    paths,
                    generated=set(generated),
                    success=success,
                ),
                "paths": sorted(paths),
            }
            for requirement_id, paths in sorted(requirements.items())
        ]
        return {
            "schema_version": 3,
            "status": "success" if success else ("partial" if generated else "failed"),
            "compatibility_mode": handoff.compatibility_mode,
            "validation_level": validation_level,
            "handoff_sha256": handoff.specification_sha256,
            "plan_sha256": plan_hash,
            "generated_files": generated,
            "checks": checks,
            "repair_rounds": repairs_used,
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


def _normalise_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Canonicalize safe planner formatting and derive dependency-first order."""
    normalized = deepcopy(plan)
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
    planned_paths = set(tasks)
    for path, task in tasks.items():
        missing = set(task["depends_on"]) - planned_paths
        if missing:
            raise WorkspaceError(
                f"Clean plan file {path!r} depends on unplanned files: "
                f"{', '.join(sorted(missing))}"
            )

    original_orders = {
        item["path"]: item["generation_order"] for item in normalized["files"]
    }
    remaining_dependencies = {
        path: set(task["depends_on"]) for path, task in tasks.items()
    }
    ordered_paths: list[str] = []
    while remaining_dependencies:
        ready = sorted(
            (
                path
                for path, dependencies in remaining_dependencies.items()
                if not dependencies
            ),
            key=lambda path: (original_orders[path], path),
        )
        if not ready:
            cycle_paths = ", ".join(sorted(remaining_dependencies))
            raise WorkspaceError(f"Clean plan contains a dependency cycle: {cycle_paths}")
        for path in ready:
            ordered_paths.append(path)
            del remaining_dependencies[path]
        completed = set(ready)
        for dependencies in remaining_dependencies.values():
            dependencies.difference_update(completed)

    for generation_order, path in enumerate(ordered_paths, start=1):
        tasks[path]["generation_order"] = generation_order
    return normalized


def _normalise_plan_path(path: str) -> str:
    normalized = re.sub(r"\*\*([A-Za-z][A-Za-z0-9_]*)\*\*", r"__\1__", path)
    return re.sub(r"(?<=\w)\\_(?=\w)", "_", normalized)


def _json_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _safe_error(exc: Exception) -> str:
    # Exception text can include provider config or host filesystem paths. The type is
    # enough to classify the Clean-side failure without feeding host data to repair.
    return f"{type(exc).__name__}: Clean operation failed"


def _requirement_status(
    paths: set[str],
    *,
    generated: set[str],
    success: bool,
) -> str:
    if success:
        return "satisfied"
    if paths.intersection(generated):
        return "partial"
    return "blocked"


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

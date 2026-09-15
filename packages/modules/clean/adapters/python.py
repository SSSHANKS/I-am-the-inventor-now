from __future__ import annotations

import re
from copy import deepcopy
from pathlib import PurePosixPath
from typing import Any, ClassVar

from packages.modules.clean.adapters.base import ReadinessExecutor, RuntimeAdapterError
from packages.modules.clean.adapters.generic import _validate_declared_operations
from packages.modules.clean.adapters.python_metadata import normalise_python_metadata
from packages.modules.clean.adapters.python_packaging import (
    python_manifest_readiness_issues,
    validate_python_packaging,
)
from packages.modules.clean.manifest import normalise_manifest
from packages.modules.clean.python_contract_paths import python_contract_module
from packages.modules.clean.validation import validate_project
from packages.modules.clean.workspace import CleanWorkspace


class PythonRuntimeAdapter:
    """Static Python policy used by the project-first reconstruction pipeline."""

    adapter_id = "python-static"
    policy_version = 1
    _base_validators = frozenset(
        {
            "manifest-integrity",
            "python-contracts",
            "python-dependencies",
            "python-entry-points",
            "python-package-layout",
            "python-packaging",
            "python-symbol-coherence",
            "utf8-text",
        }
    )
    _validator_aliases: ClassVar[dict[str, str]] = {
        "build": "python-packaging",
        "contracts": "python-contracts",
        "dependencies": "python-dependencies",
        "entry-point": "python-entry-points",
        "entry-points": "python-entry-points",
        "layout": "python-package-layout",
        "manifest": "manifest-integrity",
        "packaging": "python-packaging",
        "symbols": "python-symbol-coherence",
    }

    def __init__(self, readiness_executor: ReadinessExecutor | None = None) -> None:
        self.readiness_executor = readiness_executor

    def planning_policy(self, *, syntax_checks: bool) -> dict[str, Any]:
        validators = set(self._base_validators)
        if syntax_checks:
            validators.add("syntax")
        return {
            "adapter": self.adapter_id,
            "policy_version": self.policy_version,
            "supported_languages": ["Python"],
            "readiness_ceiling": "syntax" if syntax_checks else "structure",
            "available_checks": sorted(validators),
            "adapter_file_generation": False,
        }

    def normalise_manifest(
        self,
        architecture: dict[str, Any],
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        """Align unambiguous Python provider paths with authoritative module names."""
        normalised = (
            normalise_manifest(manifest, architecture)
            if architecture.get("components")
            else deepcopy(manifest)
        )
        for task in normalised["files"]:
            for field in ("local_validators", "integration_checks"):
                task[field] = list(
                    dict.fromkeys(
                        self._validator_aliases.get(item, item)
                        for item in task.get(field, [])
                    )
                )
        contracts = {
            item["contract_id"]: python_contract_module(
                item, architecture["contracts"]
            )
            for item in architecture["contracts"]
        }
        modules = set(contracts.values())
        layout = str(architecture["project_profile"]["layout"]).strip().casefold()
        groups: dict[str, list[dict[str, Any]]] = {}
        for task in normalised["files"]:
            if task["category"] != "source" or not task["provides"]:
                continue
            provided_modules = {contracts[item] for item in task["provides"]}
            if len(provided_modules) != 1:
                continue
            desired = _python_module_path(
                next(iter(provided_modules)), modules=modules, layout=layout
            )
            groups.setdefault(desired, []).append(task)

        renames: dict[str, str] = {}
        removed_ids: set[int] = set()
        for desired, group in groups.items():
            foreign_owner = next(
                (
                    item
                    for item in normalised["files"]
                    if item["path"] == desired and id(item) not in {id(task) for task in group}
                ),
                None,
            )
            if foreign_owner is not None:
                continue
            if len({task.get("component_id") for task in group}) != 1:
                continue
            primary = min(group, key=lambda item: item.get("generation_order", 0))
            old_paths = {task["path"] for task in group}
            for key in (
                "requirement_ids",
                "provides",
                "requires",
                "local_validators",
                "integration_checks",
            ):
                primary[key] = list(
                    dict.fromkeys(value for task in group for value in task.get(key, []))
                )
            primary["requires"] = [
                item for item in primary["requires"] if item not in primary["provides"]
            ]
            dependencies = [
                value
                for task in group
                for value in task.get("depends_on", [])
                if value not in old_paths and value != desired
            ]
            primary["depends_on"] = list(dict.fromkeys(dependencies))
            primary["purpose"] = "; ".join(
                dict.fromkeys(
                    str(task.get("purpose", "")).strip()
                    for task in group
                    if str(task.get("purpose", "")).strip()
                )
            )
            for task in group:
                renames[task["path"]] = desired
                if task is not primary:
                    removed_ids.add(id(task))
            primary["path"] = desired
        if removed_ids:
            normalised["files"] = [
                task for task in normalised["files"] if id(task) not in removed_ids
            ]
        renames.update(_package_surface_renames(normalised, layout=layout))
        if renames:
            _apply_manifest_path_renames(normalised, renames)
        return normalised

    def normalise_generated_content(
        self,
        path: str,
        content: str,
        architecture: dict[str, Any],
        manifest: dict[str, Any],
    ) -> str:
        """Remove a mistaken source-root prefix from unambiguous internal imports."""
        if path == "pyproject.toml":
            return normalise_python_metadata(content, architecture, manifest)
        if not path.endswith(".py"):
            return content
        layout = str(architecture["project_profile"]["layout"]).strip().casefold()
        if layout != "src":
            return content
        modules = {
            python_contract_module(item, architecture["contracts"])
            for item in architecture["contracts"]
        }

        def replace(match: re.Match[str]) -> str:
            module = match.group("module")
            if module not in modules and not any(
                item.startswith(f"{module}.") for item in modules
            ):
                return match.group(0)
            return f"{match.group('prefix')}{module}{match.group('suffix')}"

        return re.sub(
            r"^(?P<prefix>[ \t]*from[ \t]+)src\.(?P<module>[A-Za-z_]"
            r"[A-Za-z0-9_.]*)(?P<suffix>[ \t]+import\b)",
            replace,
            content,
            flags=re.MULTILINE,
        )

    def validate_manifest(
        self,
        architecture: dict[str, Any],
        manifest: dict[str, Any],
        *,
        syntax_checks: bool,
    ) -> None:
        policy = self.planning_policy(syntax_checks=syntax_checks)
        _validate_declared_operations(
            manifest,
            available_checks=frozenset(policy["available_checks"]),
            adapter_file_generation=policy["adapter_file_generation"],
            adapter_id=self.adapter_id,
        )
        language = str(architecture["project_profile"]["language"])
        if "python" not in language.casefold():
            raise RuntimeAdapterError(
                f"Python runtime adapter cannot validate language {language!r}"
            )
        profile = architecture["project_profile"]
        if {"layout", "build_system"}.issubset(profile):
            issues = python_manifest_readiness_issues(architecture, manifest)
            if issues:
                raise RuntimeAdapterError(
                    "Python manifest is inconsistent with its architecture: "
                    + "; ".join(issues)
                )

    def validate_project(
        self,
        workspace: CleanWorkspace,
        plan: dict[str, Any],
        *,
        syntax_checks: bool,
        architecture: dict[str, Any] | None = None,
        manifest: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        checks = validate_project(workspace, plan, syntax_checks=syntax_checks)
        if architecture is not None and manifest is not None and not any(
            item["status"] == "fail" for item in checks
        ):
            checks.extend(validate_python_packaging(workspace, architecture, manifest))
        return checks

    def validate_readiness(
        self,
        workspace: CleanWorkspace,
        architecture: dict[str, Any],
        manifest: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if self.readiness_executor is None:
            return [
                {
                    "name": "executable-validation",
                    "status": "skipped",
                    "message": "Executable Python readiness was not enabled",
                    "paths": [],
                }
            ]
        return self.readiness_executor.validate(
            workspace.project_root,
            architecture,
            manifest,
        )


def _python_module_path(module: str, *, modules: set[str], layout: str) -> str:
    prefix = PurePosixPath("src") if layout == "src" else PurePosixPath()
    parts = module.split(".")
    is_package = any(item.startswith(module + ".") for item in modules)
    relative = PurePosixPath(*parts)
    return str(prefix / relative / "__init__.py") if is_package else str(prefix / relative) + ".py"


def _package_surface_renames(
    manifest: dict[str, Any],
    *,
    layout: str,
) -> dict[str, str]:
    """Promote a module to a package when planned source descendants require it."""
    source_paths = {
        str(item["path"])
        for item in manifest["files"]
        if item["category"] == "source" and str(item["path"]).endswith(".py")
    }
    prefix = "src/" if layout == "src" else ""
    renames: dict[str, str] = {}
    for path in sorted(source_paths):
        pure = PurePosixPath(path)
        if pure.name == "__init__.py" or not path.startswith(prefix):
            continue
        package_root = pure.with_suffix("").as_posix()
        package_init = f"{package_root}/__init__.py"
        if package_init in source_paths:
            continue
        if any(item.startswith(f"{package_root}/") for item in source_paths):
            renames[path] = package_init
    return renames


def _apply_manifest_path_renames(
    manifest: dict[str, Any],
    renames: dict[str, str],
) -> None:
    for task in manifest["files"]:
        old_path = task["path"]
        task["path"] = renames.get(old_path, old_path)
        task["depends_on"] = list(
            dict.fromkeys(
                renames.get(item, item)
                for item in task["depends_on"]
                if renames.get(item, item) != task["path"]
            )
        )
    for binding in manifest["entry_points"]:
        binding["path"] = renames.get(binding["path"], binding["path"])
    for obligation in manifest["readiness_obligations"]:
        obligation["paths"] = list(
            dict.fromkeys(renames.get(item, item) for item in obligation["paths"])
        )

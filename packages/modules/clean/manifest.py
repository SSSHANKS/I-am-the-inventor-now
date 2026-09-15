from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

from packages.modules.clean.workspace import (
    MAX_FILES,
    WorkspaceError,
    validate_relative_path,
)


class CleanManifestError(ValueError):
    """A file manifest is inconsistent with its validated architecture."""


def normalise_manifest(
    manifest: dict[str, Any],
    architecture: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Remove behavioral claims that non-executable project files cannot satisfy."""
    normalised = deepcopy(manifest)
    component_requirements = {
        item["component_id"]: set(item["requirement_ids"])
        for item in (architecture or {}).get("components", [])
    }
    for task in normalised.get("files", []):
        if str(task.get("path", "")).casefold() in {
            "pyproject.toml",
            "setup.cfg",
            "setup.py",
            "package.json",
            "cargo.toml",
        }:
            task["category"] = "metadata"
        if task.get("category") in {"metadata", "documentation", "asset"}:
            task["requirement_ids"] = []
            task["provides"] = []
        elif task.get("component_id") in component_requirements:
            allowed = component_requirements[task["component_id"]]
            task["requirement_ids"] = [
                item for item in task.get("requirement_ids", []) if item in allowed
            ]
    if architecture is not None:
        components = {
            item["component_id"]: item for item in architecture.get("components", [])
        }
        component_closure = _dependency_closure(components) if components else {}
        contract_components = {
            item["contract_id"]: item["component_id"]
            for item in architecture.get("contracts", [])
            if "contract_id" in item and "component_id" in item
        }
        files_by_component: dict[str, list[str]] = {}
        providers: dict[str, list[str]] = {}
        for task in normalised.get("files", []):
            if task.get("category") == "source":
                files_by_component.setdefault(task["component_id"], []).append(task["path"])
            for contract_id in task.get("provides", []):
                providers.setdefault(contract_id, []).append(task["path"])
        for task in normalised.get("files", []):
            component_id = task.get("component_id")
            enforce_graph = component_id in component_closure
            allowed_components = {component_id, *component_closure.get(component_id, set())}
            if enforce_graph:
                task["requires"] = [
                    contract_id
                    for contract_id in task.get("requires", [])
                    if contract_components.get(contract_id) in allowed_components
                ]
            dependencies: list[str] = []
            for dependency in task.get("depends_on", []):
                replacement = dependency
                if dependency in component_requirements:
                    candidates = {
                        path
                        for contract_id in task.get("requires", [])
                        if contract_components.get(contract_id) == dependency
                        for path in providers.get(contract_id, [])
                    }
                    if len(candidates) == 1:
                        replacement = next(iter(candidates))
                    elif len(files_by_component.get(dependency, [])) == 1:
                        replacement = files_by_component[dependency][0]
                if replacement not in dependencies:
                    dependencies.append(replacement)
            task["depends_on"] = dependencies
            if enforce_graph:
                foreign_paths = {
                    item["path"]
                    for item in normalised.get("files", [])
                    if item.get("component_id") not in allowed_components
                }
                task["depends_on"] = [
                    dependency
                    for dependency in dependencies
                    if dependency not in foreign_paths
                ]
    return normalised


def architecture_sha256(architecture: dict[str, Any]) -> str:
    encoded = json.dumps(
        architecture,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_manifest(
    manifest: dict[str, Any],
    architecture: dict[str, Any],
) -> dict[str, Any]:
    """Validate file ownership, traceability, providers, and dependency order."""
    expected_hash = architecture_sha256(architecture)
    if manifest["architecture_sha256"] != expected_hash:
        raise CleanManifestError(
            "Manifest architecture hash does not match the validated architecture"
        )

    components = _id_map(architecture["components"], "component_id", "component")
    contracts = _id_map(architecture["contracts"], "contract_id", "contract")
    architecture_entries = _id_map(
        architecture["entry_points"],
        "entry_point_id",
        "architecture entry point",
    )
    files = manifest["files"]
    if len(files) > MAX_FILES:
        raise CleanManifestError(f"Manifest exceeds the {MAX_FILES}-file limit")

    paths: list[str] = []
    orders: list[int] = []
    for item in files:
        try:
            canonical = validate_relative_path(item["path"])
        except WorkspaceError as exc:
            raise CleanManifestError(str(exc)) from exc
        if canonical != item["path"]:
            raise CleanManifestError(
                f"Manifest path is not canonical: {item['path']!r}"
            )
        paths.append(canonical)
        orders.append(item["generation_order"])
    _require_unique(paths, "file paths")
    _require_unique([path.casefold() for path in paths], "case-insensitive file paths")
    _require_unique(orders, "generation orders")

    tasks = dict(zip(paths, files, strict=True))
    path_orders = {
        item["path"]: item["generation_order"] for item in files
    }
    component_files: dict[str, set[str]] = {item: set() for item in components}
    providers: dict[str, list[str]] = {}
    for path, task in tasks.items():
        component_id = task["component_id"]
        _require_known(
            {component_id},
            set(components),
            f"file {path!r} component",
        )
        component_files[component_id].add(path)
        _require_unique(task["requirement_ids"], f"requirement IDs in {path!r}")
        _require_unique(task["provides"], f"provided contracts in {path!r}")
        _require_unique(task["requires"], f"required contracts in {path!r}")
        _require_unique(task["depends_on"], f"dependencies in {path!r}")
        _require_unique(task["local_validators"], f"local validators in {path!r}")
        _require_unique(task["integration_checks"], f"integration checks in {path!r}")

        component_requirements = set(components[component_id]["requirement_ids"])
        unknown_requirements = set(task["requirement_ids"]) - component_requirements
        if unknown_requirements:
            raise CleanManifestError(
                f"File {path!r} claims requirements outside component {component_id}: "
                + ", ".join(sorted(unknown_requirements))
            )
        if task["category"] in {"metadata", "documentation"} and task[
            "requirement_ids"
        ]:
            raise CleanManifestError(
                f"{task['category'].title()} file {path!r} cannot satisfy functional "
                "requirements"
            )
        if task["category"] in {"metadata", "documentation", "asset"} and task[
            "provides"
        ]:
            raise CleanManifestError(
                f"{task['category'].title()} file {path!r} cannot provide symbol "
                "contracts"
            )

        referenced_contracts = set(task["provides"]) | set(task["requires"])
        _require_known(
            referenced_contracts,
            set(contracts),
            f"file {path!r} contracts",
        )
        overlap = set(task["provides"]) & set(task["requires"])
        if overlap:
            raise CleanManifestError(
                f"File {path!r} both provides and requires: "
                + ", ".join(sorted(overlap))
            )
        for contract_id in task["provides"]:
            contract = contracts[contract_id]
            if contract["component_id"] != component_id:
                raise CleanManifestError(
                    f"File {path!r} provides contract {contract_id} owned by "
                    f"component {contract['component_id']}"
                )
            missing_requirements = set(contract["requirement_ids"]) - set(
                task["requirement_ids"]
            )
            if missing_requirements:
                raise CleanManifestError(
                    f"Provider {path!r} omits requirements for {contract_id}: "
                    + ", ".join(sorted(missing_requirements))
                )
            providers.setdefault(contract_id, []).append(path)

        dependencies = set(task["depends_on"])
        _require_known(dependencies, set(tasks), f"file {path!r} dependencies")
        if path in dependencies:
            raise CleanManifestError(f"File {path!r} depends on itself")
        late = sorted(
            dependency
            for dependency in dependencies
            if path_orders[dependency] >= task["generation_order"]
        )
        if late:
            raise CleanManifestError(
                f"Manifest dependencies must be generated before {path!r}: "
                + ", ".join(late)
            )

    empty_components = sorted(
        component_id for component_id, owned in component_files.items() if not owned
    )
    if empty_components:
        raise CleanManifestError(
            "Manifest does not allocate files to components: "
            + ", ".join(empty_components)
        )

    missing_providers = sorted(set(contracts) - set(providers))
    if missing_providers:
        raise CleanManifestError(
            "Manifest does not provide architecture contracts: "
            + ", ".join(missing_providers)
        )
    duplicate_providers = {
        contract_id: provider_paths
        for contract_id, provider_paths in providers.items()
        if len(provider_paths) != 1
    }
    if duplicate_providers:
        rendered = "; ".join(
            f"{contract_id}: {', '.join(sorted(provider_paths))}"
            for contract_id, provider_paths in sorted(duplicate_providers.items())
        )
        raise CleanManifestError(
            f"Manifest provides architecture contracts more than once: {rendered}"
        )

    provider_by_contract = {
        contract_id: provider_paths[0]
        for contract_id, provider_paths in providers.items()
    }
    component_closure = _dependency_closure(components)
    for path, task in tasks.items():
        dependencies = set(task["depends_on"])
        component_id = task["component_id"]
        for contract_id in task["requires"]:
            provider = provider_by_contract[contract_id]
            if provider not in dependencies:
                raise CleanManifestError(
                    f"File {path!r} requires {contract_id} but does not depend on "
                    f"provider {provider!r}"
                )
            provider_component = contracts[contract_id]["component_id"]
            if (
                provider_component != component_id
                and provider_component not in component_closure[component_id]
            ):
                raise CleanManifestError(
                    f"File {path!r} consumes {contract_id} outside the architecture "
                    f"dependency graph for component {component_id}"
                )
        for dependency in dependencies:
            dependency_component = tasks[dependency]["component_id"]
            if (
                dependency_component != component_id
                and dependency_component not in component_closure[component_id]
            ):
                raise CleanManifestError(
                    f"File {path!r} depends on {dependency!r} outside the "
                    f"architecture dependency graph for component {component_id}"
                )

    manifest_entries = _id_map(
        manifest["entry_points"],
        "entry_point_id",
        "manifest entry point",
    )
    if set(manifest_entries) != set(architecture_entries):
        missing = sorted(set(architecture_entries) - set(manifest_entries))
        extra = sorted(set(manifest_entries) - set(architecture_entries))
        raise CleanManifestError(
            "Manifest entry points do not match architecture"
            f"; missing={missing}; extra={extra}"
        )
    for entry_point_id, binding in manifest_entries.items():
        path = binding["path"]
        _require_known({path}, set(tasks), f"entry point {entry_point_id} path")
        architecture_entry = architecture_entries[entry_point_id]
        if tasks[path]["component_id"] != architecture_entry["component_id"]:
            raise CleanManifestError(
                f"Entry point {entry_point_id} path is owned by the wrong component"
            )
        missing_contracts = set(architecture_entry["contract_ids"]) - set(
            tasks[path]["provides"]
        )
        if missing_contracts:
            raise CleanManifestError(
                f"Entry point {entry_point_id} path does not provide contracts: "
                + ", ".join(sorted(missing_contracts))
            )

    obligations = _id_map(
        manifest["readiness_obligations"],
        "obligation_id",
        "readiness obligation",
    )
    covered_paths: set[str] = set()
    for obligation_id, obligation in obligations.items():
        obligation_paths = set(obligation["paths"])
        obligation_components = set(obligation["component_ids"])
        _require_unique(obligation["paths"], f"paths in {obligation_id}")
        _require_unique(
            obligation["component_ids"],
            f"components in {obligation_id}",
        )
        _require_known(obligation_paths, set(tasks), f"{obligation_id} paths")
        _require_known(
            obligation_components,
            set(components),
            f"{obligation_id} components",
        )
        covered_paths.update(obligation_paths)
    uncovered = set(tasks) - covered_paths
    if uncovered:
        raise CleanManifestError(
            "Manifest files lack readiness obligations: "
            + ", ".join(sorted(uncovered))
        )
    if not any(
        item["required"] and item["kind"] == "manifest"
        for item in obligations.values()
    ):
        raise CleanManifestError(
            "Manifest requires a mandatory manifest-integrity obligation"
        )
    return manifest


def _id_map(
    items: list[dict[str, Any]],
    field: str,
    label: str,
) -> dict[str, dict[str, Any]]:
    values = [str(item[field]) for item in items]
    _require_unique(values, f"{label} IDs")
    return dict(zip(values, items, strict=True))


def _require_unique(values: list[Any], label: str) -> None:
    if len(values) != len(set(values)):
        raise CleanManifestError(f"Manifest repeats {label}")


def _require_known(referenced: set[str], known: set[str], label: str) -> None:
    unknown = referenced - known
    if unknown:
        raise CleanManifestError(
            f"Manifest {label} reference unknown values: "
            + ", ".join(sorted(unknown))
        )


def _dependency_closure(
    components: dict[str, dict[str, Any]],
) -> dict[str, set[str]]:
    closure: dict[str, set[str]] = {}
    for component_id in components:
        visited: set[str] = set()
        pending = list(components[component_id]["depends_on"])
        while pending:
            dependency = pending.pop()
            if dependency in visited:
                continue
            visited.add(dependency)
            pending.extend(components[dependency]["depends_on"])
        closure[component_id] = visited
    return closure

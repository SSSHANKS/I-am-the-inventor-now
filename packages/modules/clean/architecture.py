from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from packages.modules.clean.requirements import requirement_statements


class CleanArchitectureError(ValueError):
    """A project-first architecture is internally inconsistent or untraceable."""


def normalise_architecture(architecture: dict[str, Any]) -> dict[str, Any]:
    """Recover uniquely implied allocation and merge forced Python module owners."""
    normalised = deepcopy(architecture)
    _recover_unique_capability_requirements(normalised)
    if "python" not in str(normalised["project_profile"]["language"]).casefold():
        return normalised
    _align_python_contract_declaration_names(normalised)
    _split_python_entry_points_by_module(normalised)

    owners_by_module: dict[str, set[str]] = {}
    for contract in normalised["contracts"]:
        module, separator, _ = str(contract["qualified_name"]).rpartition(".")
        if separator:
            owners_by_module.setdefault(module, set()).add(contract["component_id"])
    parent = {item["component_id"]: item["component_id"] for item in normalised["components"]}

    def find(component_id: str) -> str:
        while parent[component_id] != component_id:
            parent[component_id] = parent[parent[component_id]]
            component_id = parent[component_id]
        return component_id

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            canonical, merged = sorted((left_root, right_root))
            parent[merged] = canonical

    for owners in owners_by_module.values():
        ordered = sorted(owners)
        for owner in ordered[1:]:
            union(ordered[0], owner)
    component_map = {component_id: find(component_id) for component_id in parent}
    if all(component_id == target for component_id, target in component_map.items()):
        return normalised

    grouped: dict[str, list[dict[str, Any]]] = {}
    for component in normalised["components"]:
        grouped.setdefault(component_map[component["component_id"]], []).append(component)
    merged_components = []
    for canonical, group in grouped.items():
        primary = next(item for item in group if item["component_id"] == canonical)
        primary["purpose"] = "; ".join(
            dict.fromkeys(item["purpose"] for item in group)
        )
        primary["requirement_ids"] = list(
            dict.fromkeys(value for item in group for value in item["requirement_ids"])
        )
        primary["depends_on"] = list(
            dict.fromkeys(
                component_map[value]
                for item in group
                for value in item["depends_on"]
                if component_map[value] != canonical
            )
        )
        merged_components.append(primary)
    normalised["components"] = merged_components

    for capability in normalised["capabilities"]:
        capability["component_ids"] = list(
            dict.fromkeys(component_map[item] for item in capability["component_ids"])
        )
    for contract in normalised["contracts"]:
        contract["component_id"] = component_map[contract["component_id"]]
    for entry_point in normalised["entry_points"]:
        entry_point["component_id"] = component_map[entry_point["component_id"]]
    for proposal in normalised["proposals"]:
        proposal["affected_component_ids"] = list(
            dict.fromkeys(
                component_map[item] for item in proposal["affected_component_ids"]
            )
        )
    return normalised


def _recover_unique_capability_requirements(architecture: dict[str, Any]) -> None:
    """Allocate an omitted behavior only when component ownership makes it unique."""
    components = {
        item["component_id"]: set(item["requirement_ids"])
        for item in architecture["components"]
    }
    owned_requirements = {item for values in components.values() for item in values}
    allocated = {
        item
        for capability in architecture["capabilities"]
        for item in capability["requirement_ids"]
    }
    for requirement_id in sorted(owned_requirements - allocated):
        candidates = [
            capability
            for capability in architecture["capabilities"]
            if any(
                requirement_id in components.get(component_id, set())
                for component_id in capability["component_ids"]
            )
        ]
        if len(candidates) == 1:
            candidates[0]["requirement_ids"].append(requirement_id)


def _align_python_contract_declaration_names(architecture: dict[str, Any]) -> None:
    """Align redundant declaration names with authoritative qualified names."""
    for contract in architecture["contracts"]:
        expected = str(contract["qualified_name"]).rsplit(".", 1)[-1]
        declaration = str(contract["declaration"])
        if declaration.lstrip().startswith("class "):
            aligned = re.sub(
                r"^(\s*class\s+)[A-Za-z_][A-Za-z0-9_]*",
                rf"\g<1>{expected}",
                declaration,
                count=1,
            )
            aligned = re.sub(
                r"(\)(?:\s*->\s*[^;:\n]+)?)\s*;\s*"
                r"([A-Za-z_][A-Za-z0-9_]*\s*\()",
                r"\1:\n        ...\n    def \2",
                aligned,
            )
            compact = re.fullmatch(
                r"(\s*class\s+[A-Za-z_][A-Za-z0-9_]*(?:\([^\n]*\))?\s*:)[ \t]*"
                r"([A-Za-z_][A-Za-z0-9_]*\([^\n]*\)(?:\s*->\s*[^:\n]+)?)\s*",
                aligned,
            )
            if compact:
                aligned = (
                    f"{compact.group(1)}\n"
                    f"    def {compact.group(2)}:\n"
                    "        pass"
                )
            contract["declaration"] = aligned
            continue
        contract["declaration"] = re.sub(
            r"^(\s*)[A-Za-z_][A-Za-z0-9_]*(?=\s*\()",
            rf"\g<1>{expected}",
            declaration,
            count=1,
        )


def _split_python_entry_points_by_module(architecture: dict[str, Any]) -> None:
    """Ensure every Python entry-point binding can resolve to one provider file."""
    contracts = {
        item["contract_id"]: str(item["qualified_name"]).rsplit(".", 1)[0]
        for item in architecture["contracts"]
    }
    used_ids = {item["entry_point_id"] for item in architecture["entry_points"]}
    next_number = max(
        (int(item.rsplit("-", 1)[-1]) for item in used_ids),
        default=0,
    ) + 1
    normalised_entries: list[dict[str, Any]] = []
    for entry in architecture["entry_points"]:
        groups: dict[str, list[str]] = {}
        for contract_id in entry["contract_ids"]:
            groups.setdefault(contracts[contract_id], []).append(contract_id)
        for index, contract_ids in enumerate(groups.values()):
            split = deepcopy(entry)
            if index:
                while f"EP-{next_number:03d}" in used_ids:
                    next_number += 1
                split["entry_point_id"] = f"EP-{next_number:03d}"
                used_ids.add(split["entry_point_id"])
                next_number += 1
            split["contract_ids"] = contract_ids
            normalised_entries.append(split)
    architecture["entry_points"] = normalised_entries


def validate_architecture(
    architecture: dict[str, Any],
    specification: str,
    *,
    compatibility_mode: str | None = None,
) -> dict[str, Any]:
    """Validate references, traceability, and the component dependency graph."""
    statements = requirement_statements(specification)
    declared = set(statements)
    behavior_ids = {item for item in declared if not item.startswith("TC-")}
    scenario_ids = declared - behavior_ids

    profile = architecture["project_profile"]
    if (
        compatibility_mode is not None
        and profile["compatibility_mode"] != compatibility_mode
    ):
        raise CleanArchitectureError(
            "Architecture compatibility mode does not match the Clean handoff"
        )

    capabilities = _unique_by_id(
        architecture["capabilities"], "capability_id", "capability"
    )
    components = _unique_by_id(
        architecture["components"], "component_id", "component"
    )
    contracts = _unique_by_id(
        architecture["contracts"], "contract_id", "contract"
    )
    entry_points = _unique_by_id(
        architecture["entry_points"], "entry_point_id", "entry point"
    )
    _unique_by_id(architecture["proposals"], "proposal_id", "proposal")
    _require_unique_values(
        [item["qualified_name"] for item in contracts.values()],
        "contract qualified names",
    )
    if "python" in str(profile["language"]).casefold():
        module_components: dict[str, set[str]] = {}
        for contract in contracts.values():
            qualified_name = str(contract["qualified_name"])
            module, separator, _ = qualified_name.rpartition(".")
            if separator:
                module_components.setdefault(module, set()).add(contract["component_id"])
        split_modules = {
            module: owners
            for module, owners in module_components.items()
            if len(owners) > 1
        }
        if split_modules:
            rendered = "; ".join(
                f"{module}: {', '.join(sorted(owners))}"
                for module, owners in sorted(split_modules.items())
            )
            raise CleanArchitectureError(
                "Python contract modules cannot span component ownership: " + rendered
            )
    _require_unique_values(
        [item["name"] for item in architecture["dependency_decisions"]],
        "dependency decision names",
    )

    for collection_name, items in (
        ("capability", capabilities.values()),
        ("component", components.values()),
        ("contract", contracts.values()),
        ("dependency decision", architecture["dependency_decisions"]),
    ):
        for item in items:
            _reject_unknown_requirements(
                item["requirement_ids"],
                declared,
                collection_name,
            )

    allocated_behavior = {
        requirement_id
        for capability in capabilities.values()
        for requirement_id in capability["requirement_ids"]
    }
    _require_complete_allocation(
        behavior_ids,
        allocated_behavior,
        "capabilities",
    )
    allocated_scenarios = {
        scenario_id
        for capability in capabilities.values()
        for scenario_id in capability["scenario_ids"]
    }
    _reject_unknown_requirements(
        allocated_scenarios,
        scenario_ids,
        "capability scenario",
    )
    _require_complete_allocation(
        scenario_ids,
        allocated_scenarios,
        "capability scenarios",
    )

    referenced_components: set[str] = set()
    for capability in capabilities.values():
        component_ids = _unique_references(
            capability["component_ids"],
            f"capability {capability['capability_id']}",
        )
        _require_known_references(
            component_ids,
            set(components),
            f"capability {capability['capability_id']} components",
        )
        referenced_components.update(component_ids)
        supported_requirements = {
            requirement_id
            for component_id in component_ids
            for requirement_id in components[component_id]["requirement_ids"]
        }
        missing = set(capability["requirement_ids"]) - supported_requirements
        if missing:
            raise CleanArchitectureError(
                f"Capability {capability['capability_id']} requirements are not owned "
                f"by its components: {', '.join(sorted(missing))}"
            )

    orphan_components = set(components) - referenced_components
    if orphan_components:
        raise CleanArchitectureError(
            "Architecture contains components unowned by any capability: "
            + ", ".join(sorted(orphan_components))
        )

    allocated_components = {
        requirement_id
        for component in components.values()
        for requirement_id in component["requirement_ids"]
    }
    _require_complete_allocation(
        behavior_ids,
        allocated_components,
        "components",
    )
    component_dependencies: dict[str, set[str]] = {}
    for component_id, component in components.items():
        dependencies = _unique_references(
            component["depends_on"],
            f"component {component_id}",
        )
        _require_known_references(
            dependencies,
            set(components),
            f"component {component_id} dependencies",
        )
        if component_id in dependencies:
            raise CleanArchitectureError(f"Component {component_id} depends on itself")
        component_dependencies[component_id] = dependencies
    _validate_acyclic(component_dependencies)

    for contract_id, contract in contracts.items():
        component_id = contract["component_id"]
        _require_known_references(
            {component_id},
            set(components),
            f"contract {contract_id} component",
        )
        unsupported = set(contract["requirement_ids"]) - set(
            components[component_id]["requirement_ids"]
        )
        if unsupported:
            raise CleanArchitectureError(
                f"Contract {contract_id} claims requirements outside component "
                f"{component_id}: {', '.join(sorted(unsupported))}"
            )

    for entry_point_id, entry_point in entry_points.items():
        component_id = entry_point["component_id"]
        _require_known_references(
            {component_id},
            set(components),
            f"entry point {entry_point_id} component",
        )
        contract_ids = _unique_references(
            entry_point["contract_ids"],
            f"entry point {entry_point_id}",
        )
        _require_known_references(
            contract_ids,
            set(contracts),
            f"entry point {entry_point_id} contracts",
        )
        foreign = {
            contract_id
            for contract_id in contract_ids
            if contracts[contract_id]["component_id"] != component_id
        }
        if foreign:
            raise CleanArchitectureError(
                f"Entry point {entry_point_id} references contracts owned by another "
                f"component: {', '.join(sorted(foreign))}"
            )

    for proposal in architecture["proposals"]:
        _require_known_references(
            set(proposal["affected_component_ids"]),
            set(components),
            f"proposal {proposal['proposal_id']} components",
        )
    return architecture


def _unique_by_id(
    items: list[dict[str, Any]],
    field: str,
    label: str,
) -> dict[str, dict[str, Any]]:
    values = [str(item[field]) for item in items]
    _require_unique_values(values, f"{label} IDs")
    return dict(zip(values, items, strict=True))


def _require_unique_values(values: list[str], label: str) -> None:
    repeated = sorted({value for value in values if values.count(value) > 1})
    if repeated:
        raise CleanArchitectureError(
            f"Architecture repeats {label}: {', '.join(repeated)}"
        )


def _unique_references(values: list[str], owner: str) -> set[str]:
    if len(values) != len(set(values)):
        raise CleanArchitectureError(f"Architecture repeats references in {owner}")
    return set(values)


def _require_known_references(
    referenced: set[str],
    declared: set[str],
    label: str,
) -> None:
    unknown = referenced - declared
    if unknown:
        raise CleanArchitectureError(
            f"Architecture {label} reference unknown IDs: {', '.join(sorted(unknown))}"
        )


def _reject_unknown_requirements(
    referenced: list[str] | set[str],
    declared: set[str],
    label: str,
) -> None:
    unknown = set(referenced) - declared
    if unknown:
        raise CleanArchitectureError(
            f"Architecture {label} references requirements absent from the "
            f"specification: {', '.join(sorted(unknown))}"
        )


def _require_complete_allocation(
    required: set[str],
    allocated: set[str],
    label: str,
) -> None:
    missing = required - allocated
    if missing:
        raise CleanArchitectureError(
            f"Architecture does not allocate requirements to {label}: "
            + ", ".join(sorted(missing))
        )


def _validate_acyclic(dependencies: dict[str, set[str]]) -> None:
    remaining = {key: set(value) for key, value in dependencies.items()}
    while remaining:
        ready = {key for key, value in remaining.items() if not value}
        if not ready:
            raise CleanArchitectureError(
                "Architecture component dependency cycle: "
                + ", ".join(sorted(remaining))
            )
        for component_id in ready:
            del remaining[component_id]
        for value in remaining.values():
            value.difference_update(ready)

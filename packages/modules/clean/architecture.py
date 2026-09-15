from __future__ import annotations

import ast
import re
from copy import deepcopy
from typing import Any

from packages.modules.clean.python_contract_paths import python_contract_module
from packages.modules.clean.requirements import requirement_statements


class CleanArchitectureError(ValueError):
    """A project-first architecture is internally inconsistent or untraceable."""


def normalise_architecture(architecture: dict[str, Any]) -> dict[str, Any]:
    """Recover uniquely implied allocation and merge forced Python module owners."""
    normalised = deepcopy(architecture)
    _recover_contract_component_requirements(normalised)
    _recover_single_component_capability_requirements(normalised)
    _recover_unique_capability_requirements(normalised)
    _align_entry_point_component_owners(normalised)
    if "python" not in str(normalised["project_profile"]["language"]).casefold():
        return normalised
    _normalise_python_package_surface_names(normalised)
    _normalise_python_contract_kinds(normalised)
    _align_python_contract_declaration_names(normalised)
    _split_python_entry_points_by_module(normalised)
    _align_entry_point_component_owners(normalised)

    owners_by_module: dict[str, set[str]] = {}
    for contract in normalised["contracts"]:
        module = python_contract_module(contract, normalised["contracts"])
        if module:
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


def normalise_behavior_rule_evidence(
    architecture: dict[str, Any],
    specification: str,
) -> dict[str, Any]:
    """Remove only misattributed entries from otherwise grounded mixed rules.

    A rule with no valid evidence remains untouched so validation still rejects it.
    Contract and component requirement allocation is never changed here.
    """
    statements = requirement_statements(specification)
    for contract in architecture["contracts"]:
        if "behavior_rules" not in contract:
            continue
        rules = contract["behavior_rules"]
        grounded_statements = {
            _normalise_evidence_text(rule["statement"])
            for rule in rules
            if rule["source"] == "specification"
            and rule.get("evidence")
            and all(
                _evidence_matches_requirement(item, statements)
                for item in rule["evidence"]
            )
        }
        retained = []
        for rule in rules:
            if rule["source"] != "specification":
                retained.append(rule)
                continue
            evidence = rule.get("evidence")
            if not evidence:
                retained.append(rule)
                continue
            valid_ids = {
                item["requirement_id"]
                for item in evidence
                if _evidence_matches_requirement(item, statements)
            }
            claimed = set(rule["requirement_ids"])
            if valid_ids == claimed:
                retained.append(rule)
                continue
            if valid_ids:
                rule["requirement_ids"] = [
                    item for item in rule["requirement_ids"] if item in valid_ids
                ]
                rule["evidence"] = [
                    item for item in evidence if item["requirement_id"] in valid_ids
                ]
                retained.append(rule)
                continue
            # A model may duplicate one grounded statement into separate rules and
            # attach an unrelated ID to one copy. Removing only that duplicate is
            # safer than accepting false provenance. A unique ungrounded rule still
            # remains present and is rejected by validation.
            if _normalise_evidence_text(rule["statement"]) not in grounded_statements:
                retained.append(rule)
        contract["behavior_rules"] = retained
    return architecture


def _normalise_python_package_surface_names(
    architecture: dict[str, Any],
) -> None:
    """Use Python's import name for contracts provided by package ``__init__``."""
    for contract in architecture["contracts"]:
        qualified_name = str(contract["qualified_name"])
        module, separator, symbol = qualified_name.rpartition(".")
        if (
            separator
            and symbol == "__init__"
            and _python_string_collection_assignment(
                str(contract.get("declaration", "")), "__init__"
            )
        ):
            contract["qualified_name"] = f"{module}.__all__"
            contract["declaration"] = re.sub(
                r"^(\s*)__init__(?=\s*=)",
                r"\1__all__",
                str(contract["declaration"]),
                count=1,
            )
            continue
        if not separator or not module.endswith(".__init__"):
            continue
        package = module.removesuffix(".__init__")
        if package:
            contract["qualified_name"] = f"{package}.{symbol}"


def _normalise_python_contract_kinds(architecture: dict[str, Any]) -> None:
    """Recover constant kinds from unambiguous assignment declarations."""
    for contract in architecture["contracts"]:
        if _python_constant_declaration(str(contract["declaration"])) is not None:
            contract["kind"] = "constant"


def _python_constant_declaration(declaration: str) -> ast.Assign | ast.AnnAssign | None:
    try:
        tree = ast.parse(declaration)
    except SyntaxError:
        return None
    node = tree.body[0] if len(tree.body) == 1 else None
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node
    if (
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
    ):
        return node
    return None


def _python_string_collection_assignment(declaration: str, name: str) -> bool:
    node = _python_constant_declaration(declaration)
    if not isinstance(node, ast.Assign) or not isinstance(node.targets[0], ast.Name):
        return False
    return (
        node.targets[0].id == name
        and isinstance(node.value, (ast.List, ast.Tuple, ast.Set))
        and all(
            isinstance(item, ast.Constant) and isinstance(item.value, str)
            for item in node.value.elts
        )
    )


def _recover_contract_component_requirements(architecture: dict[str, Any]) -> None:
    """Make a contract's declared owner own the requirements the contract implements."""
    components = {
        item["component_id"]: item for item in architecture["components"]
    }
    for contract in architecture["contracts"]:
        component = components.get(contract["component_id"])
        if component is None:
            continue
        component["requirement_ids"] = list(
            dict.fromkeys(
                [*component["requirement_ids"], *contract["requirement_ids"]]
            )
        )


def _recover_single_component_capability_requirements(
    architecture: dict[str, Any],
) -> None:
    """Give a sole responsible component the requirements its capability declares."""
    components = {
        item["component_id"]: item for item in architecture["components"]
    }
    for capability in architecture["capabilities"]:
        component_ids = list(dict.fromkeys(capability["component_ids"]))
        if len(component_ids) != 1 or component_ids[0] not in components:
            continue
        component = components[component_ids[0]]
        component["requirement_ids"] = list(
            dict.fromkeys(
                [*component["requirement_ids"], *capability["requirement_ids"]]
            )
        )


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


def _align_entry_point_component_owners(architecture: dict[str, Any]) -> None:
    """Use a shared contract owner when an entry point's ownership is unambiguous."""
    contract_owners = {
        item["contract_id"]: item["component_id"]
        for item in architecture["contracts"]
    }
    for entry_point in architecture["entry_points"]:
        owners = {
            contract_owners[contract_id]
            for contract_id in entry_point["contract_ids"]
            if contract_id in contract_owners
        }
        if len(owners) == 1:
            entry_point["component_id"] = owners.pop()


def _align_python_contract_declaration_names(architecture: dict[str, Any]) -> None:
    """Align redundant declaration names with authoritative qualified names."""
    for contract in architecture["contracts"]:
        expected = str(contract["qualified_name"]).rsplit(".", 1)[-1]
        declaration = str(contract["declaration"])
        if _python_constant_declaration(declaration) is not None:
            contract["declaration"] = re.sub(
                r"^(\s*)[A-Za-z_][A-Za-z0-9_]*",
                rf"\g<1>{expected}",
                declaration,
                count=1,
            )
            continue
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
        if declaration.lstrip().startswith(("def ", "async def ")):
            contract["declaration"] = re.sub(
                r"^(\s*(?:async\s+)?def\s+)[A-Za-z_][A-Za-z0-9_]*",
                rf"\g<1>{expected}",
                declaration,
                count=1,
            )
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
        item["contract_id"]: python_contract_module(item, architecture["contracts"])
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
    semantic_diagnostics: list[str] = []
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
            module = python_contract_module(contract, contracts.values())
            if module:
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
    try:
        _require_complete_allocation(
            behavior_ids,
            allocated_behavior,
            "capabilities",
        )
    except CleanArchitectureError as exc:
        semantic_diagnostics.append(str(exc))
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
    try:
        _require_complete_allocation(
            scenario_ids,
            allocated_scenarios,
            "capability scenarios",
        )
    except CleanArchitectureError as exc:
        semantic_diagnostics.append(str(exc))
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
    try:
        _require_complete_allocation(
            behavior_ids,
            allocated_components,
            "components",
        )
    except CleanArchitectureError as exc:
        semantic_diagnostics.append(str(exc))
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
    for validator, args in (
        (_validate_behavior_rules, (architecture, statements)),
        (_validate_observable_auxiliary_contracts, (architecture,)),
    ):
        try:
            validator(*args)
        except CleanArchitectureError as exc:
            semantic_diagnostics.append(str(exc))
    if semantic_diagnostics:
        raise CleanArchitectureError(
            "Architecture has multiple semantic defects: "
            + " | ".join(dict.fromkeys(semantic_diagnostics))
        )
    return architecture


def _validate_behavior_rules(architecture: dict[str, Any], statements: dict[str, str]) -> None:
    """Check provenance, not the semantic truth of a model's interpretation."""
    proposals = {item["proposal_id"]: item for item in architecture["proposals"]}
    for contract in architecture["contracts"]:
        for rule in contract.get("behavior_rules", []):
            label = f"Contract {contract['contract_id']} {rule['aspect']} behavior rule"
            ids = set(rule["requirement_ids"])
            if not ids or not ids.issubset(contract["requirement_ids"]):
                raise CleanArchitectureError(f"{label} references requirements outside its contract")
            if len(ids) != len(rule["requirement_ids"]):
                raise CleanArchitectureError(f"{label} repeats requirement references")
            if rule["source"] == "specification":
                if "proposal_id" in rule:
                    raise CleanArchitectureError(f"{label} cannot label a proposal as specification evidence")
                # New rules separate their synthesis from literal source evidence.
                # Legacy rules still treat the statement itself as their excerpt.
                evidence = rule.get("evidence", [
                    {"requirement_id": requirement_id, "excerpt": rule["statement"]}
                    for requirement_id in sorted(ids)
                ])
                evidence_ids = [item["requirement_id"] for item in evidence]
                if set(evidence_ids) != ids or len(evidence_ids) != len(ids):
                    raise CleanArchitectureError(
                        f"{label} evidence must reference each claimed requirement exactly once; "
                        f"claimed={sorted(ids)}, evidence={evidence_ids}"
                    )
                unmatched = [
                    item["requirement_id"] for item in evidence
                    if not _evidence_matches_requirement(item, statements)
                ]
                if unmatched:
                    raise CleanArchitectureError(
                        f"{label} evidence must quote a verbatim excerpt from its own requirement; "
                        f"unmatched={unmatched}; statement={rule['statement']!r}. "
                        "Supply separate evidence excerpts for combined requirements; "
                        "record unstated choices as clean proposals instead"
                    )
            elif rule["source"] == "clean-proposal":
                if "evidence" in rule:
                    raise CleanArchitectureError(f"{label} cannot label a proposal as specification evidence")
                proposal = proposals.get(rule.get("proposal_id"))
                if not proposal or proposal["source"] != "clean-proposal":
                    raise CleanArchitectureError(f"{label} requires an existing clean-proposal ledger entry")
                if contract["component_id"] not in proposal["affected_component_ids"]:
                    raise CleanArchitectureError(f"{label} proposal does not cover its component")
                if _normalise_evidence_text(rule["statement"]) != _normalise_evidence_text(
                    proposal["chosen_value"]
                ):
                    raise CleanArchitectureError(f"{label} must match the proposal's chosen_value")
            else:
                raise CleanArchitectureError(f"{label} has unknown provenance")


def _validate_observable_auxiliary_contracts(architecture: dict[str, Any]) -> None:
    """Require a distinct contract when the specification names a distinct event channel."""
    contracts = architecture["contracts"]
    capabilities = architecture["capabilities"]
    dependencies = {
        component["component_id"]: set(component["depends_on"])
        for component in architecture["components"]
    }

    def dependency_reaches(source: str, target: str) -> bool:
        pending = list(dependencies.get(source, ()))
        visited: set[str] = set()
        while pending:
            current = pending.pop()
            if current == target:
                return True
            if current not in visited:
                visited.add(current)
                pending.extend(dependencies.get(current, ()))
        return False

    def linked(producer: dict[str, Any], observer: dict[str, Any], requirement_id: str) -> bool:
        return (
            observer["contract_id"] != producer["contract_id"]
            and requirement_id in observer["requirement_ids"]
            and (
                observer["component_id"] == producer["component_id"]
                or any(
                    requirement_id in capability["requirement_ids"]
                    and producer["component_id"] in capability["component_ids"]
                    and observer["component_id"] in capability["component_ids"]
                    and (
                        dependency_reaches(producer["component_id"], observer["component_id"])
                        or dependency_reaches(observer["component_id"], producer["component_id"])
                    )
                    for capability in capabilities
                )
            )
        )

    def exposes_interaction(
        producer: dict[str, Any], observer: dict[str, Any], requirement_id: str
    ) -> bool:
        """Recognize a declared registration, injection, or observer-access path."""
        observer_declaration = str(observer.get("declaration", ""))

        def registration_shape(declaration: str) -> bool:
            folded = declaration.casefold()
            participant = re.search(
                r"\b(callback|callable|consumer|function|handler|listener|observer|subscriber)\b"
                r"|\bfn(?:once|mut)?\b",
                folded,
            )
            action = re.search(
                r"\b(add|attach|bind|connect|listen|observe|on|register|subscribe|watch)\w*\b",
                folded,
            )
            return bool(participant and action)

        if registration_shape(observer_declaration):
            return True
        observer_name = str(observer.get("qualified_name", "")).rsplit(".", 1)[-1]
        producer_declaration = str(producer.get("declaration", ""))
        if any(
            registration_shape(line)
            and (
                "observer" in line.casefold()
                or "lifecycle" in line.casefold()
                or observer_name.casefold() in line.casefold()
            )
            for line in producer_declaration.splitlines()
        ):
            return True
        if observer_name and re.search(
            rf"\b{re.escape(observer_name)}\b", producer_declaration
        ):
            return True
        related_components = {producer["component_id"], observer["component_id"]}
        return any(
            bridge["contract_id"] not in {
                producer["contract_id"], observer["contract_id"]
            }
            and bridge["component_id"] in related_components
            and requirement_id in bridge["requirement_ids"]
            and (
                re.search(
                    rf"\b{re.escape(observer_name)}\b",
                    str(bridge.get("declaration", "")),
                )
                or registration_shape(str(bridge.get("declaration", "")))
            )
            for bridge in contracts
        )

    missing: list[tuple[str, str, str]] = []
    for contract in contracts:
        for rule in contract.get("behavior_rules", []):
            evidence_by_requirement = {
                item["requirement_id"]: str(item["excerpt"])
                for item in rule.get("evidence", [])
            }
            for requirement_id in rule["requirement_ids"]:
                statement = evidence_by_requirement.get(
                    requirement_id, str(rule["statement"])
                ).casefold()
                distinct_channel = (
                    any(
                        word in statement
                        for word in ("auxiliary", "dedicated", "distinct", "secondary")
                    )
                    and any(
                        word in statement
                        for word in ("event", "signal", "notification")
                    )
                )
                if not distinct_channel:
                    continue
                if any(
                    linked(contract, other, requirement_id)
                    and exposes_interaction(contract, other, requirement_id)
                    for other in contracts
                ):
                    continue
                missing.append(
                    (requirement_id, contract["contract_id"], contract["component_id"])
                )
    if missing:
        rendered = ", ".join(
            f"{requirement_id} ({contract_id}/{component_id})"
            for requirement_id, contract_id, component_id in missing
        )
        raise CleanArchitectureError(
            "Observable auxiliary or lifecycle requirements must be jointly claimed by "
            f"a producing operation contract and a separate observer contract: {rendered}. "
            "Keep each listed requirement on the producer and also claim it on the observer; "
            "do not transfer it from one contract to the other. The observer may belong to "
            "another component when both components share the capability and their dependency "
            "graph supports the interaction. Declare a public interaction path for observer "
            "registration, injection, or access; a disconnected callback class is insufficient. "
            "Record an unstated name or shape as a clean "
            "proposal instead of treating ordinary operation handlers as lifecycle observers"
        )


def _evidence_matches_requirement(
    evidence: dict[str, Any],
    statements: dict[str, str],
) -> bool:
    excerpt = _normalise_evidence_text(str(evidence.get("excerpt", "")))
    statement = _normalise_evidence_text(
        statements.get(str(evidence.get("requirement_id", "")), "")
    )
    return bool(excerpt) and excerpt in statement


def _normalise_evidence_text(text: str) -> str:
    normalized = " ".join(text.split())
    normalized = re.sub(
        r"\bEvidence\s*:\s*(?=EV-\d+)",
        "",
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(
        r"\(\s*EV-\d+(?:\s*,\s*EV-\d+)*\s*\)",
        "",
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(r"\s+([,.;:!?])", r"\1", normalized)
    return " ".join(normalized.split())


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

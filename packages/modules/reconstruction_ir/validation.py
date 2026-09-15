from __future__ import annotations

from collections import Counter
from typing import Any


class ReconstructionIRError(ValueError):
    """The Reconstruction IR is internally inconsistent or incomplete."""


def validate_reconstruction_ir(ir: dict[str, Any]) -> dict[str, Any]:
    """Validate identifiers, references, evidence traceability, and completeness."""
    collections = {
        "evidence": (ir["evidence"], "evidence_id"),
        "requirements": (ir["requirements"], "requirement_id"),
        "modules": (ir["modules"], "module_id"),
        "symbols": (ir["symbols"], "symbol_id"),
        "behaviors": (ir["behaviors"], "behavior_id"),
        "entry points": (ir["entry_points"], "entry_point_id"),
        "dependencies": (ir["dependencies"], "dependency_id"),
        "configurations": (ir["configurations"], "configuration_id"),
        "gaps": (ir["gaps"], "gap_id"),
    }
    identifiers: dict[str, set[str]] = {}
    for label, (items, field) in collections.items():
        values = [item[field] for item in items]
        duplicates = sorted(value for value, count in Counter(values).items() if count > 1)
        if duplicates:
            raise ReconstructionIRError(
                f"Reconstruction IR repeats {label}: {', '.join(duplicates)}"
            )
        identifiers[label] = set(values)

    evidence_ids = identifiers["evidence"]
    requirement_ids = identifiers["requirements"]
    module_ids = identifiers["modules"]
    symbol_ids = identifiers["symbols"]
    behavior_ids = identifiers["behaviors"]
    entry_point_ids = identifiers["entry points"]
    addressable_ids = set().union(
        requirement_ids,
        module_ids,
        symbol_ids,
        behavior_ids,
        entry_point_ids,
        identifiers["configurations"],
    )

    for requirement in ir["requirements"]:
        expected_category = requirement["requirement_id"].partition("-")[0]
        if requirement["category"] != expected_category:
            raise ReconstructionIRError(
                f"Requirement {requirement['requirement_id']} has category "
                f"{requirement['category']!r}, expected {expected_category!r}"
            )
        _require_refs(requirement, "evidence_ids", evidence_ids)

    for module in ir["modules"]:
        _require_refs(module, "evidence_ids", evidence_ids)

    for symbol in ir["symbols"]:
        _require_refs(symbol, "module_id", module_ids)
        _require_refs(symbol, "requirement_ids", requirement_ids)
        _require_refs(symbol, "evidence_ids", evidence_ids)
        if symbol["visibility"] == "public" and not symbol["requirement_ids"]:
            raise ReconstructionIRError(
                f"Public symbol {symbol['symbol_id']} has no requirements"
            )

    for behavior in ir["behaviors"]:
        _require_refs(behavior, "requirement_ids", requirement_ids)
        _require_refs(behavior, "subject_symbol_ids", symbol_ids)
        _require_refs(behavior, "evidence_ids", evidence_ids)
        _require_unique_nested(behavior, "observations", "observation_id")

    for entry_point in ir["entry_points"]:
        _require_refs(entry_point, "module_id", module_ids)
        _require_refs(entry_point, "symbol_ids", symbol_ids)
        _require_refs(entry_point, "behavior_ids", behavior_ids)
        _require_refs(entry_point, "evidence_ids", evidence_ids)

    dependency_nodes = module_ids | symbol_ids
    for dependency in ir["dependencies"]:
        _require_refs(dependency, "source_id", dependency_nodes)
        _require_refs(dependency, "target_id", dependency_nodes)
        _require_refs(dependency, "evidence_ids", evidence_ids)
        if dependency["source_id"] == dependency["target_id"]:
            raise ReconstructionIRError(
                f"Dependency {dependency['dependency_id']} references itself"
            )

    for configuration in ir["configurations"]:
        _require_refs(configuration, "requirement_ids", requirement_ids)
        _require_refs(configuration, "behavior_ids", behavior_ids)
        _require_refs(configuration, "evidence_ids", evidence_ids)

    packaging = ir["packaging"]
    _require_refs(packaging, "entry_point_ids", entry_point_ids)
    _require_refs(packaging, "evidence_ids", evidence_ids)
    for dependency in packaging["runtime_dependencies"]:
        _require_refs(dependency, "evidence_ids", evidence_ids)

    for gap in ir["gaps"]:
        _require_refs(gap, "scope_ids", addressable_ids)
        _require_refs(gap, "evidence_ids", evidence_ids)

    coverage = reconstruction_ir_coverage(ir)
    blocking = {
        key: value
        for key, value in coverage.items()
        if key
        in {
            "unmapped_requirement_ids",
            "unobserved_requirement_ids",
            "unexercised_public_symbol_ids",
            "unexercised_entry_point_ids",
        }
        and value
    }
    if blocking:
        rendered = "; ".join(
            f"{key}={','.join(value)}" for key, value in sorted(blocking.items())
        )
        raise ReconstructionIRError(f"Reconstruction IR is incomplete: {rendered}")
    return ir


def reconstruction_ir_coverage(ir: dict[str, Any]) -> dict[str, Any]:
    """Return deterministic completeness metrics without inventing missing facts."""
    requirement_ids = {item["requirement_id"] for item in ir["requirements"]}
    mapped_requirements = {
        requirement_id
        for collection in (ir["symbols"], ir["behaviors"], ir["configurations"])
        for item in collection
        for requirement_id in item["requirement_ids"]
    }
    observed_requirements = {
        requirement_id
        for behavior in ir["behaviors"]
        for requirement_id in behavior["requirement_ids"]
    }
    exercised_symbols = {
        symbol_id
        for behavior in ir["behaviors"]
        for symbol_id in behavior["subject_symbol_ids"]
    } | {
        symbol_id for entry in ir["entry_points"] for symbol_id in entry["symbol_ids"]
    }
    gap_scopes = {scope_id for gap in ir["gaps"] for scope_id in gap["scope_ids"]}
    public_symbols = {
        item["symbol_id"] for item in ir["symbols"] if item["visibility"] == "public"
    }
    entry_point_ids = {item["entry_point_id"] for item in ir["entry_points"]}
    exercised_entry_points = {
        item["entry_point_id"] for item in ir["entry_points"] if item["behavior_ids"]
    }
    return {
        "requirement_count": len(requirement_ids),
        "mapped_requirement_count": len(mapped_requirements & requirement_ids),
        "observed_requirement_count": len(observed_requirements & requirement_ids),
        "public_symbol_count": len(public_symbols),
        "exercised_public_symbol_count": len(public_symbols & exercised_symbols),
        "entry_point_count": len(entry_point_ids),
        "exercised_entry_point_count": len(exercised_entry_points),
        "unmapped_requirement_ids": sorted(
            requirement_ids - mapped_requirements - gap_scopes
        ),
        "unobserved_requirement_ids": sorted(
            requirement_ids - observed_requirements - gap_scopes
        ),
        "unexercised_public_symbol_ids": sorted(
            public_symbols - exercised_symbols - gap_scopes
        ),
        "unexercised_entry_point_ids": sorted(
            entry_point_ids - exercised_entry_points - gap_scopes
        ),
        "gap_count": len(ir["gaps"]),
        "blocking_gap_count": sum(gap["impact"] == "blocking" for gap in ir["gaps"]),
    }


def _require_refs(item: dict[str, Any], field: str, allowed: set[str]) -> None:
    value = item[field]
    values = [value] if isinstance(value, str) else value
    identity = next(
        (candidate for key, candidate in item.items() if key.endswith("_id")),
        "item",
    )
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise ReconstructionIRError(
            f"{identity} references unknown {field}: {', '.join(unknown)}"
        )
    duplicates = sorted(name for name, count in Counter(values).items() if count > 1)
    if duplicates:
        raise ReconstructionIRError(
            f"{identity} repeats {field}: {', '.join(duplicates)}"
        )


def _require_unique_nested(item: dict[str, Any], field: str, id_field: str) -> None:
    values = [nested[id_field] for nested in item[field]]
    duplicates = sorted(value for value, count in Counter(values).items() if count > 1)
    if duplicates:
        raise ReconstructionIRError(
            f"{item['behavior_id']} repeats observations: {', '.join(duplicates)}"
        )

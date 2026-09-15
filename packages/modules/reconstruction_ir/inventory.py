from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import Any

from packages.modules.boundary import AliasMap

_KINDS = ("module", "class", "function", "entry_point", "configuration")
_REPORTS = ("documentation", "code_facts", "behavior")
_PRIORITIES = ("critical", "high", "medium", "low")
PLANNING_PRIORITY_LIMIT = 12

_TEST_SEGMENTS = frozenset({"test", "tests", "spec", "specs", "__tests__"})
_EXAMPLE_SEGMENTS = frozenset(
    {"example", "examples", "sample", "samples", "demo", "demos", "tutorials"}
)
_DOCUMENTATION_SEGMENTS = frozenset({"doc", "docs", "documentation"})
_TOOLING_SEGMENTS = frozenset({".github", ".gitlab", "ci", "build", "tools"})
_VENDORED_SEGMENTS = frozenset(
    {"vendor", "vendored", "third_party", "third-party", "node_modules"}
)
_PACKAGING_FILES = frozenset(
    {
        "cargo.toml",
        "composer.json",
        "dockerfile",
        "gemfile",
        "go.mod",
        "makefile",
        "package.json",
        "pom.xml",
        "pyproject.toml",
        "requirements.txt",
        "setup.cfg",
        "setup.py",
    }
)


def build_dirty_inventory(
    code_index: dict[str, Any],
    alias_map: AliasMap,
    *,
    documentation_report: dict[str, Any] | str | None = None,
    code_facts_report: dict[str, Any] | str | None = None,
    behavior_report: dict[str, Any] | str | None = None,
) -> dict[str, Any]:
    """Inventory every indexed reconstruction unit and audit report coverage.

    This is deliberately a dirty-side artifact: source paths and names are retained so
    later extraction passes can resolve omissions. It must be neutralised and approved
    by Border before any part of it is added to a Clean handoff.
    """
    reports = {
        "documentation": _normalise_report(documentation_report, "documentation"),
        "code_facts": _normalise_report(code_facts_report, "code_facts"),
        "behavior": _normalise_report(behavior_report, "behavior"),
    }
    report_locations = {
        name: _report_locations(report) for name, report in reports.items()
    }
    report_files = {
        name: _report_files(report) | {location[0] for location in report_locations[name]}
        for name, report in reports.items()
    }
    exported_names = _public_export_names(code_index)

    raw_items = _raw_inventory_items(code_index)
    items = []
    for raw in sorted(raw_items, key=_item_sort_key):
        evidence = raw.pop("evidence", None)
        definition_scope = raw.pop("_definition_scope", None)
        location = _evidence_location(evidence)
        source_path = raw["source_path"]
        covered_by = [
            name
            for name in _REPORTS
            if (
                source_path in report_files[name]
                if raw["kind"] == "module"
                else _location_is_covered(location, report_locations[name])
            )
        ]
        item_key = "|".join(
            str(raw.get(field) or "")
            for field in (
                "kind",
                "source_path",
                "line_start",
                "line_end",
                "qualified_name",
                "source_name",
            )
        )
        evidence_id = None
        if location is not None:
            evidence_id = alias_map.evidence_id(*location)
        classification = _classify(raw, exported_names, definition_scope)
        items.append(
            {
                "inventory_id": _stable_id("INV", item_key),
                **raw,
                "evidence_id": evidence_id,
                "covered_by": covered_by,
                **classification,
            }
        )

    issues = _inventory_issues(code_index)
    return {
        "schema_version": 2,
        "artifact_kind": "dirty-reconstruction-inventory",
        "items": items,
        "issues": issues,
        "coverage": _coverage(items, len(issues)),
    }


def build_planning_priorities(
    inventory: dict[str, Any] | None,
    evidence_catalogue: list[dict[str, Any]],
    stage: str,
    limit: int = PLANNING_PRIORITY_LIMIT,
) -> list[dict[str, Any]]:
    """Return a bounded, neutral priority queue for one planning stage."""
    if not inventory or stage == "documentation" or limit < 1:
        return []
    return _planning_priority_candidates(inventory, evidence_catalogue, stage)[:limit]


def build_planning_priority_batches(
    inventory: dict[str, Any] | None,
    evidence_catalogue: list[dict[str, Any]],
    stage: str,
    batch_size: int = PLANNING_PRIORITY_LIMIT,
) -> list[list[dict[str, Any]]]:
    """Partition every required contract into bounded deterministic windows."""
    if not inventory or stage == "documentation" or batch_size < 1:
        return []
    priorities = _planning_priority_candidates(inventory, evidence_catalogue, stage)
    return [
        priorities[offset : offset + batch_size]
        for offset in range(0, len(priorities), batch_size)
    ]


def _planning_priority_candidates(
    inventory: dict[str, Any],
    evidence_catalogue: list[dict[str, Any]],
    stage: str,
) -> list[dict[str, Any]]:
    catalogue_ids = {
        entry.get("evidence_id")
        for entry in evidence_catalogue
        if isinstance(entry, dict)
    }
    candidates = []
    for item in inventory.get("items", []):
        if not isinstance(item, dict) or item.get("evidence_id") not in catalogue_ids:
            continue
        behavioral_evidence = (
            stage == "behavior"
            and item.get("source_scope") in {"test", "example"}
            and item.get("kind") in {"class", "function", "entry_point"}
        )
        if not item.get("reconstruction_target") and not behavioral_evidence:
            continue
        candidates.append(
            {
                "inventory_id": item["inventory_id"],
                "evidence_id": item["evidence_id"],
                "priority": "critical" if behavioral_evidence else item["priority"],
                "contract_role": item["contract_role"],
                "source_scope": item["source_scope"],
                "planning_role": (
                    "behavioral_evidence" if behavioral_evidence else "reconstruction_contract"
                ),
                "required_output_fields": _required_output_fields(
                    stage,
                    "behavioral_evidence"
                    if behavioral_evidence
                    else "reconstruction_contract",
                    item["contract_role"],
                ),
                "required": True,
                "_source_path": item["source_path"],
            }
        )

    priority_rank = {name: position for position, name in enumerate(_PRIORITIES)}
    candidates.sort(
        key=lambda item: (
            priority_rank[item["priority"]],
            item["planning_role"] != "behavioral_evidence",
            item["_source_path"].casefold(),
            item["inventory_id"],
        )
    )
    selected = _diverse_prefix(candidates, len(candidates))
    return [
        {key: value for key, value in item.items() if not key.startswith("_")}
        for item in selected
    ]


def _required_output_fields(
    stage: str, planning_role: str, contract_role: str
) -> list[str]:
    if stage == "code_facts":
        return ["symbols"]
    if stage == "behavior":
        if planning_role == "behavioral_evidence":
            return [
                "behaviors",
                "edge_cases",
                "error_handling",
                "test_candidates",
                "specification_requirements",
            ]
        if contract_role == "configuration":
            return ["behaviors", "specification_requirements"]
        return ["behaviors", "edge_cases", "error_handling", "test_candidates"]
    if stage == "specification":
        if contract_role == "configuration":
            return ["configuration", "functional_requirements"]
        if contract_role == "entry_point":
            return [
                "components_and_interfaces",
                "functional_requirements",
                "behavioral_requirements",
                "acceptance_criteria",
            ]
        return [
            "components_and_interfaces",
            "functional_requirements",
            "behavioral_requirements",
            "error_handling",
            "acceptance_criteria",
        ]
    return []


def _diverse_prefix(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Select across source units before taking a second item from one unit."""
    selected = []
    remaining = list(items)
    while remaining and len(selected) < limit:
        seen_paths: set[str] = set()
        deferred = []
        for item in remaining:
            path = item["_source_path"]
            if path in seen_paths or len(selected) >= limit:
                deferred.append(item)
                continue
            selected.append(item)
            seen_paths.add(path)
        remaining = deferred
    return selected


def _raw_inventory_items(code_index: dict[str, Any]) -> list[dict[str, Any]]:
    config_paths = {
        item.get("file")
        for item in code_index.get("configs", [])
        if isinstance(item, dict)
    }
    items: list[dict[str, Any]] = []
    modules = code_index.get("modules")
    if isinstance(modules, list):
        for source in modules:
            if not isinstance(source, dict):
                continue
            evidence = source.get("evidence")
            evidence_data = evidence if isinstance(evidence, dict) else {}
            source_path = source.get("file") or evidence_data.get("file")
            if not isinstance(source_path, str) or source_path in config_paths:
                continue
            items.append(
                _item(
                    "module",
                    source_path,
                    line_start=evidence_data.get("line_start"),
                    line_end=evidence_data.get("line_end"),
                    source_name=PurePosixPath(source_path).stem,
                    evidence=evidence,
                )
            )
    else:
        # Older indexes represented modules only as path strings. Keep accepting
        # those artifacts, though they cannot provide an evidence-backed contract.
        for source_path in code_index.get("files_indexed", []):
            if isinstance(source_path, str) and source_path not in config_paths:
                items.append(
                    _item(
                        "module",
                        source_path,
                        source_name=PurePosixPath(source_path).stem,
                    )
                )

    for collection, kind in (
        ("classes", "class"),
        ("functions", "function"),
        ("entrypoints", "entry_point"),
        ("configs", "configuration"),
    ):
        for source in code_index.get(collection, []):
            if not isinstance(source, dict):
                continue
            evidence = source.get("evidence")
            evidence_data = evidence if isinstance(evidence, dict) else {}
            evidence_file = evidence_data.get("file")
            source_path = source.get("file") or evidence_file
            if not isinstance(source_path, str):
                continue
            source_name = source.get("name")
            if kind in {"entry_point", "configuration"}:
                source_name = source.get("kind")
            items.append(
                _item(
                    kind,
                    source_path,
                    line_start=source.get("line_start")
                    if isinstance(source.get("line_start"), int)
                    else evidence_data.get("line_start"),
                    line_end=source.get("line_end")
                    if isinstance(source.get("line_end"), int)
                    else evidence_data.get("line_end"),
                    source_name=_optional_string(source_name),
                    qualified_name=_optional_string(source.get("qualified_name")),
                    signature=_source_signature(source, kind),
                    definition_scope=_optional_string(source.get("definition_scope")),
                    evidence=evidence,
                )
            )
    return _deduplicate(items)


def _item(
    kind: str,
    source_path: str,
    *,
    line_start: Any = None,
    line_end: Any = None,
    source_name: str | None = None,
    qualified_name: str | None = None,
    signature: str | None = None,
    definition_scope: str | None = None,
    evidence: Any = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "source_path": source_path.replace("\\", "/"),
        "line_start": line_start if isinstance(line_start, int) else None,
        "line_end": line_end if isinstance(line_end, int) else None,
        "source_name": source_name,
        "qualified_name": qualified_name,
        "signature": signature,
        "_definition_scope": definition_scope,
        "evidence": evidence,
    }


def _source_signature(source: dict[str, Any], kind: str) -> str | None:
    signature = _optional_string(source.get("signature"))
    if signature:
        return signature
    if kind == "function" and isinstance(source.get("args"), list):
        args = ", ".join(str(value) for value in source["args"])
        name = source.get("qualified_name") or source.get("name") or "<anonymous>"
        return f"{name}({args})"
    if kind == "class" and isinstance(source.get("bases"), list):
        bases = ", ".join(str(value) for value in source["bases"])
        return f"class {source.get('name') or '<anonymous>'}({bases})"
    return None


def _report_locations(report: dict[str, Any]) -> set[tuple[str, int | None, int | None]]:
    locations: set[tuple[str, int | None, int | None]] = set()

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            location = _evidence_location(node.get("evidence"))
            if location is not None:
                locations.add(location)
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(report)
    return locations


def _normalise_report(
    report: dict[str, Any] | str | None, report_name: str
) -> dict[str, Any]:
    """Accept the decoded test representation and the agents' JSON-text representation."""
    if report is None:
        return {}
    if isinstance(report, dict):
        return report
    if not isinstance(report, str):
        raise TypeError(f"{report_name} report must be a JSON object or JSON text")
    try:
        payload = json.loads(report)
    except json.JSONDecodeError as error:
        raise ValueError(f"{report_name} report is not valid JSON: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{report_name} report must decode to a JSON object")
    return payload


def _report_files(report: dict[str, Any]) -> set[str]:
    files: set[str] = set()
    for key, value in report.items():
        if key.endswith("files_read") and isinstance(value, list):
            files.update(path.replace("\\", "/") for path in value if isinstance(path, str))
    return files


def _evidence_location(evidence: Any) -> tuple[str, int | None, int | None] | None:
    if not isinstance(evidence, dict) or not isinstance(evidence.get("file"), str):
        return None
    start = evidence.get("line_start")
    end = evidence.get("line_end")
    return (
        evidence["file"].replace("\\", "/"),
        start if isinstance(start, int) else None,
        end if isinstance(end, int) else None,
    )


def _location_is_covered(
    candidate: tuple[str, int | None, int | None] | None,
    report_locations: set[tuple[str, int | None, int | None]],
) -> bool:
    if candidate is None:
        return False
    path, start, end = candidate
    for report_path, report_start, report_end in report_locations:
        if path != report_path:
            continue
        if None in (start, end, report_start, report_end):
            return True
        if start <= report_end and report_start <= end:
            return True
    return False


def _inventory_issues(code_index: dict[str, Any]) -> list[dict[str, Any]]:
    issues = []
    for collection, issue_type in (
        ("files_skipped", "skipped"),
        ("errors", "index_error"),
    ):
        for source in code_index.get(collection, []):
            if not isinstance(source, dict):
                continue
            path = str(source.get("file") or "<unknown>").replace("\\", "/")
            detail = str(
                source.get("reason")
                or source.get("message")
                or source.get("error_type")
                or "unspecified"
            )
            key = f"{issue_type}|{path}|{detail}"
            issues.append(
                {
                    "issue_id": _stable_id("ISSUE", key),
                    "source_path": path,
                    "issue_type": issue_type,
                    "detail": detail,
                }
            )
    return sorted(issues, key=lambda item: (item["source_path"], item["issue_type"]))


def _coverage(items: list[dict[str, Any]], issue_count: int) -> dict[str, Any]:
    by_kind = {}
    for kind in _KINDS:
        matching = [item for item in items if item["kind"] == kind]
        covered = sum(bool(item["covered_by"]) for item in matching)
        by_kind[kind] = {
            "total": len(matching),
            "covered": covered,
            "uncovered": len(matching) - covered,
        }
    by_priority = {}
    for priority in _PRIORITIES:
        matching = [item for item in items if item["priority"] == priority]
        covered = sum(bool(item["covered_by"]) for item in matching)
        by_priority[priority] = {
            "total": len(matching),
            "covered": covered,
            "uncovered": len(matching) - covered,
        }
    covered_count = sum(bool(item["covered_by"]) for item in items)
    total_count = len(items)
    targets = [item for item in items if item["reconstruction_target"]]
    covered_targets = sum(bool(item["covered_by"]) for item in targets)
    return {
        "total_count": total_count,
        "covered_count": covered_count,
        "uncovered_count": total_count - covered_count,
        "coverage_percent": round(100 * covered_count / total_count, 2)
        if total_count
        else 100.0,
        "by_kind": by_kind,
        "by_priority": by_priority,
        "reconstruction_target_count": len(targets),
        "covered_reconstruction_target_count": covered_targets,
        "uncovered_reconstruction_target_count": len(targets) - covered_targets,
        "reconstruction_target_coverage_percent": round(
            100 * covered_targets / len(targets), 2
        )
        if targets
        else 100.0,
        "uncovered_inventory_ids": [
            item["inventory_id"] for item in items if not item["covered_by"]
        ],
        "uncovered_reconstruction_target_ids": [
            item["inventory_id"]
            for item in targets
            if not item["covered_by"]
        ],
        "issue_count": issue_count,
    }


def _classify(
    item: dict[str, Any], exported_names: set[str], definition_scope: str | None = None
) -> dict[str, Any]:
    scope, scope_reason = _source_scope(item["source_path"])
    kind = item["kind"]
    name = item["source_name"] or ""
    qualified_name = item["qualified_name"] or name
    owner = qualified_name.rpartition(".")[0]
    owner_is_internal = any(
        part.startswith("_") for part in owner.split(".") if part
    )
    internal_name = name.startswith("_")
    protocol_name = name.startswith("__") and name.endswith("__")
    module_stem = PurePosixPath(item["source_path"]).stem
    private_module = module_stem.startswith("_") and module_stem != "__init__"
    explicitly_exported = name in exported_names or qualified_name in exported_names

    if scope in {"test", "example", "documentation", "vendored"}:
        role = "excluded"
        priority = "low"
        reasons = [scope_reason, f"{scope} units are behavioral evidence, not output contracts"]
    elif definition_scope == "function":
        role = "internal"
        priority = "low"
        reasons = [
            scope_reason,
            "definition is local to a function and is not a module or class contract",
        ]
    elif kind == "entry_point":
        role = "entry_point"
        priority = "critical"
        reasons = [scope_reason, "executable entry points define directly observable behavior"]
    elif kind == "configuration":
        role = "configuration"
        file_name = PurePosixPath(item["source_path"]).name.casefold()
        priority = "critical" if _is_packaging_file(file_name) else "high"
        reasons = [scope_reason, "configuration controls build or runtime behavior"]
        if priority == "critical":
            reasons.append("recognized ecosystem packaging contract")
    elif explicitly_exported:
        role = "public_candidate"
        priority = "high"
        reasons = [scope_reason, "symbol is re-exported from a package surface module"]
    elif private_module or owner_is_internal or (internal_name and not protocol_name):
        role = "internal"
        priority = "low"
        reasons = [scope_reason, "private module or identifier marks an internal candidate"]
    elif kind == "function" and protocol_name:
        role = "protocol"
        priority = "high"
        reasons = [scope_reason, "protocol method can affect externally observable behavior"]
    elif kind in {"class", "function"}:
        role = "public_candidate"
        priority = "high"
        reasons = [scope_reason, "non-private production symbol is a public-contract candidate"]
    elif kind == "module" and name in {"__init__", "index", "mod"}:
        role = "public_candidate"
        priority = "high"
        reasons = [scope_reason, "package surface module can define exports"]
    else:
        role = "support_module"
        priority = "medium"
        reasons = [scope_reason, "production module supports reconstructed contracts"]

    reconstruction_target = priority in {"critical", "high"} and role != "excluded"
    return {
        "source_scope": scope,
        "contract_role": role,
        "priority": priority,
        "reconstruction_target": reconstruction_target,
        "classification_reasons": reasons,
    }


def _source_scope(source_path: str) -> tuple[str, str]:
    path = PurePosixPath(source_path.replace("\\", "/"))
    parts = tuple(part.casefold() for part in path.parts)
    file_name = path.name.casefold()
    stem = path.stem.casefold()

    if any(part in _VENDORED_SEGMENTS for part in parts):
        return "vendored", "path is inside a vendored dependency tree"
    if any(part in _TEST_SEGMENTS for part in parts) or _is_test_file(file_name, stem):
        return "test", "path follows a test-source convention"
    if any(part in _EXAMPLE_SEGMENTS for part in parts):
        return "example", "path is inside an example or tutorial tree"
    if any(part in _DOCUMENTATION_SEGMENTS for part in parts):
        return "documentation", "path is inside a documentation tree"
    if any(part in _TOOLING_SEGMENTS for part in parts):
        return "tooling", "path is inside a build or repository-tooling tree"
    return "production", "path is part of the production candidate tree"


def _public_export_names(code_index: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for item in code_index.get("imports", []):
        if not isinstance(item, dict) or not isinstance(item.get("file"), str):
            continue
        path = PurePosixPath(item["file"].replace("\\", "/"))
        if path.stem.casefold() not in {"__init__", "index", "mod"}:
            continue
        for field in ("name", "alias"):
            value = item.get(field)
            if isinstance(value, str) and value and value != "*":
                names.add(value)
    return names


def _is_test_file(file_name: str, stem: str) -> bool:
    return (
        stem.startswith("test_")
        or stem.endswith("_test")
        or ".test." in file_name
        or ".spec." in file_name
    )


def _is_packaging_file(file_name: str) -> bool:
    return (
        file_name in _PACKAGING_FILES
        or (
            file_name.startswith("requirements") and file_name.endswith(".txt")
        )
        or file_name.startswith("build.gradle")
    )


def _item_sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
    return (
        _KINDS.index(item["kind"]),
        item["source_path"].casefold(),
        item["line_start"] if item["line_start"] is not None else -1,
        item["qualified_name"] or item["source_name"] or "",
    )


def _deduplicate(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique = {}
    for item in items:
        key = (*_item_sort_key(item), item["line_end"])
        unique.setdefault(key, item)
    return list(unique.values())


def _stable_id(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12].upper()
    return f"{prefix}-{digest}"


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None

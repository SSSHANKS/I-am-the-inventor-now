"""Language-independent staging and regression checks for repair candidates."""
from __future__ import annotations

import tempfile
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from packages.modules.clean.workspace import CleanWorkspace


def repair_regressions(before: list[dict], after: list[dict]) -> list[str]:
    previous = defaultdict(list)
    candidate = defaultdict(list)
    for check in before:
        previous[check['name']].append(check)
    for check in after:
        candidate[check['name']].append(check)
    regressions = []
    for name, checks in previous.items():
        if all(item['status'] == 'pass' for item in checks) and (
            not candidate[name] or any(item['status'] != 'pass' for item in candidate[name])
        ):
            regressions.append(f'Previously passing check {name} no longer passes')
        if len(checks) > 1 or any(item['status'] != 'pass' for item in checks):
            for item in checks:
                if item['status'] != 'pass':
                    continue
                paths = sorted(item.get('paths', []))
                matching = [check for check in candidate[name] if sorted(check.get('paths', [])) == paths]
                if not matching or any(check['status'] != 'pass' for check in matching):
                    regressions.append(f'Previously passing check {name} for {paths} no longer passes')
        # An aggregate can contain both passing and failing individual probes.
        # Compare stable diagnostic check IDs only for a previously executed suite.
        if name.endswith('behavior-probes') and all(
            item['status'] == 'pass' or (item['status'] == 'fail' and item.get('diagnostics')) for item in checks
        ):
            if not candidate[name] or any(
                item['status'] not in {'pass', 'fail'}
                or (item['status'] == 'fail' and not item.get('diagnostics'))
                for item in candidate[name]
            ):
                regressions.append(f'Previously executed behavior suite {name} no longer has comparable results')
            old_ids = {d['check_id'] for item in checks for d in item.get('diagnostics', [])}
            new_ids = {d['check_id'] for item in candidate[name] for d in item.get('diagnostics', [])}
            for check_id in sorted(new_ids - old_ids):
                regressions.append(f'Previously passing behavior probe now fails: {check_id}')
    return regressions


def repair_improvements(before: list[dict], after: list[dict]) -> list[str]:
    """Return stable failure obligations that the candidate demonstrably resolved."""
    candidate = defaultdict(list)
    for check in after:
        candidate[check["name"]].append(check)
    resolved: list[str] = []
    for check in before:
        if check["status"] != "fail":
            continue
        matching = candidate[check["name"]]
        diagnostics = check.get("diagnostics") or []
        if diagnostics:
            comparable = [
                item for item in matching
                if item["status"] == "pass"
                or (item["status"] == "fail" and item.get("diagnostics"))
            ]
            remaining = {
                diagnostic["check_id"]
                for item in comparable
                if item["status"] == "fail"
                for diagnostic in item.get("diagnostics", [])
            }
            for diagnostic in diagnostics:
                check_id = diagnostic["check_id"]
                if comparable and check_id not in remaining:
                    resolved.append(check_id)
            continue
        paths = sorted(check.get("paths", []))
        same_scope = [
            item for item in matching
            if sorted(item.get("paths", [])) == paths
        ] or (matching if len(matching) == 1 else [])
        if any(item["status"] == "pass" for item in same_scope):
            resolved.append(f"{check['name']}:{','.join(paths)}")
    return resolved


def evaluate_repair_candidate(
    workspace: CleanWorkspace,
    replacements: list[dict[str, str]],
    allowed_paths: set[str],
    before_checks: list[dict[str, Any]],
    validator: Callable[[CleanWorkspace], list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Validate a disposable copy; never publish or modify the working project here."""
    with tempfile.TemporaryDirectory(prefix='clean-candidate-') as temporary:
        candidate = CleanWorkspace(Path(temporary) / 'workspace',
            max_files=workspace.max_files, max_file_bytes=workspace.max_file_bytes,
            max_total_bytes=workspace.max_total_bytes)
        candidate.write_generated_files([
            {'path': path, 'content': workspace.read_generated_file(path)}
            for path in workspace.list_generated_files()
        ])
        candidate.write_generated_files(replacements, allowed_paths=allowed_paths)
        before_validation = {path: candidate.read_generated_file(path) for path in candidate.list_generated_files()}
        checks = validator(candidate)
        regressions = repair_regressions(before_checks, checks)
        if any(check["status"] == "fail" for check in before_checks):
            improvements = repair_improvements(before_checks, checks)
            if not improvements:
                regressions.append(
                    "Repair candidate does not resolve any previously failing validation obligation"
                )
        after_validation = {path: candidate.read_generated_file(path) for path in candidate.list_generated_files()}
        if before_validation != after_validation:
            regressions.append('Validation modified the repair candidate; validated content cannot be published')
        return checks, regressions


def select_non_regressing_repair_subset(
    workspace: CleanWorkspace,
    replacements: list[dict[str, str]],
    allowed_paths: set[str],
    before_checks: list[dict[str, Any]],
    validator: Callable[[CleanWorkspace], list[dict[str, Any]]],
) -> tuple[list[dict[str, str]], list[dict[str, Any]] | None]:
    """Greedily retain independently safe progress from a rejected batch.

    Every accepted prefix is revalidated as one transaction against the unchanged
    workspace. This is deliberately linear rather than an exponential subset search.
    """
    accepted: list[dict[str, str]] = []
    accepted_checks: list[dict[str, Any]] | None = None
    for replacement in sorted(replacements, key=lambda item: item["path"]):
        trial = [*accepted, replacement]
        checks, regressions = evaluate_repair_candidate(
            workspace,
            trial,
            allowed_paths,
            before_checks,
            validator,
        )
        if regressions:
            continue
        accepted = trial
        accepted_checks = checks
    return accepted, accepted_checks

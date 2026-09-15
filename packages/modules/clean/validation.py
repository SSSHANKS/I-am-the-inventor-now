from __future__ import annotations

import ast
import json
import tomllib
from pathlib import PurePosixPath
from typing import Any

from packages.modules.clean.python_contracts import find_python_contract_issues
from packages.modules.clean.python_symbols import find_python_symbol_issues
from packages.modules.clean.workspace import CleanWorkspace, WorkspaceError


def validate_project(
    workspace: CleanWorkspace,
    plan: dict[str, Any],
    *,
    syntax_checks: bool = True,
) -> list[dict[str, Any]]:
    """Run deterministic, non-executing checks over Clean-generated text files."""
    planned = {item["path"] for item in plan["files"]}
    actual = set(workspace.list_generated_files())
    checks: list[dict[str, Any]] = []

    missing = sorted(planned - actual)
    checks.append(_check("planned-files-present", not missing, _message(missing, "Missing"), missing))
    extra = sorted(actual - planned)
    checks.append(_check("no-unplanned-files", not extra, _message(extra, "Unplanned"), extra))

    entry_paths = sorted({item["path"] for item in plan["entry_points"]})
    absent_entries = [path for path in entry_paths if path not in actual]
    checks.append(
        _check("entry-points-present", not absent_entries, _message(absent_entries, "Missing"), absent_entries)
    )

    utf8_failures: list[str] = []
    contents: dict[str, str] = {}
    for path in sorted(actual):
        try:
            contents[path] = workspace.read_generated_file(path)
        except WorkspaceError:
            utf8_failures.append(path)
    checks.append(_check("utf8-text", not utf8_failures, _message(utf8_failures, "Invalid"), utf8_failures))

    if not syntax_checks:
        checks.append(
            {
                "name": "syntax",
                "status": "skipped",
                "message": "Syntax validation disabled by Clean CLI option",
                "paths": [],
            }
        )
        checks.append(
            {
                "name": "python-symbol-coherence",
                "status": "skipped",
                "message": "Blocked because syntax validation is disabled",
                "paths": [],
            }
        )
        checks.append(
            {
                "name": "python-contracts",
                "status": "skipped",
                "message": "Blocked because syntax validation is disabled",
                "paths": [],
            }
        )
        return checks

    syntax_failures: list[str] = []
    for path, content in contents.items():
        suffix = PurePosixPath(path).suffix.casefold()
        try:
            if suffix == ".py":
                ast.parse(content, filename=path)
            elif suffix == ".json":
                json.loads(content)
            elif suffix == ".toml":
                tomllib.loads(content)
        except (SyntaxError, ValueError, tomllib.TOMLDecodeError):
            syntax_failures.append(path)
    checks.append(_check("syntax", not syntax_failures, _message(syntax_failures, "Invalid"), syntax_failures))
    python_contents = {
        path: content
        for path, content in contents.items()
        if PurePosixPath(path).suffix.casefold() == ".py"
    }
    if syntax_failures:
        checks.append(
            {
                "name": "python-symbol-coherence",
                "status": "skipped",
                "message": "Blocked until syntax checks pass",
                "paths": [],
            }
        )
        checks.append(
            {
                "name": "python-contracts",
                "status": "skipped",
                "message": "Blocked until syntax checks pass",
                "paths": [],
            }
        )
    elif not python_contents:
        checks.append(
            {
                "name": "python-symbol-coherence",
                "status": "skipped",
                "message": "No Python files were generated",
                "paths": [],
            }
        )
        checks.append(
            {
                "name": "python-contracts",
                "status": "skipped",
                "message": "No Python files were generated",
                "paths": [],
            }
        )
    else:
        issues = find_python_symbol_issues(python_contents)
        paths = sorted({path for issue in issues for path in issue.paths})
        message = "Passed"
        if issues:
            rendered = "; ".join(issue.message for issue in issues[:20])
            omitted = len(issues) - 20
            if omitted:
                rendered = f"{rendered}; ... {omitted} more issue(s)"
            message = rendered
        checks.append(_check("python-symbol-coherence", not issues, message, paths))
        contracts = plan.get("symbol_contracts", [])
        if not contracts:
            checks.append(
                {
                    "name": "python-contracts",
                    "status": "skipped",
                    "message": "No Python symbol contracts were declared",
                    "paths": [],
                }
            )
        else:
            contract_issues = find_python_contract_issues(python_contents, plan)
            contract_paths = sorted(
                {path for issue in contract_issues for path in issue.paths}
            )
            contract_message = "Passed"
            if contract_issues:
                rendered = "; ".join(issue.message for issue in contract_issues[:20])
                omitted = len(contract_issues) - 20
                if omitted:
                    rendered = f"{rendered}; ... {omitted} more issue(s)"
                contract_message = rendered
            checks.append(
                _check(
                    "python-contracts",
                    not contract_issues,
                    contract_message,
                    contract_paths,
                )
            )
    return checks


def failed_paths(checks: list[dict[str, Any]]) -> set[str]:
    return {
        path
        for check in checks
        if check["status"] == "fail"
        for path in check["paths"]
    }


def checks_passed(checks: list[dict[str, Any]]) -> bool:
    return all(check["status"] != "fail" for check in checks)


def _check(name: str, passed: bool, message: str, paths: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "status": "pass" if passed else "fail",
        "message": "Passed" if passed else message,
        "paths": paths,
    }


def _message(paths: list[str], prefix: str) -> str:
    return f"{prefix} generated paths: {', '.join(paths)}" if paths else "Passed"

from __future__ import annotations

import re
import tomllib
from pathlib import PurePosixPath
from typing import Any

from packages.modules.clean.diagnostics import CleanDiagnostic
from packages.modules.clean.python_contract_paths import (
    python_contract_module,
    python_contract_target,
)
from packages.modules.clean.workspace import CleanWorkspace, WorkspaceError

_NO_BUILD_SYSTEMS = frozenset({"none", "no-build", "stdlib", "not-applicable"})
_DEPENDENCY_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_BACKEND_MARKERS = {
    "setuptools": "setuptools",
    "hatch": "hatch",
    "hatchling": "hatchling",
    "flit": "flit",
    "poetry": "poetry",
    "pdm": "pdm",
}


def validate_python_packaging(
    workspace: CleanWorkspace,
    architecture: dict[str, Any],
    manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    """Validate Python module layout, build metadata, dependencies, and CLI wiring."""
    profile = architecture["project_profile"]
    checks = [_layout_check(architecture, manifest)]
    pyproject_path = _pyproject_path(manifest)
    build_system = str(profile["build_system"]).strip().casefold()
    packaging_required = build_system not in _NO_BUILD_SYSTEMS
    if pyproject_path is None:
        if packaging_required:
            checks.append(
                _failed_check(
                    "python-packaging",
                    "The architecture declares a build system but the manifest has no pyproject.toml",
                    [],
                    expected=f"pyproject.toml configured for {profile['build_system']}",
                    actual="missing",
                    repair_hint="Add project metadata consistent with the declared layout.",
                )
            )
        else:
            checks.append(_skipped("python-packaging", "No build system is declared"))
        checks.append(
            _skipped(
                "python-dependencies",
                "No pyproject.toml dependency table is applicable",
            )
        )
        checks.append(
            _entry_point_check(architecture, manifest, pyproject=None, path=None)
        )
        return checks

    try:
        content = workspace.read_generated_file(pyproject_path)
        pyproject = tomllib.loads(content)
    except (WorkspaceError, tomllib.TOMLDecodeError) as exc:
        message = f"Cannot inspect {pyproject_path}: {type(exc).__name__}"
        checks.append(
            _failed_check(
                "python-packaging",
                message,
                [pyproject_path],
                actual=type(exc).__name__,
                repair_hint="Return valid UTF-8 TOML project metadata.",
            )
        )
        checks.append(_skipped("python-dependencies", "Packaging metadata is invalid"))
        checks.append(_skipped("python-entry-points", "Packaging metadata is invalid"))
        return checks

    checks.append(
        _packaging_check(
            pyproject,
            pyproject_path,
            profile,
            manifest,
            packaging_required=packaging_required,
        )
    )
    checks.append(_dependency_check(pyproject, pyproject_path, architecture))
    checks.append(
        _entry_point_check(
            architecture,
            manifest,
            pyproject=pyproject,
            path=pyproject_path,
        )
    )
    return checks


def python_manifest_readiness_issues(
    architecture: dict[str, Any],
    manifest: dict[str, Any],
) -> tuple[str, ...]:
    """Return packaging defects knowable before any file content is generated."""
    profile = architecture["project_profile"]
    issues: list[str] = []
    layout = _layout_check(architecture, manifest)
    if layout["status"] == "fail":
        issues.append(layout["message"])
    build_system = str(profile["build_system"]).strip().casefold()
    if build_system not in _NO_BUILD_SYSTEMS and _pyproject_path(manifest) is None:
        issues.append(
            "declared build system requires a root pyproject.toml metadata file"
        )
    return tuple(issues)


def _layout_check(
    architecture: dict[str, Any],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    profile = architecture["project_profile"]
    layout = str(profile["layout"]).strip().casefold()
    contracts = {item["contract_id"]: item for item in architecture["contracts"]}
    issues: list[str] = []
    paths: list[str] = []
    module_paths: dict[str, list[str]] = {}
    for task in manifest["files"]:
        if task["category"] != "source" or not task["path"].endswith(".py"):
            continue
        module_paths.setdefault(
            _module_name(task["path"], layout=layout), []
        ).append(task["path"])
    for module, providers in sorted(module_paths.items()):
        if len(providers) < 2:
            continue
        paths.extend(providers)
        issues.append(
            f"module {module!r} has multiple source paths: "
            + ", ".join(sorted(providers))
        )
    for task in manifest["files"]:
        if task["category"] != "source" or not task["path"].endswith(".py"):
            continue
        for contract_id in task["provides"]:
            contract = contracts[contract_id]
            expected_module = python_contract_module(
                contract, architecture["contracts"]
            )
            actual_module = _module_name(task["path"], layout=layout)
            if actual_module != expected_module:
                paths.append(task["path"])
                issues.append(
                    f"{contract_id} expects module {expected_module!r}, got {actual_module!r}"
                )
    if issues:
        return _failed_check(
            "python-package-layout",
            "; ".join(issues),
            sorted(set(paths)),
            expected=f"provider modules consistent with {profile['layout']!r} layout",
            actual="; ".join(issues),
            repair_hint=(
                "Move providers or correct the architecture contract module without "
                "changing the public symbol declaration."
            ),
        )
    return _passed("python-package-layout")


def _packaging_check(
    pyproject: dict[str, Any],
    path: str,
    profile: dict[str, Any],
    manifest: dict[str, Any],
    *,
    packaging_required: bool,
) -> dict[str, Any]:
    issues: list[str] = []
    project = pyproject.get("project")
    if not isinstance(project, dict) or not str(project.get("name", "")).strip():
        issues.append("[project].name is missing")
    if not isinstance(project, dict) or not str(
        project.get("requires-python", "")
    ).strip():
        issues.append("[project].requires-python is missing")
    if isinstance(project, dict):
        readme = project.get("readme")
        readme_path = (
            readme
            if isinstance(readme, str)
            else readme.get("file") if isinstance(readme, dict) else None
        )
        planned_paths = {item["path"] for item in manifest["files"]}
        if isinstance(readme_path, str) and readme_path not in planned_paths:
            issues.append(f"[project].readme references unplanned file {readme_path!r}")
    if packaging_required:
        build = pyproject.get("build-system")
        if not isinstance(build, dict):
            issues.append("[build-system] is missing")
        else:
            if not isinstance(build.get("requires"), list) or not build["requires"]:
                issues.append("[build-system].requires is missing")
            backend = str(build.get("build-backend", "")).strip()
            if not backend:
                issues.append("[build-system].build-backend is missing")
            marker = next(
                (
                    value
                    for key, value in _BACKEND_MARKERS.items()
                    if key in str(profile["build_system"]).casefold()
                ),
                None,
            )
            if marker is not None and backend and marker not in backend.casefold():
                issues.append(
                    f"build backend {backend!r} does not match declared "
                    f"build system {profile['build_system']!r}"
                )

    layout = str(profile["layout"]).strip().casefold()
    build_system = str(profile["build_system"]).strip().casefold()
    if layout == "src" and "setuptools" in build_system:
        tool = pyproject.get("tool", {})
        setuptools = tool.get("setuptools", {}) if isinstance(tool, dict) else {}
        if not isinstance(setuptools, dict):
            issues.append("[tool.setuptools] must be a table")
            setuptools = {}
        package_dir_value = setuptools.get("package-dir", {})
        package_dir = package_dir_value if isinstance(package_dir_value, dict) else {}
        if not isinstance(package_dir_value, dict):
            issues.append("[tool.setuptools].package-dir must be a table")
        packages_value = setuptools.get("packages", {})
        find = packages_value.get("find", {}) if isinstance(packages_value, dict) else {}
        if not isinstance(packages_value, (dict, list)):
            issues.append("[tool.setuptools].packages must be an array or table")
        where = find.get("where", []) if isinstance(find, dict) else []
        if package_dir.get("") != "src" and "src" not in where:
            issues.append("setuptools package discovery does not target the src directory")
        py_modules = setuptools.get("py-modules", []) if isinstance(setuptools, dict) else []
        if not isinstance(py_modules, list):
            issues.append("[tool.setuptools].py-modules must be an array")
        declared_modules = set(py_modules) if isinstance(py_modules, list) else set()
        top_level_modules = {
            PurePosixPath(item["path"]).stem
            for item in manifest["files"]
            if item["category"] == "source"
            and PurePosixPath(item["path"]).parent == PurePosixPath("src")
            and item["path"].endswith(".py")
            and PurePosixPath(item["path"]).name != "__init__.py"
        }
        missing_modules = sorted(top_level_modules - declared_modules)
        if missing_modules:
            issues.append(
                "setuptools py-modules omits top-level src modules: "
                + ", ".join(missing_modules)
            )
    if issues:
        return _failed_check(
            "python-packaging",
            "; ".join(issues),
            [path],
            expected=(
                f"valid {profile['build_system']} metadata for {profile['layout']} layout"
            ),
            actual="; ".join(issues),
            repair_hint="Align build metadata and package discovery with the architecture profile.",
        )
    return _passed("python-packaging", [path])


def _dependency_check(
    pyproject: dict[str, Any],
    path: str,
    architecture: dict[str, Any],
) -> dict[str, Any]:
    project = pyproject.get("project", {})
    declared_values = project.get("dependencies", []) if isinstance(project, dict) else []
    if not isinstance(declared_values, list):
        return _failed_check(
            "python-dependencies",
            "[project].dependencies must be an array",
            [path],
            expected="PEP 621 dependency array",
            actual=type(declared_values).__name__,
        )
    declared = {
        _canonical_dependency(match.group(1))
        for value in declared_values
        if isinstance(value, str) and (match := _DEPENDENCY_NAME.match(value))
    }
    decisions = architecture["dependency_decisions"]
    scopes = {item["name"]: _dependency_scope(item, architecture) for item in decisions}
    expected = {
        _canonical_dependency(item["name"])
        for item in architecture["dependency_decisions"]
        if item["required"] and scopes[item["name"]] in {"runtime", "both"}
    }
    allowed = {
        _canonical_dependency(item["name"])
        for item in architecture["dependency_decisions"]
        if scopes[item["name"]] in {"runtime", "both"}
    }
    build = pyproject.get("build-system", {})
    build_values = build.get("requires", []) if isinstance(build, dict) else []
    if not isinstance(build_values, list):
        return _failed_check(
            "python-dependencies", "[build-system].requires must be an array", [path],
            expected="build dependency array", actual=type(build_values).__name__,
        )
    build_declared = {
        _canonical_dependency(match.group(1))
        for value in build_values
        if isinstance(value, str) and (match := _DEPENDENCY_NAME.match(value))
    }
    build_expected = {
        _canonical_dependency(item["name"]) for item in decisions
        if item["required"] and scopes[item["name"]] in {"build", "both"}
    }
    missing_build = sorted(build_expected - build_declared)
    missing = sorted(expected - declared)
    invented = sorted(declared - allowed)
    if missing or invented or missing_build:
        parts = []
        if missing:
            parts.append("missing required dependencies: " + ", ".join(missing))
        if invented:
            parts.append("undeclared dependencies: " + ", ".join(invented))
        if missing_build:
            parts.append("missing required build dependencies: " + ", ".join(missing_build))
        return _failed_check(
            "python-dependencies",
            "; ".join(parts),
            [path],
            expected=f"runtime: {sorted(expected)}; build: {sorted(build_expected)}",
            actual=f"runtime: {sorted(declared)}; build: {sorted(build_declared)}",
            repair_hint=(
                "Put runtime dependencies in [project].dependencies and build dependencies "
                "in [build-system].requires. Dependencies scoped both must be in both; "
                "do not move runtime requirements into the build section to bypass validation."
            ),
        )
    return _passed("python-dependencies", [path])


def _dependency_scope(item: dict[str, Any], architecture: dict[str, Any]) -> str:
    """Honor explicit scope; conservatively accommodate older architecture files."""
    if "scope" in item:
        return item["scope"]
    # Legacy schemas had no scope. Infer only the selected backend itself when
    # its purpose explicitly concerns building, and never from generated metadata.
    name = _canonical_dependency(item["name"])
    backend = _canonical_dependency(architecture["project_profile"]["build_system"])
    purpose = set(re.findall(r"[a-z]+", item.get("purpose", "").casefold()))
    if name == backend and purpose & {"build", "building", "packaging"} and not (
        purpose & {"runtime", "running", "execution", "import", "imports"}
    ):
        return "build"
    return "runtime"


def _entry_point_check(
    architecture: dict[str, Any],
    manifest: dict[str, Any],
    *,
    pyproject: dict[str, Any] | None,
    path: str | None,
) -> dict[str, Any]:
    if architecture["project_profile"]["kind"] != "cli":
        return _passed("python-entry-points", [path] if path else [])
    if pyproject is None:
        return _failed_check(
            "python-entry-points",
            "CLI architecture has no pyproject.toml script declaration",
            [],
            expected="at least one [project.scripts] command",
            actual="missing",
        )
    project = pyproject.get("project", {})
    scripts = project.get("scripts", {}) if isinstance(project, dict) else {}
    if not isinstance(scripts, dict) or not scripts:
        return _failed_check(
            "python-entry-points",
            "CLI architecture requires at least one [project.scripts] command",
            [path] if path else [],
            expected="command = 'module:callable'",
            actual="missing",
        )
    entry_ids = {
        contract_id
        for entry in architecture["entry_points"]
        for contract_id in entry["contract_ids"]
    }
    contracts = {
        item["contract_id"]: item
        for item in architecture["contracts"]
        if item["contract_id"] in entry_ids
    }
    expected_targets = {
        python_contract_target(item, architecture["contracts"])
        for item in contracts.values()
    }
    actual_targets = {str(value).strip() for value in scripts.values()}
    invalid = sorted(actual_targets - expected_targets)
    if invalid or not actual_targets.intersection(expected_targets):
        return _failed_check(
            "python-entry-points",
            "CLI script targets do not resolve to architecture entry-point contracts",
            [path] if path else [],
            expected=", ".join(sorted(expected_targets)),
            actual=", ".join(sorted(actual_targets)),
            repair_hint="Point project scripts at an exact declared entry-point callable.",
        )
    bound_paths = {
        item["path"] for item in manifest["entry_points"]
    }
    return _passed("python-entry-points", sorted(bound_paths | ({path} if path else set())))


def _module_name(path: str, *, layout: str) -> str:
    pure = PurePosixPath(path)
    parts = list(pure.with_suffix("").parts)
    if layout == "src" and parts and parts[0] == "src":
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _pyproject_path(manifest: dict[str, Any]) -> str | None:
    candidates = [
        item["path"]
        for item in manifest["files"]
        if item["path"].casefold() == "pyproject.toml"
    ]
    return candidates[0] if candidates else None


def _canonical_dependency(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value.strip().casefold())


def _passed(name: str, paths: list[str] | None = None) -> dict[str, Any]:
    return {"name": name, "status": "pass", "message": "Passed", "paths": paths or []}


def _skipped(name: str, message: str) -> dict[str, Any]:
    return {"name": name, "status": "skipped", "message": message, "paths": []}


def _failed_check(
    name: str,
    message: str,
    paths: list[str],
    *,
    expected: str | None = None,
    actual: str | None = None,
    repair_hint: str | None = None,
) -> dict[str, Any]:
    diagnostic = CleanDiagnostic(
        stage="project-readiness",
        check_id=name,
        message=message,
        paths=tuple(paths),
        expected=expected,
        actual=actual,
        repair_hint=repair_hint,
    )
    return {
        "name": name,
        "status": "fail",
        "message": message,
        "paths": paths,
        "diagnostics": [diagnostic.to_dict()],
    }

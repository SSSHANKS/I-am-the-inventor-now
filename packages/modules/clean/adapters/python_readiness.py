from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packages.modules.clean.diagnostics import CleanDiagnostic

_IMPORT_SCRIPT = """
import importlib
import json
import sys

sys.path.insert(0, sys.argv[1])
for module_name in json.loads(sys.argv[2]):
    importlib.import_module(module_name)
for target in json.loads(sys.argv[3]):
    module_name, separator, attribute = target.partition(":")
    value = importlib.import_module(module_name)
    if separator:
        for part in attribute.split("."):
            value = getattr(value, part)
        if not callable(value):
            raise TypeError(f"entry point {target!r} is not callable")
""".strip()

_NO_BUILD_SYSTEMS = frozenset({"none", "no-build", "stdlib", "not-applicable"})


@dataclass(frozen=True)
class _ProcessResult:
    returncode: int | None
    output: str
    timed_out: bool = False


class LocalPythonReadinessExecutor:
    """Run opt-in package, import, and entry-point checks in isolated child processes."""

    def __init__(
        self,
        *,
        python_executable: str | Path | None = None,
        timeout_seconds: float = 30.0,
        max_output_chars: int = 4_000,
    ) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("readiness timeout must be finite and positive")
        if max_output_chars < 256:
            raise ValueError("readiness output limit must be at least 256 characters")
        self.python_executable = str(python_executable or sys.executable)
        self.timeout_seconds = timeout_seconds
        self.max_output_chars = max_output_chars

    def validate(
        self,
        project_root: Path,
        architecture: dict[str, Any],
        manifest: dict[str, Any],
    ) -> list[dict[str, Any]]:
        root = project_root.resolve()
        if not root.is_dir():
            failure = _failed(
                "python-import-smoke",
                "Generated project directory is missing",
                [],
            )
            return [failure, _aggregate([failure])]
        before = _snapshot(root)
        planned = {item["path"] for item in manifest["files"]}
        modules = _public_modules(architecture)
        targets = _entry_targets(architecture)
        runtime_paths = sorted(
            {
                item["path"]
                for item in manifest["files"]
                if item.get("provides")
            }
            | {item["path"] for item in manifest["entry_points"]}
        )
        checks: list[dict[str, Any]] = []

        # Build backends create deeply nested cache paths. Keeping this scratch tree
        # under the workspace can exceed Windows' legacy path limit before a wheel is
        # written, so use the OS temp root and a deliberately short prefix.
        with tempfile.TemporaryDirectory(prefix="cr-") as temporary:
            temp_root = Path(temporary)
            environment = _environment(temp_root)
            import_root: Path | None = None
            build_system = str(
                architecture["project_profile"]["build_system"]
            ).strip().casefold()
            if build_system in _NO_BUILD_SYSTEMS:
                checks.append(
                    _skipped("python-package-build", "No package build is declared")
                )
                layout = str(architecture["project_profile"]["layout"]).casefold()
                import_root = root / "src" if layout == "src" else root
            else:
                wheel_root = temp_root / "wheel"
                wheel_root.mkdir()
                build_source = temp_root / "source"
                shutil.copytree(root, build_source)
                build = self._run(
                    [
                        self.python_executable,
                        "-I",
                        "-B",
                        "-m",
                        "pip",
                        "wheel",
                        ".",
                        "--no-deps",
                        "--no-build-isolation",
                        "--disable-pip-version-check",
                        "--no-index",
                        "--wheel-dir",
                        str(wheel_root),
                    ],
                    cwd=build_source,
                    environment=environment,
                    project_root=root,
                )
                wheels = sorted(wheel_root.glob("*.whl"))
                if build.returncode != 0 or len(wheels) != 1:
                    checks.append(
                        _process_failure(
                            "python-package-build",
                            build,
                            ["pyproject.toml"] if "pyproject.toml" in planned else [],
                        )
                    )
                else:
                    import_root = wheels[0]
                    checks.append(
                        _passed(
                            "python-package-build",
                            "Offline wheel assembly passed",
                            ["pyproject.toml"] if "pyproject.toml" in planned else [],
                        )
                    )
                    missing_modules = _missing_wheel_modules(wheels[0], modules)
                    if missing_modules:
                        checks.append(
                            _failed(
                                "python-wheel-contents",
                                "Built wheel omits public modules: "
                                + ", ".join(missing_modules),
                                ["pyproject.toml"] if "pyproject.toml" in planned else [],
                                expected="all public architecture modules in the wheel",
                                actual=", ".join(missing_modules),
                                repair_hint=(
                                    "Configure package discovery or py-modules to include "
                                    "the planned source modules."
                                ),
                            )
                        )
                        import_root = None
                    else:
                        checks.append(
                            _passed(
                                "python-wheel-contents",
                                f"Wheel contains {len(modules)} public module(s)",
                                ["pyproject.toml"]
                                if "pyproject.toml" in planned
                                else [],
                            )
                        )

            if import_root is None:
                checks.append(
                    _skipped("python-import-smoke", "Blocked by package build failure")
                )
                checks.append(
                    _skipped(
                        "python-entry-point-runtime",
                        "Blocked by package build failure",
                    )
                )
            elif not modules and not targets:
                checks.append(
                    _skipped(
                        "python-import-smoke",
                        "No public Python modules or entry points are declared",
                    )
                )
                checks.append(
                    _skipped(
                        "python-entry-point-runtime",
                        "No runtime entry points are declared",
                    )
                )
            else:
                imports = self._run(
                    [
                        self.python_executable,
                        "-I",
                        "-B",
                        "-c",
                        _IMPORT_SCRIPT,
                        str(import_root),
                        json.dumps(modules),
                        json.dumps(targets),
                    ],
                    cwd=temp_root,
                    environment=environment,
                    project_root=root,
                )
                implicated = _implicated_paths(imports.output, planned)
                if imports.returncode != 0:
                    checks.append(
                        _process_failure(
                            "python-import-smoke",
                            imports,
                            list(implicated) or runtime_paths,
                        )
                    )
                    checks.append(
                        _skipped(
                            "python-entry-point-runtime",
                            "Blocked by import failure",
                        )
                    )
                else:
                    checks.append(
                        _passed(
                            "python-import-smoke",
                            f"Imported {len(modules)} public module(s)",
                        )
                    )
                    checks.append(
                        _passed(
                            "python-entry-point-runtime",
                            f"Resolved {len(targets)} callable entry point(s)",
                        )
                        if targets
                        else _skipped(
                            "python-entry-point-runtime",
                            "No runtime entry points are declared",
                        )
                    )

        after = _snapshot(root)
        changed = sorted(
            path
            for path in before.keys() | after.keys()
            if before.get(path) != after.get(path)
        )
        if changed:
            checks.append(
                _failed(
                    "python-mutation-check",
                    "Readiness execution modified the generated project",
                    changed,
                    actual=", ".join(changed),
                    repair_hint="Remove import-time writes and other project mutations.",
                )
            )
        else:
            checks.append(_passed("python-mutation-check", "No project files changed"))
        checks.append(_aggregate(checks))
        return checks

    def _run(
        self,
        arguments: list[str],
        *,
        cwd: Path,
        environment: dict[str, str],
        project_root: Path,
    ) -> _ProcessResult:
        try:
            completed = subprocess.run(
                arguments,
                cwd=cwd,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            output = _joined_output(exc.stdout, exc.stderr)
            return _ProcessResult(
                None,
                self._bounded(output, project_root),
                timed_out=True,
            )
        return _ProcessResult(
            completed.returncode,
            self._bounded(
                _joined_output(completed.stdout, completed.stderr),
                project_root,
            ),
        )

    def _bounded(self, output: str, project_root: Path) -> str:
        sanitized = output.replace(str(project_root), "<project>")
        sanitized = sanitized.replace(str(project_root).replace("\\", "/"), "<project>")
        sanitized = sanitized.replace(str(project_root.parent), "<workspace>")
        sanitized = sanitized.replace(
            str(project_root.parent).replace("\\", "/"),
            "<workspace>",
        )
        sanitized = sanitized.replace(self.python_executable, "<python>")
        if len(sanitized) <= self.max_output_chars:
            return sanitized
        omitted = len(sanitized) - self.max_output_chars
        return f"[... {omitted} characters omitted ...]\n{sanitized[-self.max_output_chars:]}"


def _public_modules(architecture: dict[str, Any]) -> list[str]:
    return sorted(
        {
            str(item["qualified_name"]).rsplit(".", 1)[0]
            for item in architecture["contracts"]
            if item["visibility"] == "public" and "." in str(item["qualified_name"])
        }
    )


def _entry_targets(architecture: dict[str, Any]) -> list[str]:
    contract_ids = {
        contract_id
        for entry in architecture["entry_points"]
        for contract_id in entry["contract_ids"]
    }
    return sorted(
        {
            _target(item["qualified_name"])
            for item in architecture["contracts"]
            if item["contract_id"] in contract_ids
        }
    )


def _missing_wheel_modules(wheel: Path, modules: list[str]) -> list[str]:
    with zipfile.ZipFile(wheel) as archive:
        members = {name.rstrip("/") for name in archive.namelist()}
    missing = []
    for module in modules:
        relative = module.replace(".", "/")
        if f"{relative}.py" not in members and f"{relative}/__init__.py" not in members:
            missing.append(module)
    return missing


def _target(qualified_name: str) -> str:
    module, separator, attribute = str(qualified_name).rpartition(".")
    return f"{module}:{attribute}" if separator else str(qualified_name)


def _environment(temp_root: Path) -> dict[str, str]:
    environment = {
        key: os.environ[key]
        for key in ("SYSTEMROOT", "WINDIR")
        if key in os.environ
    }
    environment.update(
        {
            "TEMP": str(temp_root),
            "TMP": str(temp_root),
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INDEX": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONNOUSERSITE": "1",
            "PYTHONUTF8": "1",
        }
    )
    return environment


def _snapshot(root: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            snapshot[relative] = "symlink"
        elif path.is_file():
            snapshot[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snapshot


def _implicated_paths(output: str, planned: set[str]) -> tuple[str, ...]:
    normalized = output.replace("\\", "/")
    return tuple(sorted(path for path in planned if path in normalized))


def _joined_output(stdout: str | bytes | None, stderr: str | bytes | None) -> str:
    def render(value: str | bytes | None) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value or ""

    return "\n".join(item for item in (render(stdout), render(stderr)) if item)


def _process_failure(
    name: str,
    process: _ProcessResult,
    paths: list[str],
) -> dict[str, Any]:
    reason = "timed out" if process.timed_out else f"exited {process.returncode}"
    message = f"{name} failed: {reason}"
    if process.output.strip():
        message += "\n" + process.output.strip()
    return _failed(name, message, paths, actual=reason)


def _aggregate(checks: list[dict[str, Any]]) -> dict[str, Any]:
    failures = [item for item in checks if item["status"] == "fail"]
    executed = any(
        item["status"] == "pass"
        and item["name"] in {"python-import-smoke", "python-entry-point-runtime"}
        for item in checks
    )
    if failures:
        return _failed(
            "executable-validation",
            "Python project readiness failed: "
            + ", ".join(item["name"] for item in failures),
            sorted({path for item in failures for path in item["paths"]}),
        )
    if not executed:
        return _skipped(
            "executable-validation",
            "No safe import or entry-point readiness action was applicable",
        )
    return _passed(
        "executable-validation",
        "Package, import, entry-point, and mutation readiness gates passed",
    )


def _passed(
    name: str,
    message: str,
    paths: list[str] | None = None,
) -> dict[str, Any]:
    return {"name": name, "status": "pass", "message": message, "paths": paths or []}


def _skipped(name: str, message: str) -> dict[str, Any]:
    return {"name": name, "status": "skipped", "message": message, "paths": []}


def _failed(
    name: str,
    message: str,
    paths: list[str],
    *,
    actual: str | None = None,
    repair_hint: str | None = None,
) -> dict[str, Any]:
    diagnostic = CleanDiagnostic(
        stage="runtime-readiness",
        check_id=name,
        message=message,
        paths=tuple(paths),
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

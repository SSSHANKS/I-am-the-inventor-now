from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

_IMPORT_SCRIPT = """
import importlib
import json
import sys

sys.path.insert(0, sys.argv[1])
for module_name in json.loads(sys.argv[2]):
    importlib.import_module(module_name)
""".strip()

_PYTEST_SCRIPT = """
import sys

sys.path.insert(0, sys.argv.pop(1))
from pytest import main

raise SystemExit(main())
""".strip()

_COLLECTED_PATTERNS = (
    re.compile(r"(?P<count>\d+) tests? collected"),
    re.compile(r"collected (?P<count>\d+) items?"),
)


@dataclass(frozen=True)
class ExecutableValidationResult:
    """Sanitized outcome returned by a trusted Clean validation adapter."""

    passed: bool
    message: str
    paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExecutableValidationPolicy:
    """Trusted adapter capabilities and constraints supplied to the Clean planner."""

    adapter: str
    supported_languages: tuple[str, ...]
    capabilities: tuple[str, ...]
    requirements: tuple[str, ...]

    def to_prompt_dict(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "adapter": self.adapter,
            "supported_languages": list(self.supported_languages),
            "capabilities": list(self.capabilities),
            "requirements": list(self.requirements),
        }


class ValidationExecutor(Protocol):
    """Boundary for executing generated projects outside the Clean runner process."""

    def planning_policy(self) -> ExecutableValidationPolicy: ...

    def validate_plan(
        self,
        plan: dict[str, Any],
        requirement_ids: set[str],
    ) -> tuple[str, ...]: ...

    def validate(
        self,
        project_root: Path,
        plan: dict[str, Any],
    ) -> ExecutableValidationResult: ...


@dataclass(frozen=True)
class _ProcessResult:
    returncode: int | None
    output: str
    timed_out: bool = False


class LocalPythonValidationExecutor:
    """Opt-in Python validation in a child process with a stripped environment.

    This adapter limits accidental exposure and process lifetime, but it is not an OS
    security sandbox. Callers must opt in only where executing generated code locally is
    acceptable.
    """

    def __init__(
        self,
        *,
        python_executable: str | Path | None = None,
        timeout_seconds: float = 30.0,
        max_output_chars: int = 4_000,
    ) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("validation timeout must be finite and positive")
        if max_output_chars < 256:
            raise ValueError("validation output limit must be at least 256 characters")
        self.python_executable = str(python_executable or sys.executable)
        self.timeout_seconds = timeout_seconds
        self.max_output_chars = max_output_chars

    def planning_policy(self) -> ExecutableValidationPolicy:
        return ExecutableValidationPolicy(
            adapter="local-python",
            supported_languages=("Python",),
            capabilities=("import-smoke", "pytest-collection", "pytest-suite"),
            requirements=(
                "Plan at least one importable package or Python module entry point.",
                "Plan pytest-discoverable tests for every specification requirement ID.",
                (
                    "Each test must depend on a non-test implementation file. FR, BR, "
                    "EH, and AC IDs must be shared by the test and an implementation "
                    "dependency; TC IDs belong directly to test files."
                ),
                "Do not rely on dependency installation or network access.",
            ),
        )

    def validate_plan(
        self,
        plan: dict[str, Any],
        requirement_ids: set[str],
    ) -> tuple[str, ...]:
        language = str(plan.get("runtime", {}).get("language", ""))
        if "python" not in language.casefold():
            return (f"local-python does not support runtime language {language!r}",)
        if not _import_targets(plan):
            return (
                "local-python requires an importable package or Python module entry point",
            )

        tasks_by_path = {str(item["path"]): item for item in plan["files"]}
        covered: set[str] = set()
        issues: list[str] = []
        for path, item in tasks_by_path.items():
            if not is_python_test_path(path):
                continue
            implementation_dependencies = [
                dependency
                for dependency in item["depends_on"]
                if not is_python_test_path(dependency)
            ]
            if not implementation_dependencies:
                issues.append(
                    f"Python test {path!r} does not depend on an implementation file"
                )
                continue
            dependency_requirements = {
                requirement_id
                for dependency in implementation_dependencies
                for requirement_id in tasks_by_path[dependency]["requirement_ids"]
            }
            test_requirements = set(item["requirement_ids"])
            covered.update(
                requirement_id
                for requirement_id in test_requirements
                if requirement_id.startswith("TC-")
            )
            covered.update(test_requirements & dependency_requirements)

        uncovered = sorted(requirement_ids - covered)
        if uncovered:
            issues.append(
                "Python tests do not cover specification requirements: "
                f"{', '.join(uncovered)}"
            )
        return tuple(issues)

    def validate(
        self,
        project_root: Path,
        plan: dict[str, Any],
    ) -> ExecutableValidationResult:
        root = project_root.resolve()
        if not root.is_dir():
            return ExecutableValidationResult(False, "Generated project directory is missing")
        runtime = plan.get("runtime", {})
        if "python" not in str(runtime.get("language", "")).casefold():
            return ExecutableValidationResult(
                False,
                "Local Python validation requires a Python runtime plan",
            )

        planned = {str(item["path"]) for item in plan["files"]}
        before = _snapshot(root)
        import_targets = _import_targets(plan)
        if not import_targets:
            return ExecutableValidationResult(
                False,
                "import-smoke failed: no importable package or entry-point module was planned",
            )

        with tempfile.TemporaryDirectory(prefix="clean-validation-", dir=root.parent) as temp:
            temp_root = Path(temp)
            environment = _validation_environment(temp_root)
            imports = self._run(
                [
                    self.python_executable,
                    "-I",
                    "-B",
                    "-c",
                    _IMPORT_SCRIPT,
                    str(root),
                    json.dumps(import_targets),
                ],
                cwd=root,
                environment=environment,
            )
            if imports.returncode != 0:
                result = self._failure("import-smoke", imports, planned, import_targets)
                return _with_mutation_check(result, root, before, planned)

            test_paths = sorted(path for path in planned if is_python_test_path(path))
            if not test_paths:
                result = ExecutableValidationResult(
                    False,
                    "pytest-collection failed: the Clean plan contains no Python test files",
                )
                return _with_mutation_check(result, root, before, planned)

            collection = self._run(
                [
                    self.python_executable,
                    "-I",
                    "-B",
                    "-c",
                    _PYTEST_SCRIPT,
                    str(root),
                    "-p",
                    "no:cacheprovider",
                    "--collect-only",
                    f"--basetemp={temp_root / 'collect'}",
                    *test_paths,
                ],
                cwd=root,
                environment=environment,
            )
            collected = _collected_count(collection.output)
            if collection.returncode != 0 or not collected:
                result = self._failure("pytest-collection", collection, planned, test_paths)
                return _with_mutation_check(result, root, before, planned)

            suite = self._run(
                [
                    self.python_executable,
                    "-I",
                    "-B",
                    "-c",
                    _PYTEST_SCRIPT,
                    str(root),
                    "-p",
                    "no:cacheprovider",
                    "-q",
                    f"--basetemp={temp_root / 'suite'}",
                    *test_paths,
                ],
                cwd=root,
                environment=environment,
            )
            if suite.returncode != 0:
                result = self._failure("pytest-suite", suite, planned, test_paths)
                return _with_mutation_check(result, root, before, planned)

        result = ExecutableValidationResult(
            True,
            (
                f"import-smoke passed for {len(import_targets)} target(s); "
                f"pytest collected and passed {collected} test(s)"
            ),
        )
        return _with_mutation_check(result, root, before, planned)

    def _run(
        self,
        arguments: list[str],
        *,
        cwd: Path,
        environment: dict[str, str],
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
            return _ProcessResult(None, self._bounded(output, cwd), timed_out=True)
        output = _joined_output(completed.stdout, completed.stderr)
        return _ProcessResult(completed.returncode, self._bounded(output, cwd))

    def _failure(
        self,
        gate: str,
        process: _ProcessResult,
        planned: set[str],
        fallback_paths: list[str],
    ) -> ExecutableValidationResult:
        reason = "timed out" if process.timed_out else f"exited {process.returncode}"
        details = process.output.strip()
        message = f"{gate} failed: {reason}"
        if details:
            message = f"{message}\n{details}"
        implicated = _implicated_paths(process.output, planned)
        if not implicated:
            implicated = tuple(path for path in fallback_paths if path in planned)
        return ExecutableValidationResult(False, message, implicated)

    def _bounded(self, output: str, project_root: Path) -> str:
        sanitized = output.replace(str(project_root), "<project>")
        sanitized = sanitized.replace(str(project_root).replace("\\", "/"), "<project>")
        sanitized = sanitized.replace(self.python_executable, "<python>")
        if len(sanitized) <= self.max_output_chars:
            return sanitized
        omitted = len(sanitized) - self.max_output_chars
        return f"[... {omitted} characters omitted ...]\n{sanitized[-self.max_output_chars:]}"


def _validation_environment(temp_root: Path) -> dict[str, str]:
    environment = {
        key: os.environ[key]
        for key in ("SYSTEMROOT", "WINDIR")
        if key in os.environ
    }
    environment.update(
        {
            "TEMP": str(temp_root),
            "TMP": str(temp_root),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONNOUSERSITE": "1",
            "PYTHONUTF8": "1",
        }
    )
    return environment


def _import_targets(plan: dict[str, Any]) -> list[str]:
    planned = {str(item["path"]) for item in plan["files"]}
    targets: set[str] = set()
    for path in planned:
        pure = PurePosixPath(path)
        if pure.suffix != ".py" or is_python_test_path(path):
            continue
        if pure.name == "__init__.py" and pure.parent != PurePosixPath("."):
            targets.add(".".join(pure.parent.parts))
    for entry in plan.get("entry_points", []):
        pure = PurePosixPath(str(entry["path"]))
        if pure.suffix != ".py" or is_python_test_path(pure.as_posix()):
            continue
        module_parts = pure.with_suffix("").parts
        if module_parts[-1] == "__init__":
            module_parts = module_parts[:-1]
        if module_parts and _parents_are_packages(module_parts[:-1], planned):
            targets.add(".".join(module_parts))
    return sorted(targets)


def _parents_are_packages(parts: tuple[str, ...], planned: set[str]) -> bool:
    return all(
        PurePosixPath(*parts[:index], "__init__.py").as_posix() in planned
        for index in range(1, len(parts) + 1)
    )


def is_python_test_path(path: str) -> bool:
    """Return whether a planned path follows supported pytest discovery conventions."""
    pure = PurePosixPath(path)
    return pure.suffix == ".py" and (
        pure.name.startswith("test_")
        or pure.name.endswith("_test.py")
        or any(part in {"test", "tests"} for part in pure.parts[:-1])
    )


def _collected_count(output: str) -> int:
    for pattern in _COLLECTED_PATTERNS:
        match = pattern.search(output)
        if match:
            return int(match.group("count"))
    return 0


def _implicated_paths(output: str, planned: set[str]) -> tuple[str, ...]:
    normalized = output.replace("\\", "/")
    return tuple(sorted(path for path in planned if path in normalized))


def _snapshot(root: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            snapshot[relative] = "symlink"
        elif path.is_file():
            snapshot[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snapshot


def _with_mutation_check(
    result: ExecutableValidationResult,
    root: Path,
    before: dict[str, str],
    planned: set[str],
) -> ExecutableValidationResult:
    after = _snapshot(root)
    changed = sorted(
        path for path in before.keys() | after.keys() if before.get(path) != after.get(path)
    )
    if not changed:
        return result
    implicated = tuple(path for path in changed if path in planned)
    preview = ", ".join(changed[:10])
    if len(changed) > 10:
        preview = f"{preview}, ..."
    return ExecutableValidationResult(
        False,
        f"executable-validation failed: generated project was modified: {preview}",
        implicated,
    )


def _joined_output(stdout: str | bytes | None, stderr: str | bytes | None) -> str:
    def text(value: str | bytes | None) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value

    return "\n".join(part for part in (text(stdout), text(stderr)) if part)

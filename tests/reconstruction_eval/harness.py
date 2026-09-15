from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packages.agents.base_agent import StubTextClient
from packages.agents.clean_team import CleanBuilderAgent, CleanPlannerAgent, CleanRepairAgent
from packages.modules.clean import CleanRunner, LocalPythonValidationExecutor
from packages.modules.clean.execution import is_python_test_path
from packages.modules.clean.workspace import WorkspaceError, validate_relative_path
from packages.modules.handoff import export_clean_handoff

_ARCHETYPES = frozenset(
    {
        "library",
        "cli",
        "stateful-resource",
        "configuration",
        "multi-module",
        "unsupported-runtime",
    }
)
_STATUSES = frozenset({"success", "partial", "failed"})
_VALIDATION_LEVELS = frozenset({"none", "structure", "syntax", "executable"})
_REQUIREMENT_PATTERN = re.compile(r"^(?:FR|BR|EH|AC|TC)-\d{3,}$")
_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")
_ROOT_V1_KEYS = {
    "schema_version",
    "name",
    "archetype",
    "compatibility_mode",
    "specification_file",
    "plan_file",
    "generated_files",
    "hidden_tests",
    "expected",
}
_ROOT_V2_KEYS = _ROOT_V1_KEYS | {"seeded_faults"}
_EXPECTED_KEYS = {
    "status",
    "validation_level",
    "requirement_ids",
    "minimum_generated_tests",
    "minimum_hidden_tests",
}
_FAULT_KEYS = {
    "fault_id",
    "target_path",
    "replacement_file",
    "expect_generated_detection",
    "expect_hidden_detection",
}
_FAULT_ID_PATTERN = re.compile(r"^FAULT-\d{3,}$")
_HIDDEN_PYTEST_SCRIPT = """
import sys

project_root = sys.argv.pop(1)
sys.path.insert(0, project_root)
from pytest import main

raise SystemExit(main())
""".strip()
_PASSED_PATTERN = re.compile(r"(?P<count>\d+) passed")


class EvaluationFixtureError(ValueError):
    """An offline reconstruction fixture is malformed or unsafe."""


@dataclass(frozen=True)
class EvaluationExpectation:
    status: str
    validation_level: str
    requirement_ids: tuple[str, ...]
    minimum_generated_tests: int
    minimum_hidden_tests: int


@dataclass(frozen=True)
class SeededFault:
    fault_id: str
    target_path: str
    replacement: str
    expect_generated_detection: bool
    expect_hidden_detection: bool


@dataclass(frozen=True)
class EvaluationFixture:
    root: Path
    name: str
    archetype: str
    compatibility_mode: str
    specification: str
    plan: dict[str, Any]
    generated_files: dict[str, str]
    hidden_tests: tuple[str, ...]
    seeded_faults: tuple[SeededFault, ...]
    expected: EvaluationExpectation


@dataclass(frozen=True)
class HiddenEvaluation:
    passed: bool
    tests_run: int
    output: str


@dataclass(frozen=True)
class EvaluationMetrics:
    fixture_name: str
    archetype: str
    status: str
    validation_level: str
    requirement_ids: tuple[str, ...]
    generated_file_count: int
    generated_test_file_count: int
    repair_rounds: int
    model_call_count: int
    hidden: HiddenEvaluation


@dataclass(frozen=True)
class EvaluationRun:
    metrics: EvaluationMetrics
    prompts: tuple[str, ...]
    project_root: Path
    report_path: Path


@dataclass(frozen=True)
class SeededFaultEvaluation:
    fault_id: str
    target_path: str
    generated_detected: bool
    hidden_detected: bool
    generated_message: str
    hidden: HiddenEvaluation


def load_evaluation_fixture(root: str | Path) -> EvaluationFixture:
    fixture_root = Path(root)
    if fixture_root.is_symlink() or not fixture_root.is_dir():
        raise EvaluationFixtureError("Evaluation fixture root must be a real directory")
    fixture_root = fixture_root.resolve()
    manifest = _load_json_asset(fixture_root, "fixture.json")
    schema_version = manifest.get("schema_version")
    if schema_version == 1:
        _require_exact_keys(manifest, _ROOT_V1_KEYS, "fixture")
    elif schema_version == 2:
        _require_exact_keys(manifest, _ROOT_V2_KEYS, "fixture")
    else:
        raise EvaluationFixtureError("Evaluation fixture schema_version must be 1 or 2")

    name = _required_string(manifest, "name")
    if not _NAME_PATTERN.fullmatch(name):
        raise EvaluationFixtureError("Evaluation fixture name must be a lowercase slug")
    archetype = _required_string(manifest, "archetype")
    if archetype not in _ARCHETYPES:
        raise EvaluationFixtureError(f"Unsupported evaluation archetype {archetype!r}")
    compatibility_mode = _required_string(manifest, "compatibility_mode")
    if compatibility_mode not in {"drop-in", "renamed"}:
        raise EvaluationFixtureError("Fixture compatibility_mode must be drop-in or renamed")

    specification = _load_text_asset(
        fixture_root,
        _required_string(manifest, "specification_file"),
    )
    if not specification.lstrip().startswith("#"):
        raise EvaluationFixtureError("Fixture specification must be Markdown")
    plan = _load_json_asset(
        fixture_root,
        _required_string(manifest, "plan_file"),
    )

    generated_assets = manifest["generated_files"]
    if not isinstance(generated_assets, dict) or not generated_assets:
        raise EvaluationFixtureError("Fixture generated_files must be a non-empty object")
    generated_files: dict[str, str] = {}
    for raw_path, raw_asset in generated_assets.items():
        path = validate_relative_path(raw_path)
        if not isinstance(raw_asset, str):
            raise EvaluationFixtureError(f"Generated asset for {path!r} must be a path")
        generated_files[path] = _load_text_asset(fixture_root, raw_asset)

    hidden_assets = manifest["hidden_tests"]
    if not isinstance(hidden_assets, list) or not all(
        isinstance(item, str) for item in hidden_assets
    ):
        raise EvaluationFixtureError("Fixture hidden_tests must be a list of asset paths")
    hidden_tests = tuple(_load_text_asset(fixture_root, item) for item in hidden_assets)
    seeded_faults = _load_seeded_faults(
        fixture_root,
        manifest.get("seeded_faults", []),
        generated_files=set(generated_files),
    )

    expected_payload = manifest["expected"]
    if not isinstance(expected_payload, dict):
        raise EvaluationFixtureError("Fixture expected value must be an object")
    _require_exact_keys(expected_payload, _EXPECTED_KEYS, "fixture expected")
    status = _required_string(expected_payload, "status")
    validation_level = _required_string(expected_payload, "validation_level")
    requirement_ids = _requirement_ids(expected_payload["requirement_ids"])
    minimum_generated_tests = _nonnegative_integer(
        expected_payload,
        "minimum_generated_tests",
    )
    minimum_hidden_tests = _nonnegative_integer(
        expected_payload,
        "minimum_hidden_tests",
    )
    if status not in _STATUSES:
        raise EvaluationFixtureError(f"Invalid expected status {status!r}")
    if validation_level not in _VALIDATION_LEVELS:
        raise EvaluationFixtureError(
            f"Invalid expected validation level {validation_level!r}"
        )
    if minimum_hidden_tests and not hidden_tests:
        raise EvaluationFixtureError("Fixture expects hidden tests but declares none")

    return EvaluationFixture(
        root=fixture_root,
        name=name,
        archetype=archetype,
        compatibility_mode=compatibility_mode,
        specification=specification,
        plan=plan,
        generated_files=generated_files,
        hidden_tests=hidden_tests,
        seeded_faults=seeded_faults,
        expected=EvaluationExpectation(
            status=status,
            validation_level=validation_level,
            requirement_ids=requirement_ids,
            minimum_generated_tests=minimum_generated_tests,
            minimum_hidden_tests=minimum_hidden_tests,
        ),
    )


def run_seeded_fault_evaluations(
    fixture: EvaluationFixture,
    baseline: EvaluationRun,
    work_root: str | Path,
    *,
    python_executable: str | Path = sys.executable,
) -> tuple[SeededFaultEvaluation, ...]:
    root = Path(work_root)
    root.mkdir(parents=True, exist_ok=False)
    executor = LocalPythonValidationExecutor(python_executable=python_executable)
    evaluations: list[SeededFaultEvaluation] = []
    for fault in fixture.seeded_faults:
        fault_root = root / fault.fault_id.casefold()
        project_root = fault_root / "project"
        shutil.copytree(baseline.project_root, project_root)
        target = project_root.joinpath(*fault.target_path.split("/"))
        if target.is_symlink() or not target.is_file():
            raise EvaluationFixtureError(
                f"Seeded fault target {fault.target_path!r} is not a generated file"
            )
        target.write_text(fault.replacement, encoding="utf-8")

        generated = executor.validate(project_root, fixture.plan)
        hidden = _run_hidden_tests(
            fixture.hidden_tests,
            project_root,
            fault_root / "hidden-evaluation",
            python_executable=str(python_executable),
        )
        evaluations.append(
            SeededFaultEvaluation(
                fault_id=fault.fault_id,
                target_path=fault.target_path,
                generated_detected=not generated.passed,
                hidden_detected=not hidden.passed,
                generated_message=generated.message,
                hidden=hidden,
            )
        )
    return tuple(evaluations)


def run_evaluation_fixture(
    fixture: EvaluationFixture,
    work_root: str | Path,
    *,
    python_executable: str | Path = sys.executable,
) -> EvaluationRun:
    root = Path(work_root)
    root.mkdir(parents=True, exist_ok=False)
    handoff = export_clean_handoff(
        root / "handoff",
        fixture.specification,
        {"status": "pass"},
        compatibility_mode=fixture.compatibility_mode,
    )

    client = StubTextClient(_response_router(fixture))
    agent_options = {"model": "stub/evaluation", "chat_client": client}
    result = CleanRunner(
        CleanPlannerAgent(**agent_options),
        CleanBuilderAgent(**agent_options),
        CleanRepairAgent(**agent_options),
        max_repairs=0,
        validation_executor=LocalPythonValidationExecutor(
            python_executable=python_executable,
        ),
    ).run(handoff, root / "workspace")
    hidden = _run_hidden_tests(
        fixture.hidden_tests,
        result.project_root,
        root / "hidden-evaluation",
        python_executable=str(python_executable),
    )
    requirement_ids = tuple(
        sorted(item["requirement_id"] for item in result.report["requirements"])
    )
    generated_paths = tuple(result.report["generated_files"])
    metrics = EvaluationMetrics(
        fixture_name=fixture.name,
        archetype=fixture.archetype,
        status=result.status,
        validation_level=result.report["validation_level"],
        requirement_ids=requirement_ids,
        generated_file_count=len(generated_paths),
        generated_test_file_count=sum(
            is_python_test_path(path) for path in generated_paths
        ),
        repair_rounds=result.report["repair_rounds"],
        model_call_count=client.call_count,
        hidden=hidden,
    )
    return EvaluationRun(
        metrics=metrics,
        prompts=tuple(client.prompts),
        project_root=result.project_root,
        report_path=result.report_path,
    )


def expectation_failures(
    fixture: EvaluationFixture,
    metrics: EvaluationMetrics,
) -> tuple[str, ...]:
    expected = fixture.expected
    failures: list[str] = []
    if metrics.fixture_name != fixture.name:
        failures.append("fixture name does not match")
    if metrics.archetype != fixture.archetype:
        failures.append("fixture archetype does not match")
    if metrics.status != expected.status:
        failures.append(f"expected status {expected.status!r}, got {metrics.status!r}")
    if metrics.validation_level != expected.validation_level:
        failures.append(
            "expected validation level "
            f"{expected.validation_level!r}, got {metrics.validation_level!r}"
        )
    if metrics.requirement_ids != expected.requirement_ids:
        failures.append("requirement coverage does not match the fixture expectation")
    if metrics.generated_test_file_count < expected.minimum_generated_tests:
        failures.append("generated test-file count is below the fixture minimum")
    if metrics.hidden.tests_run < expected.minimum_hidden_tests:
        failures.append("hidden test count is below the fixture minimum")
    if fixture.hidden_tests and not metrics.hidden.passed:
        failures.append("hidden evaluator failed")
    return tuple(failures)


def fault_expectation_failures(
    fixture: EvaluationFixture,
    evaluations: tuple[SeededFaultEvaluation, ...],
) -> tuple[str, ...]:
    expected = {fault.fault_id: fault for fault in fixture.seeded_faults}
    actual = {evaluation.fault_id: evaluation for evaluation in evaluations}
    failures: list[str] = []
    if set(actual) != set(expected):
        failures.append("seeded fault evaluation IDs do not match the fixture")
        return tuple(failures)
    for fault_id, fault in sorted(expected.items()):
        evaluation = actual[fault_id]
        if evaluation.target_path != fault.target_path:
            failures.append(f"{fault_id} target path does not match")
        if evaluation.generated_detected != fault.expect_generated_detection:
            failures.append(
                f"{fault_id} generated detection was {evaluation.generated_detected}, "
                f"expected {fault.expect_generated_detection}"
            )
        if evaluation.hidden_detected != fault.expect_hidden_detection:
            failures.append(
                f"{fault_id} hidden detection was {evaluation.hidden_detected}, "
                f"expected {fault.expect_hidden_detection}"
            )
    return tuple(failures)


def _response_router(fixture: EvaluationFixture):
    def respond(prompt: str) -> str:
        if "<clean_plan_request>" in prompt:
            return json.dumps(fixture.plan)
        if "<clean_file_task>" in prompt:
            task = json.loads(_tag(prompt, "requested_file"))
            path = task["path"]
            if path not in fixture.generated_files:
                raise AssertionError(f"Fixture has no scripted generated file for {path!r}")
            return json.dumps(
                {
                    "files": [
                        {
                            "path": path,
                            "content": fixture.generated_files[path],
                            "requirement_ids": task["requirement_ids"],
                        }
                    ],
                    "notes": [],
                }
            )
        if "<clean_repair_request>" in prompt:
            raise AssertionError("Baseline evaluation fixture unexpectedly requested repair")
        raise AssertionError("Evaluation fixture received an unknown model prompt")

    return respond


def _run_hidden_tests(
    hidden_tests: tuple[str, ...],
    project_root: Path,
    evaluation_root: Path,
    *,
    python_executable: str,
) -> HiddenEvaluation:
    if not hidden_tests:
        return HiddenEvaluation(passed=True, tests_run=0, output="No hidden tests declared")
    evaluation_root.mkdir(parents=True)
    evaluation_root = evaluation_root.resolve()
    project_root = project_root.resolve()
    paths: list[str] = []
    for index, content in enumerate(hidden_tests, start=1):
        target = evaluation_root / f"test_hidden_{index:03d}.py"
        target.write_text(content, encoding="utf-8")
        paths.append(str(target))
    temporary = evaluation_root / "tmp"
    temporary.mkdir()
    environment = {
        key: os.environ[key]
        for key in ("SYSTEMROOT", "WINDIR")
        if key in os.environ
    }
    environment.update(
        {
            "TEMP": str(temporary),
            "TMP": str(temporary),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONNOUSERSITE": "1",
            "PYTHONUTF8": "1",
        }
    )
    completed = subprocess.run(
        [
            python_executable,
            "-I",
            "-B",
            "-c",
            _HIDDEN_PYTEST_SCRIPT,
            str(project_root),
            "-p",
            "no:cacheprovider",
            "-o",
            "addopts=",
            "-q",
            f"--basetemp={temporary}",
            *paths,
        ],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
        shell=False,
    )
    output = "\n".join(
        part for part in (completed.stdout, completed.stderr) if part
    ).strip()
    sanitized = output.replace(str(project_root), "<project>")
    sanitized = sanitized.replace(str(evaluation_root), "<hidden-evaluation>")
    match = _PASSED_PATTERN.search(output)
    tests_run = int(match.group("count")) if match else 0
    return HiddenEvaluation(
        passed=completed.returncode == 0 and tests_run > 0,
        tests_run=tests_run,
        output=sanitized,
    )


def _load_json_asset(root: Path, relative_path: str) -> dict[str, Any]:
    text = _load_text_asset(root, relative_path)
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise EvaluationFixtureError(
            f"Evaluation asset {relative_path!r} is not valid JSON"
        ) from exc
    if not isinstance(value, dict):
        raise EvaluationFixtureError(
            f"Evaluation JSON asset {relative_path!r} must contain an object"
        )
    return value


def _load_seeded_faults(
    root: Path,
    payload: Any,
    *,
    generated_files: set[str],
) -> tuple[SeededFault, ...]:
    if not isinstance(payload, list):
        raise EvaluationFixtureError("Fixture seeded_faults must be a list")
    faults: list[SeededFault] = []
    seen: set[str] = set()
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise EvaluationFixtureError(f"Seeded fault {index} must be an object")
        _require_exact_keys(item, _FAULT_KEYS, f"seeded fault {index}")
        fault_id = _required_string(item, "fault_id")
        if not _FAULT_ID_PATTERN.fullmatch(fault_id):
            raise EvaluationFixtureError(f"Invalid seeded fault ID {fault_id!r}")
        if fault_id in seen:
            raise EvaluationFixtureError(f"Duplicate seeded fault ID {fault_id!r}")
        seen.add(fault_id)
        try:
            target_path = validate_relative_path(_required_string(item, "target_path"))
        except WorkspaceError as exc:
            raise EvaluationFixtureError(
                f"Unsafe seeded fault target {item.get('target_path')!r}"
            ) from exc
        if target_path not in generated_files:
            raise EvaluationFixtureError(
                f"Seeded fault target {target_path!r} is not a scripted generated file"
            )
        generated_detection = _required_boolean(item, "expect_generated_detection")
        hidden_detection = _required_boolean(item, "expect_hidden_detection")
        if not generated_detection and not hidden_detection:
            raise EvaluationFixtureError(
                f"Seeded fault {fault_id!r} must be detected by at least one evaluator"
            )
        faults.append(
            SeededFault(
                fault_id=fault_id,
                target_path=target_path,
                replacement=_load_text_asset(
                    root,
                    _required_string(item, "replacement_file"),
                ),
                expect_generated_detection=generated_detection,
                expect_hidden_detection=hidden_detection,
            )
        )
    return tuple(faults)


def _load_text_asset(root: Path, relative_path: str) -> str:
    try:
        canonical = validate_relative_path(relative_path)
    except WorkspaceError as exc:
        raise EvaluationFixtureError(f"Unsafe evaluation asset path {relative_path!r}") from exc
    parts = canonical.split("/")
    target = root.joinpath(*parts)
    cursor = root
    for part in parts:
        cursor /= part
        if cursor.is_symlink():
            raise EvaluationFixtureError(
                f"Evaluation asset {relative_path!r} cannot use symlinks"
            )
    if not target.is_file():
        raise EvaluationFixtureError(
            f"Evaluation asset {relative_path!r} must be a regular file"
        )
    resolved = target.resolve()
    if not resolved.is_relative_to(root):
        raise EvaluationFixtureError(f"Evaluation asset {relative_path!r} escaped fixture root")
    try:
        return target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise EvaluationFixtureError(
            f"Evaluation asset {relative_path!r} must be UTF-8 text"
        ) from exc


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise EvaluationFixtureError(f"Evaluation field {key!r} must be non-empty text")
    return value


def _nonnegative_integer(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise EvaluationFixtureError(
            f"Evaluation field {key!r} must be a non-negative integer"
        )
    return value


def _required_boolean(payload: dict[str, Any], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise EvaluationFixtureError(f"Evaluation field {key!r} must be boolean")
    return value


def _requirement_ids(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise EvaluationFixtureError("Expected requirement_ids must be a list of strings")
    if len(value) != len(set(value)):
        raise EvaluationFixtureError("Expected requirement_ids must be unique")
    invalid = [item for item in value if not _REQUIREMENT_PATTERN.fullmatch(item)]
    if invalid:
        raise EvaluationFixtureError(
            f"Invalid expected requirement IDs: {', '.join(invalid)}"
        )
    return tuple(sorted(value))


def _require_exact_keys(
    payload: dict[str, Any],
    expected: set[str],
    label: str,
) -> None:
    missing = sorted(expected - set(payload))
    unknown = sorted(set(payload) - expected)
    if missing or unknown:
        details: list[str] = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if unknown:
            details.append(f"unknown: {', '.join(unknown)}")
        raise EvaluationFixtureError(f"Invalid {label} keys ({'; '.join(details)})")


def _tag(prompt: str, name: str) -> str:
    return prompt.split(f"<{name}>", 1)[1].split(f"</{name}>", 1)[0].strip()

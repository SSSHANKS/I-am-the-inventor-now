import json
import sys

import pytest

from packages.agents.base_agent import StubTextClient
from packages.agents.clean_team import (
    CleanArchitectAgent,
    CleanBehaviorProbeAgent,
    CleanBuilderAgent,
    CleanManifestAgent,
    CleanPlannerAgent,
    CleanRepairAgent,
)
from packages.modules.clean import (
    CleanBuildError,
    CleanRunner,
    ExecutableValidationPolicy,
    ExecutableValidationResult,
    LocalPythonValidationExecutor,
)
from packages.modules.clean.behavior import LocalPythonBehaviorProbeExecutor
from packages.modules.clean.manifest import architecture_sha256
from packages.modules.clean.runner import (
    _build_succeeded,
    _normalise_plan,
    _repair_context,
    _repair_paths,
    _safe_error,
)
from packages.modules.clean.workspace import CleanWorkspace, WorkspaceError
from packages.modules.handoff import export_clean_handoff

SPECIFICATION = """# Greeting Project

## Functional Requirements

- FR-001: A greeting operation returns the text hello.
"""


def test_clean_repair_diagnostic_preserves_actionable_validation_details():
    error = WorkspaceError(
        "Architecture does not allocate requirements to capabilities: AC-001, AC-002"
    )

    assert _safe_error(error) == (
        "WorkspaceError: Architecture does not allocate requirements to capabilities: "
        "AC-001, AC-002"
    )


def test_clean_repair_diagnostic_redacts_host_paths():
    error = WorkspaceError(
        r"Validation failed in C:\Users\person\private-workspace\project.py"
    )

    diagnostic = _safe_error(error)

    assert "C:\\Users" not in diagnostic
    assert "person" not in diagnostic
    assert "<host-path>" in diagnostic


def test_skipped_behavior_probes_keep_an_executable_build_partial():
    checks = [
        {
            "name": name,
            "status": "pass",
            "message": "Passed",
            "paths": [],
        }
        for name in (
            "planned-files-present",
            "no-unplanned-files",
            "entry-points-present",
            "utf8-text",
            "syntax",
            "executable-validation",
        )
    ]
    assert _build_succeeded(checks)

    checks.append(
        {
            "name": "python-behavior-probes",
            "status": "skipped",
            "message": "Probe design unavailable",
            "paths": [],
        }
    )

    assert not _build_succeeded(checks)


def test_missing_behavior_suite_is_reported_without_blocking_static_validation():
    class PassingAdapter:
        @staticmethod
        def validate_project(*_args, **_kwargs):
            return [
                {
                    "name": name,
                    "status": "pass",
                    "message": "Passed",
                    "paths": [],
                }
                for name in (
                    "planned-files-present",
                    "no-unplanned-files",
                    "entry-points-present",
                    "utf8-text",
                    "syntax",
                )
            ]

        @staticmethod
        def validate_readiness(*_args, **_kwargs):
            return [
                {
                    "name": "executable-validation",
                    "status": "pass",
                    "message": "Readiness passed",
                    "paths": [],
                }
            ]

    runner = CleanRunner.__new__(CleanRunner)
    runner.syntax_checks = True
    runner.behavior_executor = object()

    checks = runner._validate(
        None,
        {},
        [],
        runtime_adapter=PassingAdapter(),
        architecture={},
        manifest={},
        behavior_probe_design_failure="BehaviorProbeError: weak assertion",
    )
    behavior = next(
        check for check in checks if check["name"] == "python-behavior-probes"
    )

    assert behavior["status"] == "skipped"
    assert "weak assertion" in behavior["message"]
    assert all(check["status"] != "fail" for check in checks)


def _plan():
    return {
        "schema_version": 2,
        "summary": "A dependency-free greeting module.",
        "project_kind": "application",
        "runtime": {"language": "Python", "minimum_version": "3.12"},
        "dependencies": [],
        "packages": [],
        "entry_points": [{"path": "greeting.py", "description": "Greeting module"}],
        "symbol_contracts": [
            {
                "symbol_id": "SYM-001",
                "qualified_name": "greeting.greeting",
                "kind": "function",
                "signature": "greeting() -> str",
                "visibility": "public",
                "requirement_ids": ["FR-001"],
            }
        ],
        "files": [
            {
                "path": "greeting.py",
                "purpose": "Implement FR-001",
                "requirement_ids": ["FR-001"],
                "provides": ["SYM-001"],
                "requires": [],
                "depends_on": [],
                "generation_order": 1,
            }
        ],
        "validation_strategy": ["Parse Python source"],
        "open_questions": [],
    }


def _plan_with_test():
    plan = _plan()
    plan["files"].append(
        {
            "path": "tests/test_greeting.py",
            "purpose": "Verify FR-001",
            "requirement_ids": ["FR-001"],
            "provides": [],
            "requires": ["SYM-001"],
            "depends_on": ["greeting.py"],
            "generation_order": 2,
        }
    )
    return plan


def test_clean_plan_normalization_orders_dependencies_and_repairs_init_path():
    plan = _plan()
    plan["files"][0]["generation_order"] = 2
    plan["files"].append(
        {
            "path": "greeting/**init**.py",
            "purpose": "Expose the greeting API",
            "requirement_ids": ["FR-001"],
            "provides": [],
            "requires": ["SYM-001"],
            "depends_on": ["greeting.py"],
            "generation_order": 1,
        }
    )
    plan["entry_points"] = [
        {"path": "greeting/**init**.py", "description": "Package API"}
    ]

    normalized = _normalise_plan(plan)

    tasks = {item["path"]: item for item in normalized["files"]}
    assert set(tasks) == {"greeting.py", "greeting/__init__.py"}
    assert tasks["greeting.py"]["generation_order"] == 1
    assert tasks["greeting/__init__.py"]["generation_order"] == 2
    assert normalized["entry_points"][0]["path"] == "greeting/__init__.py"
    assert plan["files"][1]["path"] == "greeting/**init**.py"


def test_clean_plan_normalization_rejects_dependency_cycles():
    plan = _plan()
    plan["files"][0]["depends_on"] = ["other.py"]
    plan["files"].append(
        {
            "path": "other.py",
            "purpose": "Cycle participant",
            "requirement_ids": ["FR-001"],
            "provides": [],
            "requires": [],
            "depends_on": ["greeting.py"],
            "generation_order": 2,
        }
    )

    with pytest.raises(WorkspaceError, match="dependency cycle"):
        _normalise_plan(plan)


def test_clean_plan_normalization_infers_symbol_provider_dependencies():
    plan = _plan()
    plan["files"].append(
        {
            "path": "greeting/__init__.py",
            "purpose": "Expose the greeting API",
            "requirement_ids": ["FR-001"],
            "provides": [],
            "requires": ["SYM-001"],
            "depends_on": [],
            "generation_order": 1,
        }
    )
    plan["entry_points"] = [
        {"path": "greeting/__init__.py", "description": "Package API"}
    ]

    normalized = _normalise_plan(plan)

    tasks = {item["path"]: item for item in normalized["files"]}
    assert tasks["greeting/__init__.py"]["depends_on"] == ["greeting.py"]
    assert tasks["greeting.py"]["generation_order"] == 1
    assert tasks["greeting/__init__.py"]["generation_order"] == 2


def test_clean_plan_normalization_inherits_dependency_requirements_for_tests():
    plan = _plan_with_test()
    plan["files"][1]["requirement_ids"] = ["TC-001"]

    normalized = _normalise_plan(plan)

    tasks = {item["path"]: item for item in normalized["files"]}
    assert tasks["tests/test_greeting.py"]["requirement_ids"] == ["FR-001", "TC-001"]


def test_clean_plan_normalization_recovers_unique_test_candidate_allocation():
    specification = SPECIFICATION + """

## Test Candidates

- TC-001: Execute the greeting and inspect its output.
"""
    plan = _plan_with_test()
    plan["files"][1]["purpose"] = "Verify greeting output behavior"

    normalized = _normalise_plan(plan, specification=specification)

    tasks = {item["path"]: item for item in normalized["files"]}
    assert tasks["tests/test_greeting.py"]["requirement_ids"] == ["FR-001", "TC-001"]


def test_clean_plan_normalization_does_not_guess_unrelated_test_candidate():
    specification = SPECIFICATION + """

## Test Candidates

- TC-001: Parse a configuration file and inspect its settings.
"""
    normalized = _normalise_plan(_plan_with_test(), specification=specification)

    tasks = {item["path"]: item for item in normalized["files"]}
    assert tasks["tests/test_greeting.py"]["requirement_ids"] == ["FR-001"]


def test_clean_plan_normalization_does_not_guess_ambiguous_test_candidate():
    specification = SPECIFICATION + """

## Test Candidates

- TC-001: Execute the greeting and inspect its output.
"""
    plan = _plan_with_test()
    plan["files"][1]["purpose"] = "Verify greeting output behavior"
    duplicate = dict(plan["files"][1])
    duplicate["path"] = "tests/test_greeting_output.py"
    duplicate["requirement_ids"] = list(duplicate["requirement_ids"])
    duplicate["requires"] = list(duplicate["requires"])
    duplicate["depends_on"] = list(duplicate["depends_on"])
    duplicate["generation_order"] = 3
    plan["files"].append(duplicate)

    normalized = _normalise_plan(plan, specification=specification)

    test_tasks = [
        item for item in normalized["files"] if item["path"].startswith("tests/")
    ]
    assert all("TC-001" not in task["requirement_ids"] for task in test_tasks)


def test_clean_plan_normalization_adds_supplemental_behavior_test():
    plan = _plan()
    plan["files"].append(
        {
            "path": "greeting/__init__.py",
            "purpose": "Re-export the greeting API",
            "requirement_ids": ["FR-001"],
            "provides": [],
            "requires": ["SYM-001"],
            "depends_on": ["greeting.py"],
            "generation_order": 2,
        }
    )
    normalized = _normalise_plan(
        plan,
        specification=SPECIFICATION,
        ensure_python_test_coverage=True,
    )

    tasks = {item["path"]: item for item in normalized["files"]}
    supplemental = tasks["tests/test_greeting_requirements.py"]
    assert supplemental["requirement_ids"] == ["FR-001"]
    assert supplemental["requires"] == ["SYM-001"]
    assert supplemental["depends_on"] == ["greeting.py"]
    assert tasks["greeting.py"]["generation_order"] < supplemental["generation_order"]


def test_clean_plan_normalization_preserves_existing_behavior_coverage():
    normalized = _normalise_plan(
        _plan_with_test(),
        specification=SPECIFICATION,
        ensure_python_test_coverage=True,
    )

    test_paths = [
        item["path"] for item in normalized["files"] if item["path"].startswith("tests/")
    ]
    assert test_paths == ["tests/test_greeting.py"]


def test_clean_plan_normalization_generates_all_implementation_before_tests():
    plan = _plan_with_test()
    plan["files"].append(
        {
            "path": "support.py",
            "purpose": "Additional implementation support",
            "requirement_ids": [],
            "provides": [],
            "requires": [],
            "depends_on": ["greeting.py"],
            "generation_order": 3,
        }
    )

    normalized = _normalise_plan(plan)

    order = {
        item["path"]: item["generation_order"] for item in normalized["files"]
    }
    assert order["greeting.py"] < order["support.py"] < order["tests/test_greeting.py"]


def test_clean_plan_normalization_rejects_implementation_depending_on_tests():
    plan = _plan_with_test()
    plan["files"][0]["depends_on"] = ["tests/test_greeting.py"]

    with pytest.raises(WorkspaceError, match="depends on test files"):
        _normalise_plan(plan)


def test_clean_plan_normalization_adds_declared_package_scaffold():
    plan = _plan()
    plan["project_kind"] = "library"
    plan["packages"] = [
        {"import_name": "greeting", "purpose": "Public greeting package"}
    ]

    normalized = _normalise_plan(plan)

    tasks = {item["path"]: item for item in normalized["files"]}
    assert "pyproject.toml" in tasks
    assert "README.md" in tasks
    assert tasks["pyproject.toml"]["requirement_ids"] == []
    assert tasks["README.md"]["requirement_ids"] == []


def test_clean_runner_rejects_combined_test_candidates(tmp_path):
    specification = """# Example

## Functional Requirements

- FR-001: Execute a greeting command.

## Test Candidates

- TC-001: Execute the greeting command and inspect its output.
- TC-002: Parse a configuration file and inspect its settings.
"""
    plan = _plan_with_test()
    plan["files"][1]["requirement_ids"].extend(["TC-001", "TC-002"])
    handoff = export_clean_handoff(
        tmp_path / "handoff", specification, {"status": "pass"}
    )
    runner = CleanRunner(
        _agent(CleanPlannerAgent, json.dumps(plan)),
        _agent(CleanBuilderAgent, "{}"),
        _agent(CleanRepairAgent, "{}"),
    )

    with pytest.raises(CleanBuildError, match="combines distinct test candidates"):
        runner.run(handoff, tmp_path / "output")


def test_clean_runner_rejects_semantically_unrelated_test_candidate(tmp_path):
    specification = """# Example

## Functional Requirements

- FR-001: Execute a greeting command.

## Test Candidates

- TC-001: Parse a configuration file and inspect its settings.
"""
    plan = _plan_with_test()
    plan["files"][1]["requirement_ids"].append("TC-001")
    handoff = export_clean_handoff(
        tmp_path / "handoff", specification, {"status": "pass"}
    )
    runner = CleanRunner(
        _agent(CleanPlannerAgent, json.dumps(plan)),
        _agent(CleanBuilderAgent, "{}"),
        _agent(CleanRepairAgent, "{}"),
    )

    with pytest.raises(CleanBuildError, match="does not correspond"):
        runner.run(handoff, tmp_path / "output")


def _agent(agent_type, response):
    return agent_type(
        model="stub/model",
        profile_path=None,
        retry_profile_path=None,
        chat_client=StubTextClient([response]),
    )


def _assert_plan_rejected(tmp_path, plan, message):
    handoff = export_clean_handoff(
        tmp_path / "handoff", SPECIFICATION, {"status": "pass"}
    )
    output = tmp_path / "output"
    runner = CleanRunner(
        _agent(CleanPlannerAgent, json.dumps(plan)),
        _agent(CleanBuilderAgent, "{}"),
        _agent(CleanRepairAgent, "{}"),
    )

    with pytest.raises(CleanBuildError, match=message):
        runner.run(handoff, output)

    assert not output.exists()


def test_clean_runner_labels_unidentified_requirements_before_planning(tmp_path):
    specification = """# Greeting Project

## Functional Requirements

- A greeting operation returns the text hello.
"""
    handoff = export_clean_handoff(
        tmp_path / "handoff", specification, {"status": "pass"}
    )
    planner_client = StubTextClient([json.dumps(_plan())])
    runner = CleanRunner(
        CleanPlannerAgent(model="stub/model", chat_client=planner_client),
        _agent(
            CleanBuilderAgent,
            json.dumps(
                {
                    "files": [
                        {
                            "path": "greeting.py",
                            "content": "def greeting() -> str:\n    return 'hello'\n",
                            "requirement_ids": ["FR-001"],
                        }
                    ],
                    "notes": [],
                }
            ),
        ),
        _agent(CleanRepairAgent, "{}"),
    )

    result = runner.run(handoff, tmp_path / "output")

    assert result.status == "partial"
    assert "FR-001: A greeting operation" in planner_client.prompts[0]
    catalogue = json.loads(
        (tmp_path / "output" / "_clean" / "requirements.json").read_text()
    )
    assert catalogue["requirement_ids"] == ["FR-001"]
    assert catalogue["generated_labels"][0]["source_line"] == 5


class StubValidationExecutor:
    def __init__(self, result, *, plan_issues=(), policy=None):
        self.result = result
        self.calls = []
        self.plan_calls = []
        self.plan_issues = tuple(plan_issues)
        self.policy = policy or ExecutableValidationPolicy(
            adapter="stub",
            supported_languages=("any",),
            capabilities=("scripted-validation",),
            requirements=(),
        )

    def planning_policy(self):
        return self.policy

    def validate_plan(self, plan, requirement_ids):
        self.plan_calls.append((plan, requirement_ids))
        return self.plan_issues

    def validate(self, project_root, plan):
        self.calls.append((project_root, plan))
        return self.result


def test_clean_normalization_supplies_test_coverage_for_executable_validation():
    normalized = _normalise_plan(
        _plan(),
        specification=SPECIFICATION,
        ensure_python_test_coverage=True,
    )
    executor = LocalPythonValidationExecutor(python_executable=sys.executable)

    issues = executor.validate_plan(normalized, {"FR-001"})

    assert issues == ()


def test_clean_runner_rejects_tests_disconnected_from_implementation(tmp_path):
    plan = _plan_with_test()
    plan["files"][1]["requires"] = []
    plan["files"][1]["depends_on"] = []
    handoff = export_clean_handoff(
        tmp_path / "handoff", SPECIFICATION, {"status": "pass"}
    )
    runner = CleanRunner(
        _agent(CleanPlannerAgent, json.dumps(plan)),
        _agent(CleanBuilderAgent, "{}"),
        _agent(CleanRepairAgent, "{}"),
        validation_executor=LocalPythonValidationExecutor(
            python_executable=sys.executable
        ),
    )

    with pytest.raises(CleanBuildError, match="does not depend on an implementation file"):
        runner.run(handoff, tmp_path / "output")

    assert not (tmp_path / "output").exists()


def test_clean_runner_delegates_plan_policy_to_generic_executor(tmp_path):
    handoff = export_clean_handoff(
        tmp_path / "handoff", SPECIFICATION, {"status": "pass"}
    )
    planner_client = StubTextClient([json.dumps(_plan())])
    executor = StubValidationExecutor(
        ExecutableValidationResult(True, "unused"),
        plan_issues=("a build manifest is required",),
        policy=ExecutableValidationPolicy(
            adapter="example-build",
            supported_languages=("ExampleLang",),
            capabilities=("compile", "test"),
            requirements=("Plan a build manifest.",),
        ),
    )
    runner = CleanRunner(
        CleanPlannerAgent(model="stub/model", chat_client=planner_client),
        _agent(CleanBuilderAgent, "{}"),
        _agent(CleanRepairAgent, "{}"),
        validation_executor=executor,
    )

    with pytest.raises(CleanBuildError, match="a build manifest is required"):
        runner.run(handoff, tmp_path / "output")

    assert executor.plan_calls[0][1] == {"FR-001"}
    policy = json.loads(
        planner_client.prompts[0]
        .split("<executable_validation_policy>", 1)[1]
        .split("</executable_validation_policy>", 1)[0]
    )
    assert policy == executor.policy.to_prompt_dict()
    assert not (tmp_path / "output").exists()


def test_clean_agents_keep_the_shared_base_constructor():
    assert "__init__" not in CleanArchitectAgent.__dict__
    assert "__init__" not in CleanPlannerAgent.__dict__
    assert "__init__" not in CleanBuilderAgent.__dict__
    assert "__init__" not in CleanManifestAgent.__dict__
    assert "__init__" not in CleanRepairAgent.__dict__


def test_clean_runner_builds_repairs_without_claiming_static_success_or_reading_dirty_data(
    tmp_path,
):
    run_root = tmp_path / "dirty-run"
    run_root.mkdir()
    canary = "ORIGINAL-SECRET-CANARY"
    (run_root / "dirty-index.json").write_text(canary, encoding="utf-8")
    (run_root / "border_verdict.json").write_text(canary, encoding="utf-8")
    (run_root / "_private").mkdir()
    (run_root / "_private" / "alias_map.json").write_text(canary, encoding="utf-8")
    handoff = export_clean_handoff(
        run_root / "clean_handoff",
        SPECIFICATION,
        {"status": "pass"},
        compatibility_mode="drop-in",
    )

    planner_client = StubTextClient([json.dumps(_plan())])
    builder_client = StubTextClient(
        [json.dumps(
            {
                "files": [
                    {
                        "path": "greeting.py",
                        "content": "def greeting(:\n",
                        "requirement_ids": ["FR-001"],
                    }
                ],
                "notes": [],
            }
        )]
    )
    repair_client = StubTextClient(
        [json.dumps(
            {
                "replacements": [
                    {
                        "path": "greeting.py",
                        "content": 'def greeting() -> str:\n    return "hello"\n',
                    }
                ],
                "rationale": "Correct the Python syntax.",
            }
        )]
    )
    planner = CleanPlannerAgent(model="stub/model", chat_client=planner_client)
    builder = CleanBuilderAgent(model="stub/model", chat_client=builder_client)
    repairer = CleanRepairAgent(model="stub/model", chat_client=repair_client)

    result = CleanRunner(planner, builder, repairer, max_repairs=2).run(
        handoff, tmp_path / "clean-output"
    )

    assert not result.succeeded
    assert result.status == "partial"
    assert result.report["schema_version"] == 4
    assert result.report["compatibility_mode"] == "drop-in"
    assert result.report["validation_level"] == "syntax"
    executable = next(
        check for check in result.report["checks"] if check["name"] == "executable-validation"
    )
    assert executable["status"] == "skipped"
    assert result.report["repair_rounds"] == 1
    assert result.report["requirements"][0]["status"] == "partial"
    assert (result.project_root / "greeting.py").read_text(encoding="utf-8").endswith(
        'def greeting() -> str:\n    return "hello"\n'
    )
    all_prompts = planner_client.prompts + builder_client.prompts + repair_client.prompts
    assert SPECIFICATION in planner_client.prompts[0]
    for prompt in builder_client.prompts + repair_client.prompts:
        assert "<relevant_specification_context>" in prompt
        assert "FR-001: A greeting operation returns the text hello." in prompt
    assert all("<compatibility_mode>\ndrop-in\n</compatibility_mode>" in prompt for prompt in all_prompts)
    assert all(canary not in prompt for prompt in all_prompts)
    disabled_policy = json.loads(
        planner_client.prompts[0]
        .split("<executable_validation_policy>", 1)[1]
        .split("</executable_validation_policy>", 1)[0]
    )
    assert disabled_policy == {"enabled": False}
    assert planner.source_reader is None and planner.alias_map is None
    assert builder.source_reader is None and builder.alias_map is None
    assert repairer.source_reader is None and repairer.alias_map is None


def test_clean_runner_reports_failure_when_repair_budget_is_zero(tmp_path):
    handoff = export_clean_handoff(
        tmp_path / "handoff", SPECIFICATION, {"status": "pass"}
    )
    planner = _agent(CleanPlannerAgent, json.dumps(_plan()))
    builder = _agent(
        CleanBuilderAgent,
        json.dumps(
            {
                "files": [
                    {
                        "path": "greeting.py",
                        "content": "def broken(:\n",
                        "requirement_ids": ["FR-001"],
                    }
                ],
                "notes": [],
            }
        ),
    )
    repairer = _agent(CleanRepairAgent, "{}")

    result = CleanRunner(planner, builder, repairer, max_repairs=0).run(
        handoff, tmp_path / "output"
    )

    assert not result.succeeded
    assert result.report["status"] == "partial"
    assert result.report["repair_stop_reason"] == "repair-budget-exhausted"
    assert result.report_path.is_file()


def test_clean_runner_rejects_changed_content_with_same_failure_then_retries(tmp_path):
    handoff = export_clean_handoff(
        tmp_path / "handoff", SPECIFICATION, {"status": "pass"}
    )
    builder = _agent(
        CleanBuilderAgent,
        json.dumps(
            {
                "files": [
                    {
                        "path": "greeting.py",
                        "content": (
                            "def greeting() -> bytes:\n"
                            "    return b'wrong'\n"
                        ),
                        "requirement_ids": ["FR-001"],
                    }
                ],
                "notes": [],
            }
        ),
    )
    repair_client = StubTextClient(
        [
            json.dumps(
                {
                    "replacements": [
                        {
                            "path": "greeting.py",
                            "content": (
                                "def greeting() -> bytes:\n"
                                "    return b'still wrong'\n"
                            ),
                        }
                    ],
                    "rationale": "Changed the body without resolving the contract.",
                }
            ),
            json.dumps(
                {
                    "replacements": [
                        {
                            "path": "greeting.py",
                            "content": (
                                "def greeting() -> str:\n"
                                "    return 'hello'\n"
                            ),
                        }
                    ],
                    "rationale": "Use the remaining repair attempt to fix the contract.",
                }
            ),
        ]
    )
    runner = CleanRunner(
        _agent(CleanPlannerAgent, json.dumps(_plan())),
        builder,
        CleanRepairAgent(model="stub/model", chat_client=repair_client),
        max_repairs=2,
    )

    result = runner.run(handoff, tmp_path / "output")

    assert not any(
        check["status"] == "fail" for check in result.report["checks"]
    )
    assert result.report["repair_rounds"] == 2
    assert result.report["repair_stop_reason"] is None
    assert repair_client.call_count == 2
    record = json.loads(
        (result.output_root / "_clean/iterations/repair-1.json").read_text(
            encoding="utf-8"
        )
    )
    assert record["outcome"] == "rejected"
    assert record["stop_reason"] is None
    assert record["changed_paths"] == []
    assert record["candidate_changed_paths"] == ["greeting.py"]
    assert any("does not resolve any" in item for item in record["regressions"])
    assert (
        record["before_failure_fingerprint"]
        == record["after_failure_fingerprint"]
    )
    assert (
        record["before_content_hashes"]["greeting.py"]
        == record["after_content_hashes"]["greeting.py"]
    )
    assert "<repair_attempt>\n2\n</repair_attempt>" in repair_client.prompts[1]
    assert "repair-candidate-rejected" in repair_client.prompts[1]
    assert "candidate was not published" in repair_client.prompts[1]


def test_clean_runner_rejects_out_of_scope_repair_then_retries(tmp_path):
    handoff = export_clean_handoff(
        tmp_path / "handoff", SPECIFICATION, {"status": "pass"}
    )
    builder = _agent(
        CleanBuilderAgent,
        json.dumps({
            "files": [{
                "path": "greeting.py",
                "content": "def greeting() -> bytes:\n    return b'wrong'\n",
                "requirement_ids": ["FR-001"],
            }],
            "notes": [],
        }),
    )
    repair_client = StubTextClient([
        json.dumps({
            "replacements": [{"path": "unrelated.py", "content": "value = 1\n"}],
            "rationale": "Changed an unrelated path.",
        }),
        json.dumps({
            "replacements": [{
                "path": "greeting.py",
                "content": "def greeting() -> str:\n    return 'hello'\n",
            }],
            "rationale": "Repair the allowed provider.",
        }),
    ])
    runner = CleanRunner(
        _agent(CleanPlannerAgent, json.dumps(_plan())),
        builder,
        CleanRepairAgent(model="stub/model", chat_client=repair_client),
        max_repairs=2,
    )

    result = runner.run(handoff, tmp_path / "output")

    assert not any(check["status"] == "fail" for check in result.report["checks"])
    assert not (result.project_root / "unrelated.py").exists()
    record = json.loads(
        (result.output_root / "_clean/iterations/repair-1.json").read_text(
            encoding="utf-8"
        )
    )
    assert record["outcome"] == "rejected"
    assert record["candidate_changed_paths"] == ["unrelated.py"]
    assert "outside its failure task" in record["regressions"][0]
    assert "candidate was not published" in repair_client.prompts[1]


def test_clean_runner_preserves_validation_state_after_transient_model_failure(tmp_path):
    from packages.agents.base_agent import ModelCallError

    handoff = export_clean_handoff(
        tmp_path / "handoff", SPECIFICATION, {"status": "pass"}
    )
    builder = _agent(
        CleanBuilderAgent,
        json.dumps({
            "files": [{
                "path": "greeting.py",
                "content": "def greeting() -> bytes:\n    return b'wrong'\n",
                "requirement_ids": ["FR-001"],
            }],
            "notes": [],
        }),
    )

    class Repairer:
        source_reader = alias_map = artifact_verifier = None

        def __init__(self):
            self.calls = 0

        def repair_files(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise ModelCallError("temporary timeout", kind="server", status_code=503)
            return {
                "replacements": [{
                    "path": "greeting.py",
                    "content": "def greeting() -> str:\n    return 'hello'\n",
                }],
                "rationale": "Repair the contract after the transient failure.",
            }

    repairer = Repairer()
    runner = CleanRunner(
        _agent(CleanPlannerAgent, json.dumps(_plan())),
        builder,
        repairer,
        max_repairs=2,
    )

    result = runner.run(handoff, tmp_path / "output")

    assert not any(check["status"] == "fail" for check in result.report["checks"])
    assert repairer.calls == 2
    record = json.loads(
        (result.output_root / "_clean/iterations/repair-1.json").read_text(
            encoding="utf-8"
        )
    )
    assert record["outcome"] == "model-call-failed"
    assert record["error_kind"] == "server"
    assert record["before_failure_fingerprint"] == record["after_failure_fingerprint"]


def test_clean_repair_receives_read_only_dependent_context(tmp_path):
    handoff = export_clean_handoff(
        tmp_path / "handoff", SPECIFICATION, {"status": "pass"}
    )
    plan = _plan()
    plan["files"].append(
        {
            "path": "tests/test_greeting.py",
            "purpose": "Verify FR-001",
            "requirement_ids": ["FR-001"],
            "provides": [],
            "requires": ["SYM-001"],
            "depends_on": ["greeting.py"],
            "generation_order": 2,
        }
    )
    builder = CleanBuilderAgent(
        model="stub/model",
        chat_client=StubTextClient(
            [
                json.dumps(
                    {
                        "files": [
                            {
                                "path": "greeting.py",
                                "content": "def greeting(:\n",
                                "requirement_ids": ["FR-001"],
                            }
                        ],
                        "notes": [],
                    }
                ),
                json.dumps(
                    {
                        "files": [
                            {
                                "path": "tests/test_greeting.py",
                                "content": (
                                    "from greeting import greeting\n\n\n"
                                    "def test_greeting():\n"
                                    "    assert greeting() == 'hello'\n"
                                ),
                                "requirement_ids": ["FR-001"],
                            }
                        ],
                        "notes": [],
                    }
                ),
            ]
        ),
    )
    repair_client = StubTextClient(
        [
            json.dumps(
                {
                    "replacements": [
                        {
                            "path": "greeting.py",
                            "content": 'def greeting() -> str:\n    return "hello"\n',
                        }
                    ],
                    "rationale": "Restore the contracted provider.",
                }
            )
        ]
    )
    runner = CleanRunner(
        _agent(CleanPlannerAgent, json.dumps(plan)),
        builder,
        CleanRepairAgent(model="stub/model", chat_client=repair_client),
        max_repairs=1,
    )

    result = runner.run(handoff, tmp_path / "output")

    assert result.report["repair_rounds"] == 1
    prompt = repair_client.prompts[0]
    related_block = prompt.split("<related_generated_files_read_only>", 1)[1].split(
        "</related_generated_files_read_only>", 1
    )[0]
    assert json.loads(related_block) == {
        "tests/test_greeting.py": (
            "from greeting import greeting\n\n\n"
            "def test_greeting():\n"
            "    assert greeting() == 'hello'\n"
        )
    }
    assert "tests/test_greeting.py" not in json.loads(
        prompt.split("<allowed_paths>", 1)[1].split("</allowed_paths>", 1)[0]
    )
    repair_record = json.loads(
        (result.output_root / "_clean/iterations/repair-1.json").read_text(encoding="utf-8")
    )
    assert repair_record["related_paths"] == ["tests/test_greeting.py"]
    assert repair_record["omitted_related_paths"] == []
    assert repair_record["requirement_ids"] == ["FR-001"]
    assert repair_record["symbol_ids"] == ["SYM-001"]


def test_clean_repair_context_includes_direct_dependencies_only(tmp_path, monkeypatch):
    plan = _plan()
    plan["files"].extend(
        [
            {
                "path": "consumer.py",
                "purpose": "Consume greeting",
                "requirement_ids": ["FR-001"],
                "provides": [],
                "requires": ["SYM-001"],
                "depends_on": ["greeting.py"],
                "generation_order": 2,
            },
            {
                "path": "unrelated.py",
                "purpose": "Unrelated support",
                "requirement_ids": [],
                "provides": [],
                "requires": [],
                "depends_on": [],
                "generation_order": 3,
            },
        ]
    )
    workspace = CleanWorkspace(tmp_path / "output")
    workspace.write_generated_file("greeting.py", "def greeting() -> str:\n    return 'hello'\n")
    workspace.write_generated_file("consumer.py", "from greeting import greeting\n")
    workspace.write_generated_file("unrelated.py", "VALUE = 1\n")

    related, omitted = _repair_context(workspace, plan, {"consumer.py"})

    assert related == {"greeting.py": "def greeting() -> str:\n    return 'hello'\n"}
    assert omitted == []

    monkeypatch.setattr("packages.modules.clean.runner.MAX_DEPENDENCY_CONTEXT_BYTES", 1)
    related, omitted = _repair_context(workspace, plan, {"consumer.py"})

    assert related == {}
    assert omitted == ["greeting.py"]


def test_pytest_assertion_failure_repairs_implementation_and_freezes_test():
    plan = _plan_with_test()
    checks = [
        {
            "name": "executable-validation",
            "status": "fail",
            "message": "pytest-suite failed: exited 1",
            "paths": ["greeting.py", "tests/test_greeting.py"],
        }
    ]

    assert _repair_paths(checks, plan) == {"greeting.py"}


def test_pytest_assertion_failure_maps_test_to_implementation_dependency():
    plan = _plan_with_test()
    checks = [
        {
            "name": "executable-validation",
            "status": "fail",
            "message": "pytest-suite failed: exited 1",
            "paths": ["tests/test_greeting.py"],
        }
    ]

    assert _repair_paths(checks, plan) == {"greeting.py"}


def test_pytest_collection_failure_can_repair_test_file():
    plan = _plan_with_test()
    checks = [
        {
            "name": "executable-validation",
            "status": "fail",
            "message": "pytest-collection failed: exited 2",
            "paths": ["tests/test_greeting.py"],
        }
    ]

    assert _repair_paths(checks, plan) == {"tests/test_greeting.py"}


def test_clean_runner_rejects_unallocated_requirements_before_creating_output(tmp_path):
    handoff = export_clean_handoff(
        tmp_path / "handoff", SPECIFICATION, {"status": "pass"}
    )
    plan = _plan()
    plan["files"][0]["requirement_ids"] = []
    output = tmp_path / "output"
    runner = CleanRunner(
        _agent(CleanPlannerAgent, json.dumps(plan)),
        _agent(CleanBuilderAgent, "{}"),
        _agent(CleanRepairAgent, "{}"),
    )

    with pytest.raises(CleanBuildError, match="does not allocate"):
        runner.run(handoff, output)

    assert not output.exists()


def test_clean_runner_rejects_a_symbol_without_a_provider(tmp_path):
    plan = _plan()
    plan["files"][0]["provides"] = []

    _assert_plan_rejected(tmp_path, plan, "does not provide declared symbols")


def test_clean_runner_rejects_duplicate_symbol_providers(tmp_path):
    plan = _plan()
    plan["files"].append(
        {
            "path": "duplicate.py",
            "purpose": "Incorrect duplicate provider",
            "requirement_ids": ["FR-001"],
            "provides": ["SYM-001"],
            "requires": [],
            "depends_on": [],
            "generation_order": 2,
        }
    )

    _assert_plan_rejected(tmp_path, plan, "provides symbols more than once")


def test_clean_runner_rejects_undeclared_symbol_references(tmp_path):
    plan = _plan()
    plan["files"][0]["requires"] = ["SYM-999"]

    _assert_plan_rejected(tmp_path, plan, "references undeclared symbols")


def test_clean_runner_rejects_contract_requirements_absent_from_specification(tmp_path):
    plan = _plan()
    plan["symbol_contracts"][0]["requirement_ids"].append("FR-999")
    plan["files"][0]["requirement_ids"].append("FR-999")

    _assert_plan_rejected(tmp_path, plan, "absent from the specification")


def test_clean_runner_rejects_invalid_python_contract_signature(tmp_path):
    plan = _plan()
    plan["symbol_contracts"][0]["signature"] = "other() -> str"

    _assert_plan_rejected(tmp_path, plan, "Invalid Python contract")


def test_clean_runner_reports_structure_level_when_syntax_is_disabled(tmp_path):
    handoff = export_clean_handoff(
        tmp_path / "handoff", SPECIFICATION, {"status": "pass"}
    )
    runner = CleanRunner(
        _agent(CleanPlannerAgent, json.dumps(_plan())),
        _agent(
            CleanBuilderAgent,
            json.dumps(
                {
                    "files": [
                        {
                            "path": "greeting.py",
                            "content": "def broken(:\n",
                            "requirement_ids": ["FR-001"],
                        }
                    ],
                    "notes": [],
                }
            ),
        ),
        _agent(CleanRepairAgent, "{}"),
        syntax_checks=False,
    )

    result = runner.run(handoff, tmp_path / "output")

    assert result.status == "partial"
    assert result.report["validation_level"] == "structure"
    assert result.report["requirements"][0]["status"] == "partial"
    assert next(check for check in result.report["checks"] if check["name"] == "syntax")[
        "status"
    ] == "skipped"


def test_clean_runner_requires_a_passed_executable_gate_for_success(tmp_path):
    handoff = export_clean_handoff(
        tmp_path / "handoff", SPECIFICATION, {"status": "pass"}
    )
    executor = StubValidationExecutor(
        ExecutableValidationResult(
            passed=True,
            message="Import and generated tests passed",
            paths=("greeting.py",),
        )
    )
    builder = CleanBuilderAgent(
        model="stub/model",
        chat_client=StubTextClient(
            [
                json.dumps(
                    {
                        "files": [
                            {
                                "path": "greeting.py",
                                "content": 'def greeting() -> str:\n    return "hello"\n',
                                "requirement_ids": ["FR-001"],
                            }
                        ],
                        "notes": [],
                    }
                ),
                json.dumps(
                    {
                        "files": [
                            {
                                "path": "tests/test_greeting.py",
                                "content": (
                                    "from greeting import greeting\n\n\n"
                                    "def test_greeting():\n"
                                    "    assert greeting() == 'hello'\n"
                                ),
                                "requirement_ids": ["FR-001"],
                            }
                        ],
                        "notes": [],
                    }
                ),
            ]
        ),
    )
    runner = CleanRunner(
        _agent(CleanPlannerAgent, json.dumps(_plan_with_test())),
        builder,
        _agent(CleanRepairAgent, "{}"),
        validation_executor=executor,
    )

    result = runner.run(handoff, tmp_path / "output")

    assert result.succeeded
    assert result.report["validation_level"] == "executable"
    assert result.report["requirements"][0]["status"] == "satisfied"
    assert len(executor.calls) == 1
    assert executor.calls[0][0] == result.project_root


def test_clean_runner_reaches_success_through_real_python_validation(tmp_path):
    handoff = export_clean_handoff(
        tmp_path / "handoff", SPECIFICATION, {"status": "pass"}
    )
    plan = _plan_with_test()
    builder_client = StubTextClient(
        [
            json.dumps(
                {
                    "files": [
                        {
                            "path": "greeting.py",
                            "content": 'def greeting() -> str:\n    return "hello"\n',
                            "requirement_ids": ["FR-001"],
                        }
                    ],
                    "notes": [],
                }
            ),
            json.dumps(
                {
                    "files": [
                        {
                            "path": "tests/test_greeting.py",
                            "content": (
                                "from greeting import greeting\n\n\n"
                                "def test_greeting():\n"
                                "    assert greeting() == 'hello'\n"
                            ),
                            "requirement_ids": ["FR-001"],
                        }
                    ],
                    "notes": [],
                }
            ),
        ]
    )
    builder = CleanBuilderAgent(
        model="stub/model",
        chat_client=builder_client,
    )
    runner = CleanRunner(
        _agent(CleanPlannerAgent, json.dumps(plan)),
        builder,
        _agent(CleanRepairAgent, "{}"),
        max_repairs=0,
        validation_executor=LocalPythonValidationExecutor(
            python_executable=sys.executable,
            timeout_seconds=10,
        ),
    )

    result = runner.run(handoff, tmp_path / "output")

    assert result.succeeded
    assert result.report["validation_level"] == "executable"
    executable = next(
        check for check in result.report["checks"] if check["name"] == "executable-validation"
    )
    assert executable["status"] == "pass"
    assert "passed 1 test(s)" in executable["message"]
    first_dependency_block = builder_client.prompts[0].split(
        "<completed_dependency_files>", 1
    )[1].split("</completed_dependency_files>", 1)[0]
    assert first_dependency_block.strip() == "{}"
    first_plan_block = builder_client.prompts[0].split(
        "<scoped_clean_plan>", 1
    )[1].split("</scoped_clean_plan>", 1)[0]
    assert "tests/test_greeting.py" not in first_plan_block
    dependency_block = builder_client.prompts[1].split(
        "<completed_dependency_files>", 1
    )[1].split("</completed_dependency_files>", 1)[0]
    assert "def greeting() -> str:" in dependency_block
    generation_record = json.loads(
        (result.output_root / "_clean/generation/002.json").read_text(encoding="utf-8")
    )
    assert generation_record["path"] == "tests/test_greeting.py"
    assert generation_record["dependency_paths"] == ["greeting.py"]
    assert generation_record["requirement_ids"] == ["FR-001"]
    assert generation_record["symbol_ids"] == ["SYM-001"]


def test_project_first_behavior_failure_repairs_production_with_frozen_probe(tmp_path):
    handoff = export_clean_handoff(
        tmp_path / "handoff", SPECIFICATION, {"status": "pass"}
    )
    architecture = {
        "schema_version": 1,
        "project_profile": {
            "kind": "library",
            "language": "Python",
            "runtime_version": "3.12",
            "build_system": "none",
            "layout": "flat",
            "compatibility_mode": "renamed",
        },
        "capabilities": [
            {
                "capability_id": "CAP-001",
                "purpose": "Return the specified greeting.",
                "requirement_ids": ["FR-001"],
                "scenario_ids": [],
                "component_ids": ["CMP-001"],
            }
        ],
        "components": [
            {
                "component_id": "CMP-001",
                "purpose": "Provide greeting behavior.",
                "kind": "domain",
                "requirement_ids": ["FR-001"],
                "depends_on": [],
            }
        ],
        "contracts": [
            {
                "contract_id": "SYM-001",
                "component_id": "CMP-001",
                "qualified_name": "greeting.greeting",
                "kind": "function",
                "declaration": "greeting() -> str",
                "visibility": "public",
                "requirement_ids": ["FR-001"],
            }
        ],
        "entry_points": [
            {
                "entry_point_id": "EP-001",
                "component_id": "CMP-001",
                "description": "Public greeting function.",
                "contract_ids": ["SYM-001"],
            }
        ],
        "dependency_decisions": [],
        "proposals": [],
        "unresolved_gaps": [],
    }
    manifest = {
        "schema_version": 1,
        "architecture_sha256": "0" * 64,
        "files": [
            {
                "path": "greeting.py",
                "category": "source",
                "component_id": "CMP-001",
                "purpose": "Implement greeting behavior.",
                "requirement_ids": ["FR-001"],
                "provides": ["SYM-001"],
                "requires": [],
                "depends_on": [],
                "generation_order": 1,
                "generation_owner": "model",
                "local_validators": ["syntax", "python-contracts"],
                "integration_checks": ["python-symbol-coherence"],
            }
        ],
        "entry_points": [{"entry_point_id": "EP-001", "path": "greeting.py"}],
        "readiness_obligations": [
            {
                "obligation_id": "READY-001",
                "kind": "manifest",
                "description": "Validate the complete manifest.",
                "component_ids": ["CMP-001"],
                "paths": ["greeting.py"],
                "required": True,
            }
        ],
    }
    manifest["architecture_sha256"] = architecture_sha256(architecture)
    probes = {
        "schema_version": 1,
        "probes": [
            {
                "probe_id": "PROBE-001",
                "capability_id": "CAP-001",
                "requirement_ids": ["FR-001"],
                "scenario_ids": [],
                "code": "from greeting import greeting\nassert greeting() == 'hello'\n",
            }
        ],
    }
    probe_client = StubTextClient([json.dumps(probes)])
    builder = _agent(
        CleanBuilderAgent,
        json.dumps(
            {
                "files": [
                    {
                        "path": "greeting.py",
                        "content": "def greeting() -> bytes:\n    return b'wrong'\n",
                        "requirement_ids": ["FR-001"],
                    }
                ],
                "notes": [],
            }
        ),
    )
    repair_client = StubTextClient(
        [
            json.dumps(
                {
                    "replacements": [
                        {
                            "path": "greeting.py",
                            "content": "def greeting() -> str:\n    return 'wrong'\n",
                        }
                    ],
                    "rationale": "Restore the public contract.",
                }
            ),
            json.dumps(
                {
                    "replacements": [
                        {
                            "path": "greeting.py",
                            "content": "def greeting() -> str:\n    return 'hello'\n",
                        }
                    ],
                    "rationale": "Implement the asserted greeting result.",
                }
            ),
        ]
    )
    repairer = CleanRepairAgent(
        model="stub/model",
        chat_client=repair_client,
    )

    class PassingReadiness:
        def validate(self, project_root, architecture, manifest):
            return [
                {
                    "name": "executable-validation",
                    "status": "pass",
                    "message": "Readiness passed",
                    "paths": [],
                }
            ]

    result = CleanRunner(
        None,
        builder,
        repairer,
        architect=_agent(CleanArchitectAgent, json.dumps(architecture)),
        manifest_designer=_agent(CleanManifestAgent, json.dumps(manifest)),
        behavior_prober=CleanBehaviorProbeAgent(
            model="stub/model", chat_client=probe_client
        ),
        behavior_executor=LocalPythonBehaviorProbeExecutor(
            python_executable=sys.executable,
            timeout_seconds=10,
        ),
        readiness_executor=PassingReadiness(),
        max_repairs=1,
    ).run(handoff, tmp_path / "output")

    assert result.succeeded
    assert result.report["repair_rounds"] == 2
    assert repair_client.call_count == 2
    assert probe_client.call_count == 1
    assert json.loads(
        (result.output_root / "_clean/behavior_probes.json").read_text(
            encoding="utf-8"
        )
    ) == probes
    assert result.report["behavior_probe_sha256"]
    behavior = next(
        item
        for item in result.report["checks"]
        if item["name"] == "python-behavior-probes"
    )
    assert behavior["status"] == "pass"
    assert "<failing_behavior_probes_read_only>" in repair_client.prompts[1]
    assert "assert greeting() == 'hello'" in repair_client.prompts[1]

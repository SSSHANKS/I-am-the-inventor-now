import json
import sys

import pytest

from packages.agents.base_agent import StubTextClient
from packages.agents.clean_team import CleanBuilderAgent, CleanPlannerAgent, CleanRepairAgent
from packages.modules.clean import (
    CleanBuildError,
    CleanRunner,
    ExecutableValidationPolicy,
    ExecutableValidationResult,
    LocalPythonValidationExecutor,
)
from packages.modules.clean.runner import _normalise_plan, _repair_context
from packages.modules.clean.workspace import CleanWorkspace, WorkspaceError
from packages.modules.handoff import export_clean_handoff

SPECIFICATION = """# Greeting Project

## Functional Requirements

- FR-001: A greeting operation returns the text hello.
"""


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


def test_clean_runner_requires_test_coverage_for_executable_validation(tmp_path):
    handoff = export_clean_handoff(
        tmp_path / "handoff", SPECIFICATION, {"status": "pass"}
    )
    planner_client = StubTextClient([json.dumps(_plan())])
    runner = CleanRunner(
        CleanPlannerAgent(model="stub/model", chat_client=planner_client),
        _agent(CleanBuilderAgent, "{}"),
        _agent(CleanRepairAgent, "{}"),
        validation_executor=LocalPythonValidationExecutor(
            python_executable=sys.executable
        ),
    )

    with pytest.raises(CleanBuildError, match="Python tests do not cover"):
        runner.run(handoff, tmp_path / "output")

    assert not (tmp_path / "output").exists()
    assert (
        '"adapter": "local-python"' in planner_client.prompts[0]
    )


def test_clean_runner_rejects_incomplete_test_requirement_coverage(tmp_path):
    plan = _plan_with_test()
    plan["files"][1]["requirement_ids"] = []
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

    with pytest.raises(CleanBuildError, match="FR-001"):
        runner.run(handoff, tmp_path / "output")

    assert not (tmp_path / "output").exists()


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
    assert "__init__" not in CleanPlannerAgent.__dict__
    assert "__init__" not in CleanBuilderAgent.__dict__
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
    assert result.report["schema_version"] == 3
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
    assert result.report_path.is_file()


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


def test_clean_runner_rejects_required_symbol_without_provider_dependency(tmp_path):
    plan = _plan()
    plan["files"].append(
        {
            "path": "consumer.py",
            "purpose": "Consume greeting",
            "requirement_ids": ["FR-001"],
            "provides": [],
            "requires": ["SYM-001"],
            "depends_on": [],
            "generation_order": 2,
        }
    )

    _assert_plan_rejected(tmp_path, plan, "does not depend on its provider")


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

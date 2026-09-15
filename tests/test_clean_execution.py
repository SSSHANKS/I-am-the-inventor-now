import os
import sys

import pytest

from packages.modules.clean import LocalPythonValidationExecutor


def _plan(*paths, entry="sample/__init__.py"):
    return {
        "runtime": {"language": "Python", "minimum_version": "3.12"},
        "entry_points": [{"path": entry, "description": "Import target"}],
        "files": [{"path": path} for path in paths],
    }


def _write(root, relative, content):
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _executor(**overrides):
    return LocalPythonValidationExecutor(
        python_executable=sys.executable,
        timeout_seconds=overrides.get("timeout_seconds", 10),
        max_output_chars=overrides.get("max_output_chars", 2_000),
    )


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_local_python_executor_rejects_invalid_timeouts(timeout):
    with pytest.raises(ValueError, match="finite and positive"):
        LocalPythonValidationExecutor(timeout_seconds=timeout)


def test_local_python_executor_declares_planning_policy():
    policy = _executor().planning_policy().to_prompt_dict()

    assert policy["adapter"] == "local-python"
    assert policy["supported_languages"] == ["Python"]
    assert policy["capabilities"] == [
        "import-smoke",
        "pytest-collection",
        "pytest-suite",
    ]
    assert any("network" in requirement for requirement in policy["requirements"])


def test_local_python_executor_preflight_rejects_unsupported_runtime():
    plan = _plan("app.js", entry="app.js")
    plan["runtime"]["language"] = "JavaScript"

    issues = _executor().validate_plan(plan, {"FR-001"})

    assert issues == ("local-python does not support runtime language 'JavaScript'",)


def test_local_python_executor_treats_test_candidates_as_test_only_requirements():
    plan = {
        "runtime": {"language": "Python", "minimum_version": "3.12"},
        "entry_points": [
            {"path": "sample/__init__.py", "description": "Import target"}
        ],
        "files": [
            {
                "path": "sample/__init__.py",
                "requirement_ids": ["FR-001"],
                "depends_on": [],
            },
            {
                "path": "tests/test_sample.py",
                "requirement_ids": ["FR-001", "TC-001"],
                "depends_on": ["sample/__init__.py"],
            },
        ],
    }

    issues = _executor().validate_plan(plan, {"FR-001", "TC-001"})

    assert issues == ()


def test_local_python_executor_requires_test_candidates_on_test_files():
    plan = {
        "runtime": {"language": "Python", "minimum_version": "3.12"},
        "entry_points": [
            {"path": "sample/__init__.py", "description": "Import target"}
        ],
        "files": [
            {
                "path": "sample/__init__.py",
                "requirement_ids": ["FR-001", "TC-001"],
                "depends_on": [],
            },
            {
                "path": "tests/test_sample.py",
                "requirement_ids": ["FR-001"],
                "depends_on": ["sample/__init__.py"],
            },
        ],
    }

    issues = _executor().validate_plan(plan, {"FR-001", "TC-001"})

    assert issues == (
        "Python tests do not cover specification requirements: TC-001",
    )


def test_local_python_executor_imports_collects_and_runs_tests(tmp_path):
    project = tmp_path / "project"
    _write(project, "sample/__init__.py", "def answer():\n    return 42\n")
    _write(
        project,
        "tests/test_sample.py",
        "from sample import answer\n\n\ndef test_answer():\n    assert answer() == 42\n",
    )
    plan = _plan("sample/__init__.py", "tests/test_sample.py")

    result = _executor().validate(project, plan)

    assert result.passed
    assert "pytest collected and passed 1 test(s)" in result.message
    assert result.paths == ()


def test_local_python_executor_reports_broken_package_import(tmp_path):
    project = tmp_path / "project"
    _write(project, "sample/__init__.py", "from sample.missing import value\n")
    _write(project, "tests/test_sample.py", "def test_placeholder():\n    assert True\n")
    plan = _plan("sample/__init__.py", "tests/test_sample.py")

    result = _executor().validate(project, plan)

    assert not result.passed
    assert result.message.startswith("import-smoke failed")
    assert "sample/__init__.py" in result.paths
    assert str(project) not in result.message


def test_local_python_executor_rejects_collection_errors(tmp_path):
    project = tmp_path / "project"
    _write(project, "sample/__init__.py", "VALUE = 42\n")
    _write(project, "tests/test_sample.py", "from sample import MISSING\n")
    plan = _plan("sample/__init__.py", "tests/test_sample.py")

    result = _executor().validate(project, plan)

    assert not result.passed
    assert result.message.startswith("pytest-collection failed")
    assert "tests/test_sample.py" in result.paths


def test_local_python_executor_rejects_failing_tests(tmp_path):
    project = tmp_path / "project"
    _write(project, "sample/__init__.py", "VALUE = 42\n")
    _write(
        project,
        "tests/test_sample.py",
        "from sample import VALUE\n\n\ndef test_value():\n    assert VALUE == 0\n",
    )
    plan = _plan("sample/__init__.py", "tests/test_sample.py")

    result = _executor().validate(project, plan)

    assert not result.passed
    assert result.message.startswith("pytest-suite failed")
    assert "tests/test_sample.py" in result.paths


def test_local_python_executor_strips_parent_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("IATIN_EXECUTOR_SECRET", "must-not-cross")
    project = tmp_path / "project"
    _write(project, "sample/__init__.py", "VALUE = 42\n")
    _write(
        project,
        "tests/test_environment.py",
        (
            "import os\n\n\n"
            "def test_secret_is_absent():\n"
            "    assert 'IATIN_EXECUTOR_SECRET' not in os.environ\n"
        ),
    )
    plan = _plan("sample/__init__.py", "tests/test_environment.py")

    result = _executor().validate(project, plan)

    assert result.passed
    assert os.environ["IATIN_EXECUTOR_SECRET"] == "must-not-cross"


def test_local_python_executor_times_out(tmp_path):
    project = tmp_path / "project"
    _write(project, "sample/__init__.py", "VALUE = 42\n")
    _write(
        project,
        "tests/test_slow.py",
        "import time\n\n\ndef test_slow():\n    time.sleep(2)\n",
    )
    plan = _plan("sample/__init__.py", "tests/test_slow.py")

    result = _executor(timeout_seconds=0.5).validate(project, plan)

    assert not result.passed
    assert "timed out" in result.message


def test_local_python_executor_detects_project_mutation(tmp_path):
    project = tmp_path / "project"
    _write(project, "sample/__init__.py", "VALUE = 42\n")
    _write(
        project,
        "tests/test_mutation.py",
        (
            "from pathlib import Path\n\n"
            "Path('created-by-test.txt').write_text('changed', encoding='utf-8')\n\n"
            "def test_value():\n"
            "    assert True\n"
        ),
    )
    plan = _plan("sample/__init__.py", "tests/test_mutation.py")

    result = _executor().validate(project, plan)

    assert not result.passed
    assert "generated project was modified: created-by-test.txt" in result.message

import sys
import zipfile

from packages.modules.clean.adapters.python_readiness import (
    LocalPythonReadinessExecutor,
    _missing_wheel_modules,
)
from packages.modules.clean.workspace import CleanWorkspace


def _architecture():
    return {
        "project_profile": {
            "kind": "application",
            "language": "Python",
            "runtime_version": "3.12",
            "build_system": "none",
            "layout": "flat",
            "compatibility_mode": "renamed",
        },
        "contracts": [
            {
                "contract_id": "SYM-001",
                "component_id": "CMP-001",
                "qualified_name": "app.main",
                "kind": "function",
                "declaration": "main() -> None",
                "visibility": "public",
                "requirement_ids": ["FR-001"],
            }
        ],
        "entry_points": [
            {
                "entry_point_id": "EP-001",
                "component_id": "CMP-001",
                "description": "Application entry point",
                "contract_ids": ["SYM-001"],
            }
        ],
    }


def _manifest():
    return {
        "files": [{"path": "app.py"}],
        "entry_points": [{"entry_point_id": "EP-001", "path": "app.py"}],
    }


def _executor():
    return LocalPythonReadinessExecutor(
        python_executable=sys.executable,
        timeout_seconds=10,
    )


def _check(checks, name):
    return next(item for item in checks if item["name"] == name)


def test_python_readiness_imports_modules_and_resolves_entry_points(tmp_path):
    workspace = CleanWorkspace(tmp_path / "output")
    workspace.write_generated_file(
        "app.py",
        "def main() -> None:\n    return None\n",
    )

    checks = _executor().validate(
        workspace.project_root,
        _architecture(),
        _manifest(),
    )

    assert _check(checks, "python-package-build")["status"] == "skipped"
    assert _check(checks, "python-import-smoke")["status"] == "pass"
    assert _check(checks, "python-entry-point-runtime")["status"] == "pass"
    assert _check(checks, "python-mutation-check")["status"] == "pass"
    assert _check(checks, "executable-validation")["status"] == "pass"


def test_python_readiness_reports_import_failure_without_pytest(tmp_path):
    workspace = CleanWorkspace(tmp_path / "output")
    workspace.write_generated_file(
        "app.py",
        "import missing_runtime_dependency\n\ndef main() -> None:\n    return None\n",
    )

    checks = _executor().validate(
        workspace.project_root,
        _architecture(),
        _manifest(),
    )

    failure = _check(checks, "python-import-smoke")
    assert failure["status"] == "fail"
    assert failure["paths"] == ["app.py"]
    assert "missing_runtime_dependency" in failure["message"]
    assert _check(checks, "python-entry-point-runtime")["status"] == "skipped"
    assert _check(checks, "executable-validation")["status"] == "fail"


def test_python_readiness_rejects_import_time_project_mutation(tmp_path):
    workspace = CleanWorkspace(tmp_path / "output")
    workspace.write_generated_file(
        "app.py",
        (
            "from pathlib import Path\n\n"
            "Path(__file__).with_name('marker.txt').write_text('changed')\n\n"
            "def main() -> None:\n"
            "    return None\n"
        ),
    )

    checks = _executor().validate(
        workspace.project_root,
        _architecture(),
        _manifest(),
    )

    mutation = _check(checks, "python-mutation-check")
    assert mutation["status"] == "fail"
    assert mutation["paths"] == ["marker.txt"]
    assert _check(checks, "executable-validation")["status"] == "fail"


def test_python_readiness_skips_executable_claim_without_targets(tmp_path):
    workspace = CleanWorkspace(tmp_path / "output")
    workspace.write_generated_file("config.json", "{}\n")
    architecture = _architecture()
    architecture["contracts"] = []
    architecture["entry_points"] = []
    manifest = {"files": [{"path": "config.json"}], "entry_points": []}

    checks = _executor().validate(
        workspace.project_root,
        architecture,
        manifest,
    )

    assert _check(checks, "python-import-smoke")["status"] == "skipped"
    assert _check(checks, "executable-validation")["status"] == "skipped"


def test_python_readiness_detects_public_modules_missing_from_wheel(tmp_path):
    wheel = tmp_path / "sample.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("present.py", "")

    assert _missing_wheel_modules(wheel, ["missing", "present"]) == ["missing"]

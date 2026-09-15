import json

from clean_main import main
from packages.modules.handoff import export_clean_handoff


def test_clean_cli_stub_builds_without_api_calls_but_does_not_claim_success(tmp_path):
    handoff = export_clean_handoff(
        tmp_path / "handoff",
        "# Stub Project\n\n## Requirements\n\n- FR-001: Provide an entry point.\n",
        {"status": "pass"},
    )
    output = tmp_path / "output"

    exit_code = main([str(handoff), "--output", str(output), "--stub"])

    assert exit_code == 4
    assert (output / "project" / "app.py").is_file()
    report_path = output / "_clean" / "clean_build_report.json"
    assert report_path.is_file()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "partial"
    assert report["validation_level"] == "syntax"
    assert report["planning_mode"] == "project-first"
    assert report["runtime_adapter"] == "python-static"
    assert report["architecture_sha256"]
    assert report["manifest_sha256"]
    assert (output / "_clean" / "architecture.json").is_file()
    assert (output / "_clean" / "manifest.json").is_file()


def test_clean_cli_refuses_dirty_run_directory(tmp_path):
    dirty = tmp_path / "dirty-run"
    dirty.mkdir()
    (dirty / "specification.md").write_text("# Not approved\n", encoding="utf-8")
    (dirty / "manifest.json").write_text("{}", encoding="utf-8")

    assert main([str(dirty), "--output", str(tmp_path / "output"), "--stub"]) == 2


def test_clean_cli_project_first_publishes_validated_behavior_tests(tmp_path):
    handoff = export_clean_handoff(
        tmp_path / "handoff",
        "# Stub Project\n\n## Requirements\n\n- FR-001: Provide an entry point.\n",
        {"status": "pass"},
    )
    output = tmp_path / "output"

    exit_code = main(
        [
            str(handoff),
            "--output",
            str(output),
            "--stub",
            "--execute-generated-code",
            "--clean-max-repairs",
            "0",
        ]
    )

    assert exit_code == 0
    published_test = output / "project" / "tests/test_reconstruction_behavior.py"
    assert published_test.is_file()
    assert "test_probe_001_fr_001" in published_test.read_text(encoding="utf-8")
    report = json.loads(
        (output / "_clean" / "clean_build_report.json").read_text(encoding="utf-8")
    )
    executable = next(
        check for check in report["checks"] if check["name"] == "executable-validation"
    )
    assert report["status"] == "success"
    assert report["planning_mode"] == "project-first"
    assert executable["status"] == "pass"
    assert "readiness gates passed" in executable["message"]

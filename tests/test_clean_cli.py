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


def test_clean_cli_refuses_dirty_run_directory(tmp_path):
    dirty = tmp_path / "dirty-run"
    dirty.mkdir()
    (dirty / "specification.md").write_text("# Not approved\n", encoding="utf-8")
    (dirty / "manifest.json").write_text("{}", encoding="utf-8")

    assert main([str(dirty), "--output", str(tmp_path / "output"), "--stub"]) == 2


def test_clean_cli_stub_can_pass_local_executable_validation(tmp_path):
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
    assert (output / "project" / "tests" / "test_app.py").is_file()
    report = json.loads(
        (output / "_clean" / "clean_build_report.json").read_text(encoding="utf-8")
    )
    executable = next(
        check for check in report["checks"] if check["name"] == "executable-validation"
    )
    assert report["status"] == "success"
    assert executable["status"] == "pass"
    assert "passed 1 test(s)" in executable["message"]

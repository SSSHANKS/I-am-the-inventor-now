from pathlib import Path

import main as pipeline_main
from packages.modules.clean import CleanBuildResult


def _clean_result(tmp_path: Path, *, status: str) -> CleanBuildResult:
    output = tmp_path / "clean-output"
    return CleanBuildResult(
        status=status,
        output_root=output,
        project_root=output / "project",
        report_path=output / "_clean" / "clean_build_report.json",
        report={"status": status, "validation_level": "executable"},
    )


def test_unified_cli_enables_clean_by_default():
    args = pipeline_main.parse_args(["https://example.invalid/project.git"])

    assert args.skip_clean is False
    assert args.clean_output is None
    assert args.clean_max_repairs == 2
    assert args.execute_generated_code is False
    assert args.validation_timeout == 30.0


def test_clean_stage_receives_unified_pipeline_options(tmp_path, monkeypatch):
    captured = []
    expected = _clean_result(tmp_path, status="success")
    monkeypatch.setattr(
        pipeline_main,
        "run_clean",
        lambda args: captured.append(args) or expected,
    )
    output = tmp_path / "chosen-clean-output"
    args = pipeline_main.parse_args([
        "https://example.invalid/project.git",
        "--stub",
        "--clean-output",
        str(output),
        "--clean-max-repairs",
        "4",
        "--execute-generated-code",
        "--validation-timeout",
        "12.5",
    ])
    handoff = tmp_path / "artifacts" / "run-1" / "clean_handoff"
    specification = handoff.parent / "specification.md"

    result = pipeline_main._run_clean_stage(args, handoff, specification)

    assert result is expected
    assert len(captured) == 1
    clean_args = captured[0]
    assert clean_args.handoff == handoff
    assert clean_args.output == output
    assert clean_args.clean_max_repairs == 4
    assert clean_args.stub is True
    assert clean_args.no_validate is False
    assert clean_args.execute_generated_code is True
    assert clean_args.validation_timeout == 12.5


def test_unified_cli_returns_clean_failure_code_for_partial_build(
    tmp_path, monkeypatch, capsys
):
    specification = tmp_path / "artifacts" / "run-1" / "specification.md"
    clean = _clean_result(tmp_path, status="partial")
    monkeypatch.setattr(
        pipeline_main,
        "run",
        lambda args: pipeline_main.PipelineResult(
            specification_path=specification,
            handoff_path=specification.parent / "clean_handoff",
            clean_result=clean,
        ),
    )

    exit_code = pipeline_main.main(["https://example.invalid/project.git"])

    assert exit_code == 4
    output = capsys.readouterr().out
    assert "clean status:  partial" in output
    assert f"project:       {clean.project_root}" in output
    assert f"clean report:  {clean.report_path}" in output


def test_unified_cli_can_stop_after_the_verified_handoff(
    tmp_path, monkeypatch, capsys
):
    specification = tmp_path / "artifacts" / "run-1" / "specification.md"
    monkeypatch.setattr(
        pipeline_main,
        "run",
        lambda args: pipeline_main.PipelineResult(
            specification_path=specification,
            handoff_path=specification.parent / "clean_handoff",
            clean_result=None,
        ),
    )

    exit_code = pipeline_main.main([
        "https://example.invalid/project.git",
        "--skip-clean",
    ])

    assert exit_code == 0
    assert "clean status:" not in capsys.readouterr().out

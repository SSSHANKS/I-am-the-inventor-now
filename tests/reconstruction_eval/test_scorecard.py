from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from .scorecard import main, run_evaluation_suite, write_scorecard

FIXTURE = Path(__file__).with_name("fixtures") / "python_multi_module"


def _fixture_root(tmp_path: Path) -> Path:
    root = tmp_path / "fixtures"
    root.mkdir()
    shutil.copytree(FIXTURE, root / FIXTURE.name)
    return root


def test_evaluation_scorecard_aggregates_baseline_and_fault_metrics(tmp_path):
    scorecard = run_evaluation_suite(
        _fixture_root(tmp_path),
        tmp_path / "evaluation",
        python_executable=sys.executable,
    )

    assert scorecard.passed
    assert scorecard.totals == {
        "fixture_count": 1,
        "passed_fixture_count": 1,
        "fixture_pass_rate": 1.0,
        "hidden_oracle_passed_fixture_count": 1,
        "hidden_oracle_pass_rate": 1.0,
        "hidden_test_count": 3,
        "generated_test_file_count": 1,
        "repair_round_count": 0,
        "model_call_count": 7,
        "seeded_fault_count": 1,
        "generated_detected_fault_count": 0,
        "hidden_detected_fault_count": 1,
        "hidden_fault_detection_rate": 1.0,
    }
    fixture = scorecard.fixtures[0]
    assert fixture.baseline["check_status_counts"] == {"pass": 8}
    assert fixture.baseline["requirement_status_counts"] == {"satisfied": 5}
    assert fixture.baseline["unresolved_gap_count"] == 0
    assert fixture.seeded_faults[0]["hidden_detected"] is True


def test_scorecard_writer_refuses_to_overwrite_report(tmp_path):
    scorecard = run_evaluation_suite(
        _fixture_root(tmp_path),
        tmp_path / "evaluation",
        python_executable=sys.executable,
    )
    path = write_scorecard(scorecard, tmp_path / "evaluation")

    assert json.loads(path.read_text(encoding="utf-8"))["passed"] is True
    with pytest.raises(ValueError, match="must not already exist"):
        write_scorecard(scorecard, tmp_path / "evaluation")


def test_scorecard_cli_reports_configuration_error_for_existing_output(tmp_path, capsys):
    output = tmp_path / "existing"
    output.mkdir()

    result = main(["--fixtures", str(_fixture_root(tmp_path)), "--output", str(output)])

    assert result == 2
    assert "output path must not already exist" in capsys.readouterr().err

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from .harness import (
    expectation_failures,
    fault_expectation_failures,
    load_evaluation_fixture,
    run_evaluation_fixture,
    run_seeded_fault_evaluations,
)

FIXTURES_ROOT = Path(__file__).with_name("fixtures")
FIXTURE_PATHS = tuple(sorted(path for path in FIXTURES_ROOT.iterdir() if path.is_dir()))


@pytest.mark.parametrize("fixture_path", FIXTURE_PATHS, ids=lambda path: path.name)
def test_reconstruction_baseline(fixture_path, tmp_path):
    fixture = load_evaluation_fixture(fixture_path)

    result = run_evaluation_fixture(
        fixture,
        tmp_path / fixture.name,
        python_executable=sys.executable,
    )

    assert expectation_failures(fixture, result.metrics) == ()
    assert result.metrics.model_call_count == len(fixture.generated_files) + 1
    assert result.metrics.repair_rounds == 0
    assert result.metrics.hidden.passed


@pytest.mark.parametrize(
    "fixture_path",
    FIXTURE_PATHS,
    ids=lambda path: path.name,
)
def test_seeded_fault_detection_baseline(fixture_path, tmp_path):
    fixture = load_evaluation_fixture(fixture_path)
    if not fixture.seeded_faults:
        pytest.skip("fixture declares no seeded faults")
    baseline = run_evaluation_fixture(
        fixture,
        tmp_path / "baseline",
        python_executable=sys.executable,
    )
    original_hashes = {
        path: (baseline.project_root / path).read_bytes()
        for path in fixture.generated_files
    }

    evaluations = run_seeded_fault_evaluations(
        fixture,
        baseline,
        tmp_path / "faults",
        python_executable=sys.executable,
    )

    assert fault_expectation_failures(fixture, evaluations) == ()
    assert all(not evaluation.generated_detected for evaluation in evaluations)
    assert all(evaluation.hidden_detected for evaluation in evaluations)
    assert original_hashes == {
        path: (baseline.project_root / path).read_bytes()
        for path in fixture.generated_files
    }

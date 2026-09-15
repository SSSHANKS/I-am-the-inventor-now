"""Run all offline reconstruction fixtures and write an aggregate scorecard."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .harness import (
    EvaluationRun,
    expectation_failures,
    fault_expectation_failures,
    load_evaluation_fixture,
    run_evaluation_fixture,
    run_seeded_fault_evaluations,
)


@dataclass(frozen=True)
class FixtureScore:
    name: str
    archetype: str | None
    passed: bool
    baseline: dict[str, Any] | None
    seeded_faults: tuple[dict[str, Any], ...]
    failures: tuple[str, ...]
    error: str | None = None


@dataclass(frozen=True)
class EvaluationScorecard:
    schema_version: int
    passed: bool
    totals: dict[str, int | float]
    fixtures: tuple[FixtureScore, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_evaluation_suite(
    fixtures_root: str | Path,
    work_root: str | Path,
    *,
    python_executable: str | Path = sys.executable,
) -> EvaluationScorecard:
    """Run every fixture in a real directory without any network or model calls."""
    source = Path(fixtures_root)
    if source.is_symlink() or not source.is_dir():
        raise ValueError("Evaluation fixtures root must be a real directory")
    fixture_paths = sorted(path for path in source.iterdir() if path.is_dir())
    if not fixture_paths:
        raise ValueError("Evaluation fixtures root contains no fixture directories")

    output = Path(work_root)
    if output.exists() or output.is_symlink():
        raise ValueError("Evaluation output path must not already exist")
    output.mkdir(parents=True)

    scores = tuple(
        _run_fixture(path, output, python_executable=python_executable)
        for path in fixture_paths
    )
    totals = _totals(scores)
    return EvaluationScorecard(
        schema_version=1,
        passed=all(item.passed for item in scores),
        totals=totals,
        fixtures=scores,
    )


def write_scorecard(scorecard: EvaluationScorecard, output_root: str | Path) -> Path:
    target = Path(output_root) / "scorecard.json"
    if target.exists() or target.is_symlink():
        raise ValueError("Scorecard target must not already exist")
    target.write_text(
        json.dumps(scorecard.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


def _run_fixture(
    fixture_path: Path,
    output: Path,
    *,
    python_executable: str | Path,
) -> FixtureScore:
    try:
        fixture = load_evaluation_fixture(fixture_path)
        baseline = run_evaluation_fixture(
            fixture,
            output / fixture.name / "baseline",
            python_executable=python_executable,
        )
        baseline_failures = expectation_failures(fixture, baseline.metrics)
        fault_results = (
            run_seeded_fault_evaluations(
                fixture,
                baseline,
                output / fixture.name / "seeded-faults",
                python_executable=python_executable,
            )
            if fixture.seeded_faults
            else ()
        )
        fault_failures = fault_expectation_failures(fixture, fault_results)
        failures = (*baseline_failures, *fault_failures)
        return FixtureScore(
            name=fixture.name,
            archetype=fixture.archetype,
            passed=not failures,
            baseline=_baseline_dict(baseline),
            seeded_faults=tuple(asdict(item) for item in fault_results),
            failures=failures,
        )
    except Exception as exc:
        return FixtureScore(
            name=fixture_path.name,
            archetype=None,
            passed=False,
            baseline=None,
            seeded_faults=(),
            failures=("fixture execution raised an exception",),
            error=f"{type(exc).__name__}: {exc}",
        )


def _baseline_dict(run: EvaluationRun) -> dict[str, Any]:
    report = json.loads(run.report_path.read_text(encoding="utf-8"))
    check_counts = Counter(item["status"] for item in report["checks"])
    requirement_counts = Counter(item["status"] for item in report["requirements"])
    value = asdict(run.metrics)
    value.update(
        {
            "check_status_counts": dict(sorted(check_counts.items())),
            "requirement_status_counts": dict(sorted(requirement_counts.items())),
            "unresolved_gap_count": len(report["unresolved_gaps"]),
        }
    )
    return value


def _totals(scores: tuple[FixtureScore, ...]) -> dict[str, int | float]:
    baselines = [item.baseline for item in scores if item.baseline is not None]
    faults = [fault for item in scores for fault in item.seeded_faults]
    fixture_count = len(scores)
    passed_count = sum(item.passed for item in scores)
    hidden_passed_count = sum(bool(item["hidden"]["passed"]) for item in baselines)
    return {
        "fixture_count": fixture_count,
        "passed_fixture_count": passed_count,
        "fixture_pass_rate": passed_count / fixture_count,
        "hidden_oracle_passed_fixture_count": hidden_passed_count,
        "hidden_oracle_pass_rate": hidden_passed_count / fixture_count,
        "hidden_test_count": sum(int(item["hidden"]["tests_run"]) for item in baselines),
        "generated_test_file_count": sum(
            int(item["generated_test_file_count"]) for item in baselines
        ),
        "repair_round_count": sum(int(item["repair_rounds"]) for item in baselines),
        "model_call_count": sum(int(item["model_call_count"]) for item in baselines),
        "seeded_fault_count": len(faults),
        "generated_detected_fault_count": sum(
            bool(item["generated_detected"]) for item in faults
        ),
        "hidden_detected_fault_count": sum(
            bool(item["hidden_detected"]) for item in faults
        ),
        "hidden_fault_detection_rate": (
            sum(bool(item["hidden_detected"]) for item in faults) / len(faults)
            if faults
            else 1.0
        ),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run deterministic offline reconstruction evaluation fixtures."
    )
    parser.add_argument(
        "--fixtures",
        type=Path,
        default=Path(__file__).with_name("fixtures"),
        help="directory containing reconstruction fixtures",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        scorecard = run_evaluation_suite(
            args.fixtures,
            args.output,
            python_executable=args.python,
        )
        path = write_scorecard(scorecard, args.output)
    except (OSError, ValueError) as exc:
        print(f"evaluation configuration problem: {exc}", file=sys.stderr)
        return 2
    print(f"evaluation passed: {scorecard.passed}")
    print(f"fixtures:          {scorecard.totals['passed_fixture_count']}/{scorecard.totals['fixture_count']}")
    print(f"report:            {path.resolve()}")
    return 0 if scorecard.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

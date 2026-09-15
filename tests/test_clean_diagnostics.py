from packages.modules.clean.diagnostics import (
    CleanDiagnostic,
    content_hashes,
    failure_fingerprint,
)


def test_diagnostic_identity_is_stable_across_collection_order():
    first = CleanDiagnostic(
        stage="file-contract",
        check_id="python.contract.declaration",
        message="Signature mismatch",
        paths=("b.py", "a.py"),
        contract_ids=("SYM-002", "SYM-001"),
        requirement_ids=("FR-002", "FR-001"),
        expected="run(value: str) -> str",
        actual="run(value: bytes) -> bytes",
    )
    second = CleanDiagnostic(
        stage="file-contract",
        check_id="python.contract.declaration",
        message="Signature mismatch",
        paths=("a.py", "b.py"),
        contract_ids=("SYM-001", "SYM-002"),
        requirement_ids=("FR-001", "FR-002"),
        expected="run(value: str) -> str",
        actual="run(value: bytes) -> bytes",
    )

    assert first.diagnostic_id == second.diagnostic_id
    assert first.fingerprint == second.fingerprint


def test_failure_fingerprint_is_independent_of_check_and_diagnostic_order():
    diagnostics = [
        CleanDiagnostic(
            stage="file-validation",
            check_id="clean.syntax",
            message="Invalid Python",
            paths=("a.py",),
        ).to_dict(),
        CleanDiagnostic(
            stage="file-contract",
            check_id="python.contract.declaration",
            message="Signature mismatch",
            paths=("b.py",),
        ).to_dict(),
    ]
    first = [
        {
            "name": "syntax",
            "status": "fail",
            "message": "failed",
            "paths": ["a.py", "b.py"],
            "diagnostics": diagnostics,
        }
    ]
    second = [
        {"name": "passing", "status": "pass", "message": "Passed", "paths": []},
        {
            "name": "syntax",
            "status": "fail",
            "message": "different summary is ignored",
            "paths": ["b.py", "a.py"],
            "diagnostics": list(reversed(diagnostics)),
        },
    ]

    assert failure_fingerprint(first) == failure_fingerprint(second)


def test_content_hashes_are_path_sorted_and_content_sensitive():
    hashes = content_hashes({"b.py": "same", "a.py": "same"})

    assert list(hashes) == ["a.py", "b.py"]
    assert hashes["a.py"] == hashes["b.py"]
    assert hashes["a.py"] != content_hashes({"a.py": "changed"})["a.py"]

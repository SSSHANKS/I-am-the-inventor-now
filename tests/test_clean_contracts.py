from packages.modules.clean.validation import validate_project
from packages.modules.clean.workspace import CleanWorkspace


def _plan(signature, *, kind="function", qualified_name="sample.run"):
    return {
        "runtime": {"language": "Python", "minimum_version": "3.12"},
        "entry_points": [{"path": "sample.py", "description": "Provider"}],
        "symbol_contracts": [
            {
                "symbol_id": "SYM-001",
                "qualified_name": qualified_name,
                "kind": kind,
                "signature": signature,
                "visibility": "public",
                "requirement_ids": ["FR-001"],
            }
        ],
        "files": [
            {
                "path": "sample.py",
                "provides": ["SYM-001"],
            }
        ],
    }


def _check(tmp_path, content, plan):
    workspace = CleanWorkspace(tmp_path / "output")
    workspace.write_generated_file("sample.py", content)
    checks = validate_project(workspace, plan)
    return next(check for check in checks if check["name"] == "python-contracts")


def test_python_contracts_accept_exact_function_signature(tmp_path):
    plan = _plan("run(value: str, count: int = 1) -> str")

    check = _check(
        tmp_path,
        "def run(value: str, count: int = 1) -> str:\n    return value * count\n",
        plan,
    )

    assert check["status"] == "pass"


def test_python_contracts_reject_missing_provider_definition(tmp_path):
    check = _check(tmp_path, "def other() -> str:\n    return 'x'\n", _plan("run() -> str"))

    assert check["status"] == "fail"
    assert "does not define contracted symbol 'run'" in check["message"]
    assert check["paths"] == ["sample.py"]


def test_python_contracts_reject_wrong_symbol_kind(tmp_path):
    check = _check(tmp_path, "class run:\n    pass\n", _plan("run() -> str"))

    assert check["status"] == "fail"
    assert "wrong kind for function contract" in check["message"]


def test_python_contracts_reject_function_signature_drift(tmp_path):
    check = _check(
        tmp_path,
        "def run(value: bytes) -> bytes:\n    return value\n",
        _plan("run(value: str) -> str"),
    )

    assert check["status"] == "fail"
    assert "arguments do not match" in check["message"]


def test_python_contracts_validate_class_constructor(tmp_path):
    plan = _plan("Service(value: str)", kind="class", qualified_name="sample.Service")

    check = _check(
        tmp_path,
        (
            "class Service:\n"
            "    def __init__(self, value: str):\n"
            "        self.value = value\n"
        ),
        plan,
    )

    assert check["status"] == "pass"


def test_python_contracts_accept_none_return_for_constructor_style_class(tmp_path):
    plan = _plan(
        "Service(value: str) -> None",
        kind="class",
        qualified_name="sample.Service",
    )

    check = _check(
        tmp_path,
        (
            "class Service:\n"
            "    def __init__(self, value: str) -> None:\n"
            "        self.value = value\n"
        ),
        plan,
    )

    assert check["status"] == "pass"


def test_python_contracts_reject_value_return_for_constructor_style_class(tmp_path):
    plan = _plan(
        "Service(value: str) -> str",
        kind="class",
        qualified_name="sample.Service",
    )

    check = _check(tmp_path, "class Service:\n    pass\n", plan)

    assert check["status"] == "fail"
    assert "may only return None" in check["message"]


def test_python_contracts_validate_class_declaration_bases(tmp_path):
    plan = _plan(
        "class NonClosingTextIO(io.TextIOWrapper): ...",
        kind="class",
        qualified_name="sample.NonClosingTextIO",
    )

    check = _check(
        tmp_path,
        (
            "import io\n\n"
            "class NonClosingTextIO(io.TextIOWrapper):\n"
            "    pass\n"
        ),
        plan,
    )

    assert check["status"] == "pass"


def test_python_contracts_reject_class_declaration_base_drift(tmp_path):
    plan = _plan(
        "class NonClosingTextIO(io.TextIOWrapper): ...",
        kind="class",
        qualified_name="sample.NonClosingTextIO",
    )

    check = _check(
        tmp_path,
        "class NonClosingTextIO(object):\n    pass\n",
        plan,
    )

    assert check["status"] == "fail"
    assert "base classes do not match" in check["message"]


def test_python_class_protocol_does_not_constrain_undeclared_bases(tmp_path):
    plan = _plan(
        "class Service:\n    def run(self) -> bool:\n        ...",
        kind="class",
        qualified_name="sample.Service",
    )

    check = _check(
        tmp_path,
        (
            "class Base:\n"
            "    pass\n\n"
            "class Service(Base):\n"
            "    def run(self) -> bool:\n"
            "        return True\n"
        ),
        plan,
    )

    assert check["status"] == "pass"


def test_python_contracts_accept_compact_class_constructor_declaration(tmp_path):
    signature = (
        "class StreamFixupHelper: def __init__(self, stream: object, "
        "force_readable: bool | None = None, "
        "force_writable: bool | None = None) -> None: ..."
    )
    plan = _plan(
        signature,
        kind="class",
        qualified_name="sample.StreamFixupHelper",
    )

    check = _check(
        tmp_path,
        (
            "class StreamFixupHelper:\n"
            "    def __init__(\n"
            "        self,\n"
            "        stream: object,\n"
            "        force_readable: bool | None = None,\n"
            "        force_writable: bool | None = None,\n"
            "    ) -> None:\n"
            "        self.stream = stream\n"
        ),
        plan,
    )

    assert check["status"] == "pass"


def test_python_contracts_reject_compact_constructor_drift(tmp_path):
    plan = _plan(
        "class Service: def __init__(self, value: str) -> None: ...",
        kind="class",
        qualified_name="sample.Service",
    )

    check = _check(
        tmp_path,
        "class Service:\n    def __init__(self, value: bytes) -> None:\n        pass\n",
        plan,
    )

    assert check["status"] == "fail"
    assert "constructor arguments do not match" in check["message"]


def test_python_contracts_accept_markdown_mangled_class_protocol(tmp_path):
    signature = r"""class StreamFixupHelper:
    def **init**(self, stream: object, force\_readable: bool | None = None, force\_writable: bool | None = None):
        pass
    def readable(self) -> bool:
        pass
    def writable(self) -> bool:
        pass
    def seekable(self) -> bool:
        pass"""
    plan = _plan(
        signature,
        kind="class",
        qualified_name="sample.StreamFixupHelper",
    )

    check = _check(
        tmp_path,
        (
            "class StreamFixupHelper:\n"
            "    def __init__(self, stream: object, force_readable: bool | None = None, "
            "force_writable: bool | None = None):\n"
            "        self.stream = stream\n"
            "    def readable(self) -> bool:\n"
            "        return True\n"
            "    def writable(self) -> bool:\n"
            "        return True\n"
            "    def seekable(self) -> bool:\n"
            "        return True\n"
        ),
        plan,
    )

    assert check["status"] == "pass"


def test_python_contracts_reject_class_protocol_method_drift(tmp_path):
    plan = _plan(
        "class Service:\n    def run(self, value: str) -> bool:\n        ...",
        kind="class",
        qualified_name="sample.Service",
    )

    check = _check(
        tmp_path,
        "class Service:\n    def run(self, value: bytes) -> bool:\n        return True\n",
        plan,
    )

    assert check["status"] == "fail"
    assert "method 'run' arguments do not match" in check["message"]


def test_python_contracts_reject_executable_class_contract_body(tmp_path):
    plan = _plan(
        "class Service:\n    def run(self) -> bool:\n        return True",
        kind="class",
        qualified_name="sample.Service",
    )

    check = _check(tmp_path, "class Service:\n    pass\n", plan)

    assert check["status"] == "fail"
    assert "invalid class signature" in check["message"]


def test_python_contracts_validate_basic_dataclass_constructor(tmp_path):
    plan = _plan("Service(value: str)", kind="class", qualified_name="sample.Service")

    check = _check(
        tmp_path,
        "from dataclasses import dataclass\n\n@dataclass\nclass Service:\n    value: str\n",
        plan,
    )

    assert check["status"] == "pass"


def test_python_contracts_validate_constant_annotation(tmp_path):
    plan = _plan("LIMIT: int", kind="constant", qualified_name="sample.LIMIT")

    assert _check(tmp_path, "LIMIT: int = 10\n", plan)["status"] == "pass"
    mismatch = _check(tmp_path / "mismatch", "LIMIT: str = '10'\n", plan)
    assert mismatch["status"] == "fail"
    assert "annotation does not match" in mismatch["message"]

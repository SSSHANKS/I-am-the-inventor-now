from packages.modules.clean.validation import validate_project
from packages.modules.clean.workspace import CleanWorkspace


def _workspace(tmp_path, files):
    workspace = CleanWorkspace(tmp_path / "output")
    workspace.write_generated_files(
        [{"path": path, "content": content} for path, content in files.items()]
    )
    return workspace


def _plan(*paths):
    return {
        "files": [{"path": path} for path in paths],
        "entry_points": [{"path": paths[0], "description": "Primary module"}],
    }


def _coherence(checks):
    return next(check for check in checks if check["name"] == "python-symbol-coherence")


def test_python_symbol_coherence_accepts_valid_local_imports_and_exports(tmp_path):
    files = {
        "sample/__init__.py": (
            "from .service import Service\n\n"
            "__all__ = ['Service']\n"
        ),
        "sample/service.py": "class Service:\n    pass\n",
        "tests/test_service.py": "from sample import Service\n\nassert Service\n",
    }
    workspace = _workspace(tmp_path, files)

    check = _coherence(validate_project(workspace, _plan(*files)))

    assert check == {
        "name": "python-symbol-coherence",
        "status": "pass",
        "message": "Passed",
        "paths": [],
    }


def test_python_symbol_coherence_rejects_broken_package_reexport(tmp_path):
    files = {
        "project_x/__init__.py": "from .attributes import DynamicAttributeLookup\n",
        "project_x/attributes.py": "class ModuleAttributeLookup:\n    pass\n",
    }
    workspace = _workspace(tmp_path, files)

    check = _coherence(validate_project(workspace, _plan(*files)))

    assert check["status"] == "fail"
    assert "missing symbol 'DynamicAttributeLookup'" in check["message"]
    assert check["paths"] == ["project_x/__init__.py", "project_x/attributes.py"]


def test_python_symbol_coherence_rejects_broken_generated_test_import(tmp_path):
    files = {
        "project_x/__init__.py": "VALUE = 1\n",
        "project_x/streams.py": "class StreamFixupHelper:\n    pass\n",
        "tests/test_streams.py": "from project_x.streams import ComponentN\n",
    }
    workspace = _workspace(tmp_path, files)

    check = _coherence(validate_project(workspace, _plan(*files)))

    assert check["status"] == "fail"
    assert "missing symbol 'ComponentN'" in check["message"]
    assert check["paths"] == ["project_x/streams.py", "tests/test_streams.py"]


def test_python_symbol_coherence_rejects_missing_all_member(tmp_path):
    files = {"sample/__init__.py": "VALUE = 1\n__all__ = ['VALUE', 'MISSING']\n"}
    workspace = _workspace(tmp_path, files)

    check = _coherence(validate_project(workspace, _plan(*files)))

    assert check["status"] == "fail"
    assert "missing __all__ symbol 'MISSING'" in check["message"]
    assert check["paths"] == ["sample/__init__.py"]


def test_python_symbol_coherence_rejects_missing_local_module(tmp_path):
    files = {"sample/__init__.py": "from .missing import VALUE\n"}
    workspace = _workspace(tmp_path, files)

    check = _coherence(validate_project(workspace, _plan(*files)))

    assert check["status"] == "fail"
    assert "missing local module 'sample.missing'" in check["message"]


def test_python_symbol_coherence_rejects_relative_import_beyond_package(tmp_path):
    files = {
        "sample/__init__.py": "VALUE = 1\n",
        "sample/module.py": "from ..outside import VALUE\n",
    }
    workspace = _workspace(tmp_path, files)

    check = _coherence(validate_project(workspace, _plan(*files)))

    assert check["status"] == "fail"
    assert "relative import beyond its package" in check["message"]


def test_python_symbol_coherence_allows_external_and_dynamic_imports(tmp_path):
    files = {
        "sample/__init__.py": (
            "from pathlib import Path\n\n"
            "__all__ = ['dynamic_name']\n\n"
            "def __getattr__(name):\n"
            "    return Path(name)\n"
        ),
        "consumer.py": "from sample import dynamic_name\n",
    }
    workspace = _workspace(tmp_path, files)

    check = _coherence(validate_project(workspace, _plan(*files)))

    assert check["status"] == "pass"


def test_python_symbol_coherence_is_blocked_by_invalid_syntax(tmp_path):
    files = {"broken.py": "def broken(:\n"}
    workspace = _workspace(tmp_path, files)

    check = _coherence(validate_project(workspace, _plan(*files)))

    assert check["status"] == "skipped"
    assert "syntax" in check["message"]

from copy import deepcopy

import pytest

from packages.modules.clean.adapters.python_packaging import (
    python_manifest_readiness_issues,
    validate_python_packaging,
)
from packages.modules.clean.workspace import CleanWorkspace


def _architecture(*, kind="library", layout="src", dependencies=None):
    return {
        "project_profile": {
            "kind": kind,
            "language": "Python",
            "runtime_version": "3.12",
            "build_system": "setuptools",
            "layout": layout,
            "compatibility_mode": "renamed",
        },
        "components": [
            {
                "component_id": "CMP-001",
                "requirement_ids": ["FR-001"],
                "depends_on": [],
            }
        ],
        "contracts": [
            {
                "contract_id": "SYM-001",
                "component_id": "CMP-001",
                "qualified_name": "sample.api.run",
                "kind": "function",
                "declaration": "run() -> None",
                "visibility": "public",
                "requirement_ids": ["FR-001"],
            }
        ],
        "entry_points": [
            {
                "entry_point_id": "EP-001",
                "component_id": "CMP-001",
                "description": "Public operation",
                "contract_ids": ["SYM-001"],
            }
        ],
        "dependency_decisions": dependencies or [],
    }


def _manifest(*, provider="src/sample/api.py", include_pyproject=True):
    files = [
        {
            "path": provider,
            "category": "source",
            "component_id": "CMP-001",
            "provides": ["SYM-001"],
        }
    ]
    if include_pyproject:
        files.append(
            {
                "path": "pyproject.toml",
                "category": "metadata",
                "component_id": "CMP-001",
                "provides": [],
            }
        )
    return {
        "files": files,
        "entry_points": [{"entry_point_id": "EP-001", "path": provider}],
    }


def _pyproject(*, dependencies=(), scripts=None, backend="setuptools.build_meta"):
    lines = [
        "[build-system]",
        'requires = ["setuptools>=68"]',
        f'build-backend = "{backend}"',
        "",
        "[project]",
        'name = "sample-project"',
        'version = "0.1.0"',
        'requires-python = ">=3.12"',
        "dependencies = [" + ", ".join(f'"{item}"' for item in dependencies) + "]",
    ]
    if scripts:
        lines.extend(["", "[project.scripts]"])
        lines.extend(f'{name} = "{target}"' for name, target in scripts.items())
    lines.extend(["", "[tool.setuptools.packages.find]", 'where = ["src"]', ""])
    return "\n".join(lines)


def _workspace(tmp_path, pyproject=None):
    workspace = CleanWorkspace(tmp_path / "output")
    workspace.write_generated_file(
        "src/sample/api.py",
        "def run() -> None:\n    return None\n",
    )
    if pyproject is not None:
        workspace.write_generated_file("pyproject.toml", pyproject)
    return workspace


def _by_name(checks, name):
    return next(item for item in checks if item["name"] == name)


@pytest.mark.parametrize("scope,runtime,build,passed", [
    ("build", [], ["backend-kit>=1"], True),
    ("build", ["backend-kit"], [], False),
    ("build", [], [], False),
    ("runtime", ["backend-kit>=1"], [], True),
    ("runtime", [], ["backend-kit"], False),
    ("both", ["backend-kit"], ["backend-kit"], True),
    ("both", ["backend-kit"], [], False),
    ("both", [], ["backend-kit"], False),
])
def test_dependency_scopes_require_correct_metadata_section(scope, runtime, build, passed):
    from packages.modules.clean.adapters.python_packaging import _dependency_check
    architecture = _architecture(dependencies=[{
        "name": "backend-kit", "purpose": "Declared dependency", "required": True, "scope": scope,
    }])
    check = _dependency_check({"project": {"dependencies": runtime},
                               "build-system": {"requires": build}}, "pyproject.toml", architecture)
    assert (check["status"] == "pass") == passed


def test_legacy_selected_backend_is_build_only(tmp_path):
    architecture = _architecture(dependencies=[{
        "name": "setuptools", "purpose": "Build and packaging specification framework.",
        "required": True,
    }])
    checks = validate_python_packaging(_workspace(tmp_path, _pyproject()), architecture, _manifest())
    assert _by_name(checks, "python-dependencies")["status"] == "pass"


@pytest.mark.parametrize("purpose,scope", [
    ("Build and runtime plugin loading", None),
    ("Build and packaging framework", "runtime"),
    ("Required framework", None),
])
def test_backend_name_alone_does_not_exempt_runtime_dependency(tmp_path, purpose, scope):
    decision = {"name": "setuptools", "purpose": purpose, "required": True}
    if scope is not None:
        decision["scope"] = scope
    checks = validate_python_packaging(_workspace(tmp_path, _pyproject()),
                                      _architecture(dependencies=[decision]), _manifest())
    check = _by_name(checks, "python-dependencies")
    assert check["status"] == "fail"
    assert "missing required dependencies: setuptools" in check["message"]


def test_dependency_scope_schema_is_optional_but_validated():
    from marshmallow import ValidationError
    from packages.modules.supervising.schemas.clean import CleanDependencyDecisionSchema
    decision = {"name": "tool", "purpose": "Building", "required": True,
                "source": "adapter-baseline", "requirement_ids": []}
    schema = CleanDependencyDecisionSchema()
    assert "scope" not in schema.load(decision)
    for scope in ("build", "runtime", "both"):
        assert schema.load({**decision, "scope": scope})["scope"] == scope
    with pytest.raises(ValidationError):
        schema.load({**decision, "scope": "anything"})


def test_python_packaging_accepts_coherent_src_layout_and_metadata(tmp_path):
    architecture = _architecture(
        dependencies=[
            {
                "name": "httpx",
                "purpose": "Required HTTP client",
                "required": True,
            }
        ]
    )
    checks = validate_python_packaging(
        _workspace(tmp_path, _pyproject(dependencies=["httpx>=0.27"])),
        architecture,
        _manifest(),
    )

    assert all(item["status"] != "fail" for item in checks)
    assert _by_name(checks, "python-package-layout")["status"] == "pass"
    assert _by_name(checks, "python-packaging")["status"] == "pass"
    assert _by_name(checks, "python-dependencies")["status"] == "pass"


def test_python_packaging_rejects_provider_module_layout_drift(tmp_path):
    manifest = _manifest(provider="src/sample/wrong.py")

    check = _by_name(
        validate_python_packaging(
            _workspace(tmp_path, _pyproject()),
            _architecture(),
            manifest,
        ),
        "python-package-layout",
    )

    assert check["status"] == "fail"
    assert "expects module 'sample.api', got 'sample.wrong'" in check["message"]
    assert check["diagnostics"][0]["stage"] == "project-readiness"


def test_python_packaging_requires_metadata_for_declared_build_system(tmp_path):
    checks = validate_python_packaging(
        _workspace(tmp_path),
        _architecture(),
        _manifest(include_pyproject=False),
    )

    packaging = _by_name(checks, "python-packaging")
    assert packaging["status"] == "fail"
    assert packaging["diagnostics"][0]["actual"] == "missing"


def test_python_manifest_detects_packaging_defects_before_generation():
    issues = python_manifest_readiness_issues(
        _architecture(),
        _manifest(provider="src/sample/wrong.py", include_pyproject=False),
    )

    assert any("expects module 'sample.api'" in item for item in issues)
    assert any("requires a root pyproject.toml" in item for item in issues)


@pytest.mark.parametrize(
    ("declared", "generated", "message"),
    [
        (
            [{"name": "httpx", "purpose": "HTTP", "required": True}],
            [],
            "missing required dependencies: httpx",
        ),
        ([], ["invented-lib>=1"], "undeclared dependencies: invented-lib"),
    ],
)
def test_python_packaging_enforces_architecture_dependency_decisions(
    tmp_path,
    declared,
    generated,
    message,
):
    checks = validate_python_packaging(
        _workspace(tmp_path, _pyproject(dependencies=generated)),
        _architecture(dependencies=declared),
        _manifest(),
    )

    dependency_check = _by_name(checks, "python-dependencies")
    assert dependency_check["status"] == "fail"
    assert message in dependency_check["message"]


def test_python_packaging_rejects_build_backend_drift(tmp_path):
    check = _by_name(
        validate_python_packaging(
            _workspace(tmp_path, _pyproject(backend="hatchling.build")),
            _architecture(),
            _manifest(),
        ),
        "python-packaging",
    )

    assert check["status"] == "fail"
    assert "does not match declared build system" in check["message"]


def test_python_packaging_requires_top_level_src_modules_in_wheel_metadata(tmp_path):
    architecture = _architecture()
    architecture["contracts"][0]["qualified_name"] = "sample.run"
    manifest = _manifest(provider="src/sample.py")
    workspace = CleanWorkspace(tmp_path / "output")
    workspace.write_generated_file("src/sample.py", "def run() -> None:\n    return None\n")
    workspace.write_generated_file(
        "pyproject.toml",
        _pyproject() + '\n\n[project.urls]\nHomepage = "https://example.invalid"\n',
    )

    check = _by_name(
        validate_python_packaging(workspace, architecture, manifest),
        "python-packaging",
    )

    assert check["status"] == "fail"
    assert "py-modules omits top-level src modules: sample" in check["message"]


def test_python_manifest_rejects_module_and_package_with_same_import_name():
    architecture = _architecture()
    manifest = _manifest()
    manifest["files"].append(
        {
            "path": "src/sample/api/__init__.py",
            "category": "source",
            "component_id": "CMP-001",
            "provides": [],
        }
    )

    issues = python_manifest_readiness_issues(architecture, manifest)

    assert any(
        "module 'sample.api' has multiple source paths" in issue for issue in issues
    )


def test_python_packaging_reports_packages_array_without_crashing(tmp_path):
    malformed = _pyproject().replace(
        '[tool.setuptools.packages.find]\nwhere = ["src"]',
        '[tool.setuptools]\npackages = ["sample"]',
    )

    check = _by_name(
        validate_python_packaging(
            _workspace(tmp_path, malformed),
            _architecture(),
            _manifest(),
        ),
        "python-packaging",
    )

    assert check["status"] == "fail"
    assert "package discovery does not target the src directory" in check["message"]


def test_python_packaging_allows_automatic_src_discovery(tmp_path):
    content = _pyproject().replace('[tool.setuptools.packages.find]\nwhere = ["src"]', '')
    checks = validate_python_packaging(_workspace(tmp_path, content), _architecture(), _manifest())
    assert _by_name(checks, 'python-packaging')['status'] == 'pass'


def test_python_packaging_still_rejects_explicit_wrong_discovery(tmp_path):
    content = _pyproject().replace('where = ["src"]', 'where = ["wrong"]')
    checks = validate_python_packaging(_workspace(tmp_path, content), _architecture(), _manifest())
    assert _by_name(checks, 'python-packaging')['status'] == 'fail'


def test_python_packaging_rejects_unplanned_readme_reference(tmp_path):
    pyproject = _pyproject().replace(
        'requires-python = ">=3.12"',
        'requires-python = ">=3.12"\nreadme = "README.md"',
    )

    check = _by_name(
        validate_python_packaging(
            _workspace(tmp_path, pyproject),
            _architecture(),
            _manifest(),
        ),
        "python-packaging",
    )

    assert check["status"] == "fail"
    assert "references unplanned file 'README.md'" in check["message"]


def test_python_cli_script_must_target_architecture_entry_contract(tmp_path):
    architecture = _architecture(kind="cli")
    invalid = _by_name(
        validate_python_packaging(
            _workspace(
                tmp_path,
                _pyproject(scripts={"sample": "sample.api:invented"}),
            ),
            architecture,
            _manifest(),
        ),
        "python-entry-points",
    )

    assert invalid["status"] == "fail"
    assert invalid["diagnostics"][0]["expected"] == "sample.api:run"

    valid_workspace = _workspace(
        tmp_path / "valid",
        _pyproject(scripts={"sample": "sample.api:run"}),
    )
    valid = _by_name(
        validate_python_packaging(valid_workspace, architecture, _manifest()),
        "python-entry-points",
    )
    assert valid["status"] == "pass"


def test_python_application_without_build_system_can_remain_flat(tmp_path):
    architecture = _architecture(kind="application", layout="flat")
    architecture["project_profile"]["build_system"] = "none"
    architecture["contracts"][0]["qualified_name"] = "app.run"
    manifest = _manifest(provider="app.py", include_pyproject=False)
    workspace = CleanWorkspace(tmp_path / "output")
    workspace.write_generated_file("app.py", "def run() -> None:\n    return None\n")

    checks = validate_python_packaging(workspace, architecture, manifest)

    assert _by_name(checks, "python-package-layout")["status"] == "pass"
    assert _by_name(checks, "python-packaging")["status"] == "skipped"
    assert all(item["status"] != "fail" for item in checks)


def test_python_packaging_does_not_mutate_architecture_or_manifest(tmp_path):
    architecture = _architecture()
    manifest = _manifest()
    original_architecture = deepcopy(architecture)
    original_manifest = deepcopy(manifest)

    validate_python_packaging(
        _workspace(tmp_path, _pyproject()),
        architecture,
        manifest,
    )

    assert architecture == original_architecture
    assert manifest == original_manifest

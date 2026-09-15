import tomllib

from packages.modules.clean.adapters.python import PythonRuntimeAdapter


def _normalise(content, paths=(), backend="setuptools"):
    return PythonRuntimeAdapter().normalise_generated_content(
        "pyproject.toml", content,
        {"project_profile": {"build_system": backend, "layout": "src"}},
        {"files": [{"path": path} for path in paths]},
    )


def test_metadata_corrects_discovery_and_missing_readme_without_changing_project():
    content = '''[project]
name = "example"
version = "1.2.3"
readme = "README.md"
dependencies = ["example-dependency>=1"]
[tool.setuptools.packages.find]
where = ["."]
include = ["example*"]
'''
    result = _normalise(content)
    parsed = tomllib.loads(result)
    assert parsed["project"] == {
        "name": "example", "version": "1.2.3",
        "dependencies": ["example-dependency>=1"],
    }
    assert parsed["tool"]["setuptools"]["packages"]["find"] == {
        "where": ["src"], "include": ["example*"],
    }
    assert _normalise(result) == result


def test_metadata_preserves_planned_or_inline_readme():
    for declaration in ('"README.md"', '{text = "Documentation", content-type = "text/plain"}'):
        content = f"[project]\nreadme = {declaration}\n"
        assert _normalise(content, paths=["README.md"]) == content


def test_metadata_preserves_unrecognized_toml_syntax_and_other_backends():
    content = '[tool.setuptools.packages.find]\nwhere = [\n"."\n]\n'
    assert _normalise(content) == content
    assert _normalise(content, backend="hatchling") == content
    assert _normalise("invalid = [") == "invalid = ["

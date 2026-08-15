"""Indexing: structure extraction and the evidence contract."""

from packages.modules.indexing import (
    SourceCodeIndexer,
    SourceDocIndexer,
    build_source_code_index_context,
    evidence,
)


def test_code_index_finds_the_structure(reader, manifest):
    index = SourceCodeIndexer(reader).index(manifest)
    assert index["errors"] == []
    assert "src/store.py" in index["files_indexed"]
    class_names = {c["name"] for c in index["classes"]}
    assert "WidgetStore" in class_names
    assert "build_store" in {f["qualified_name"] for f in index["functions"]}
    assert "WidgetStore.load" in {f["qualified_name"] for f in index["functions"]}


def test_a_ternary_or_lambda_no_longer_aborts_the_file(reader, manifest):
    """Regression: `body` is a bare expression on IfExp/Lambda, not a list.

    Iterating it raised TypeError, which the per-file handler swallowed - so any module
    containing a ternary or a lambda silently lost its functions, entrypoints, calls and
    analysis targets while still being reported as indexed.
    """
    index = SourceCodeIndexer(reader).index(manifest)
    assert index["errors"] == [], index["errors"]
    # the sample module has both a ternary and a lambda, and still yields everything
    assert index["entrypoints"], "main guard was not found"
    assert index["calls"], "no calls extracted"
    assert index["analysis_targets"], "no analysis targets produced"


def test_json_configuration_is_indexed(reader, manifest):
    index = SourceCodeIndexer(reader).index(manifest)
    configs = [c for c in index["configs"] if c["file"] == "config.json"]
    assert configs and configs[0]["top_level_type"] == "dict"
    assert set(configs[0]["top_level_keys"]) == {"limit", "name"}


def test_classified_code_languages_are_indexed_not_skipped(reader, manifest):
    """Every extension ingest puts on the code list must produce index entries.

    The old gap left .js/.java/.cpp/.ipynb under files_skipped while the manifest
    claimed they were analysable - planning then starved on non-Python repos.
    """
    index = SourceCodeIndexer(reader).index(manifest)
    assert index["files_skipped"] == [], index["files_skipped"]
    assert index["errors"] == [], index["errors"]

    indexed = set(index["files_indexed"])
    assert {
        "src/store.py",
        "src/view.js",
        "src/WidgetService.java",
        "src/box.cpp",
        "analysis.ipynb",
        "config.json",
        "pyproject.toml",
        "Dockerfile",
    } <= indexed

    class_names = {item["name"] for item in index["classes"]}
    assert {"WidgetStore", "WidgetView", "WidgetService", "WidgetBox", "NotebookStore"} <= class_names

    function_names = {item["name"] for item in index["functions"]}
    assert {"createView", "build_notebook_store"} <= function_names

    assert any(item["file"] == "src/view.js" for item in index["entrypoints"])
    assert any(item["file"] == "src/WidgetService.java" for item in index["entrypoints"])
    assert any(item["file"] == "src/box.cpp" for item in index["entrypoints"])

    toml = [c for c in index["configs"] if c["file"] == "pyproject.toml"]
    assert toml and "project" in toml[0]["top_level_keys"]
    docker = [c for c in index["configs"] if c["file"] == "Dockerfile"]
    assert docker and "FROM" in docker[0]["top_level_keys"]


def test_code_extensions_and_handlers_stay_aligned():
    """Classify and index must agree on what 'code' means - no silent skips."""
    from packages.modules.indexing.indexers import resolve_code_handler
    from packages.modules.ingesting import CODE_EXTENSIONS

    for extension in sorted(CODE_EXTENSIONS):
        handler = resolve_code_handler(f"sample.{extension}")
        assert handler is not None, f".{extension} is classified as code but has no indexer"


def test_ignored_files_never_reach_the_indexer(reader, manifest):
    assert "notes.bin" in manifest.ignored
    index = SourceCodeIndexer(reader).index(manifest)
    assert all("notes.bin" not in str(entry) for entry in index["files_skipped"])


def test_doc_index_finds_sections_and_commands(reader, manifest):
    index = SourceDocIndexer(reader).index(manifest)
    assert index["errors"] == []
    titles = {h["title"] for h in index["headings"]}
    assert {"Sample Project", "Setup", "Features"} <= titles
    assert any("pip install" in c["command"] for c in index["commands"])
    assert index["code_blocks"] and index["links"]


def test_every_indexed_item_carries_locatable_evidence(reader, manifest):
    index = SourceCodeIndexer(reader).index(manifest)
    for item in index["classes"] + index["functions"]:
        ev = item["evidence"]
        assert ev["file"] and ev["line_start"] >= 1
        assert ev["line_end"] >= ev["line_start"]


def test_evidence_clamps_to_the_file(reader):
    lines = ["one", "two"]
    assert evidence("f.py", lines, 99, 120)["line_start"] == 2
    assert evidence("f.py", [], 5)["excerpt"] == ""


def test_prompt_context_trims_the_index(reader, manifest):
    index = SourceCodeIndexer(reader).index(manifest)
    context = build_source_code_index_context(index, limit=1)
    assert len(context["classes"]) <= 1
    assert "calls" not in context  # not useful in a prompt, and large

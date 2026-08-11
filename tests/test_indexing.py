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
    assert {c["name"] for c in index["classes"]} == {"WidgetStore"}
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


def test_code_files_without_an_indexer_are_recorded_not_dropped(reader, manifest):
    """`CODE_EXTENSIONS` admits nine languages; only .py and .json have an indexer.

    The rest must show up under files_skipped so a later stage can see the gap rather
    than mistake a partial index for a complete one.
    """
    index = SourceCodeIndexer(reader).index(manifest)
    skipped = {s["file"]: s["reason"] for s in index["files_skipped"]}
    assert "widget.js" in skipped
    assert "unsupported_extension" in skipped["widget.js"]


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

"""Ingesting: file classification and manifest safety."""

import dataclasses

import pytest

from packages.modules.ingesting import (
    IngestingError,
    SourceManifest,
    classify_files,
    provide_source_ingestor,
)


def test_files_are_split_by_role():
    result = classify_files(["README.md", "src/app.py", "logo.png", "Dockerfile"])
    assert result["documentation"] == ("README.md",)
    assert result["code"] == ("src/app.py", "Dockerfile")
    assert result["ignored"] == ("logo.png",)


def test_config_files_count_as_code_even_without_a_code_extension():
    assert classify_files(["requirements.txt"])["code"] == ("requirements.txt",)
    # ...while a plain .txt is documentation
    assert classify_files(["notes.txt"])["documentation"] == ("notes.txt",)


def test_manifests_do_not_share_default_collections():
    """The legacy NamedTuple defaulted these to one shared mutable list."""
    first = SourceManifest(source_type="url_git_repo", repo_url="https://a.invalid/x.git")
    second = SourceManifest(source_type="url_git_repo", repo_url="https://b.invalid/y.git")
    assert first.documentation == () and second.documentation == ()
    with pytest.raises(AttributeError):
        first.documentation.append("leaked.md")  # type: ignore[attr-defined]


def test_manifest_is_immutable(manifest):
    with pytest.raises(dataclasses.FrozenInstanceError):
        manifest.repo_url = "https://elsewhere.invalid/z.git"


def test_manifest_round_trips_to_plain_data(manifest):
    payload = manifest.to_dict()
    assert isinstance(payload["documentation"], list)
    assert payload["commit_hash"] == "0123456789abcdef"


def test_ordinary_repository_urls_are_accepted():
    """The legacy check required a .git suffix, rejecting normal GitHub URLs."""

    class _Config:
        workspace_dir = "temp"

    assert provide_source_ingestor("https://example.invalid/owner/project", _Config())
    assert provide_source_ingestor("https://example.invalid/owner/project.git", _Config())


def test_non_repository_sources_are_refused():
    with pytest.raises(IngestingError):
        provide_source_ingestor("/some/local/folder", object())


def test_clone_targets_are_unique_per_run(tmp_path):
    """Re-running used to collide on temp/<name>, which git refuses."""
    from packages.modules.ingesting.git_source import GitRepoIngestor

    class _Config:
        workspace_dir = tmp_path

    ingestor = GitRepoIngestor(config=_Config())
    first = ingestor._clone_target("https://example.invalid/owner/project.git")
    second = ingestor._clone_target("https://example.invalid/owner/project.git")
    assert first != second
    assert first.name.startswith("project-") and second.name.startswith("project-")

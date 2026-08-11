"""Storing: round-tripping, and refusing to persist invalid artifacts."""

import pytest
from marshmallow import Schema, fields

from packages.modules.storing import ArtifactValidationError, Storage, StorageError
from packages.modules.supervising.schemas import ManifestSchema


class _Tiny(Schema):
    name = fields.String(required=True)


def test_json_round_trip(storage):
    """Regression: read_file called itself for .json and recursed until it blew up."""
    storage.save_json("index.json", {"commit_hash": "abc", "items": [1, 2]})
    assert storage.read_json("index.json")["commit_hash"] == "abc"


def test_text_round_trip(storage):
    storage.save_text("spec.md", "# Specification\n")
    assert storage.read_text("spec.md").startswith("# Specification")


def test_valid_artifact_is_written(storage):
    storage.save_artifact("ok.json", {"name": "value"}, _Tiny())
    assert storage.exists("ok.json")


def test_invalid_artifact_is_refused_and_not_written(storage):
    with pytest.raises(ArtifactValidationError):
        storage.save_artifact("bad.json", {"nope": 1}, _Tiny())
    assert not storage.exists("bad.json")


def test_manifest_passes_its_schema_before_storage(storage, manifest):
    storage.save_artifact("manifest.json", manifest, ManifestSchema())
    assert storage.read_json("manifest.json")["branch"] == "main"


def test_manifest_with_a_bad_source_type_is_refused(storage, manifest):
    payload = manifest.to_dict() | {"source_type": "carrier_pigeon"}
    with pytest.raises(ArtifactValidationError):
        storage.save_artifact("manifest.json", payload, ManifestSchema())


def test_private_artifacts_land_in_their_own_area(storage):
    path = storage.save_private("alias_map.json", {"project_name": "PROJECT-X"})
    assert "_private" in path.parts


def test_reading_a_missing_artifact_reports_clearly(storage):
    with pytest.raises(StorageError):
        storage.read_text("nothing.md")


def test_artifact_paths_cannot_escape_the_run_directory(storage):
    with pytest.raises(StorageError):
        storage.save_text("../escaped.md", "nope")


def test_run_names_survive_windows_hostile_characters(tmp_path):
    store = Storage(artifacts_dir=tmp_path, run_name="Agent [X -> Y]")
    store.save_text("a.md", "ok")
    assert store.exists("a.md")

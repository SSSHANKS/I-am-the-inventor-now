import pytest

from packages.modules.clean.workspace import CleanWorkspace, WorkspaceError, validate_relative_path


@pytest.mark.parametrize(
    "path",
    ["", ".", "../escape.py", "a/../escape.py", "/absolute.py", "C:/drive.py", "\\\\host\\share.py", "a\\b.py", "file:stream"],
)
def test_unsafe_generated_paths_are_rejected(path):
    with pytest.raises(WorkspaceError):
        validate_relative_path(path)


def test_batch_is_validated_before_any_file_is_written(tmp_path):
    workspace = CleanWorkspace(tmp_path / "output")

    with pytest.raises(WorkspaceError):
        workspace.write_generated_files(
            [
                {"path": "valid.py", "content": "value = 1\n"},
                {"path": "../escape.py", "content": "bad = True\n"},
            ]
        )

    assert workspace.list_generated_files() == []
    assert not (tmp_path / "escape.py").exists()


def test_workspace_refuses_non_empty_output_and_restricts_repairs(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="empty"):
        CleanWorkspace(output)
    assert marker.read_text(encoding="utf-8") == "keep"

    workspace = CleanWorkspace(tmp_path / "fresh")
    with pytest.raises(WorkspaceError, match="allowed task"):
        workspace.write_generated_files(
            [{"path": "other.py", "content": ""}], allowed_paths={"planned.py"}
        )

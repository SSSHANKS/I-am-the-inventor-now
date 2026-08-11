"""Reading: the snapshot boundary and line addressing."""

import pytest

from packages.modules.skills.reading import ReadingError


def test_reads_a_file_from_the_snapshot(reader):
    assert "WidgetStore" in reader.read_file("src/store.py")


def test_line_ranges_are_one_based_and_inclusive(reader):
    assert reader.read_lines("README.md", 1, 1) == "# Sample Project"
    assert reader.read_lines("README.md", 1, 3).count("\n") == 2


@pytest.mark.parametrize("start,end", [(0, 3), (3, 1), (-1, 2)])
def test_invalid_line_ranges_are_refused(reader, start, end):
    with pytest.raises(ReadingError):
        reader.read_lines("README.md", start, end)


def test_paths_cannot_escape_the_snapshot(reader):
    with pytest.raises(ReadingError):
        reader.read_file("../outside.txt")
    assert reader.file_exists("../outside.txt") is False


def test_missing_files_raise_rather_than_return_empty(reader):
    with pytest.raises(ReadingError):
        reader.read_file("does/not/exist.py")


def test_search_returns_numbered_matches(reader):
    matches = reader.search("src/store.py", r"class \w+")
    assert len(matches) == 1
    assert matches[0]["line_number"] > 0
    assert "class WidgetStore" in matches[0]["line"]


def test_search_with_no_match_returns_empty(reader):
    assert reader.search("README.md", r"zzz-not-here") == []


def test_invalid_regex_is_reported(reader):
    with pytest.raises(ReadingError):
        reader.search("README.md", "(unclosed")

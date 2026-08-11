"""Shared fixtures.

Every test here is offline and deterministic: no model is contacted, no repository is
cloned. Agent behaviour is exercised through `StubTextClient`.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from packages.modules.ingesting import SourceManifest, classify_files
from packages.modules.skills.reading import Reader
from packages.modules.storing import Storage

SAMPLE_MODULE = '''\
"""A tiny module used as an analysis target."""

import json
from pathlib import Path

WIDGET_LIMIT = 5


class WidgetStore:
    """Holds widgets."""

    def __init__(self, root):
        self.root = Path(root)

    def load(self, name):
        # a ternary: the construct that used to abort indexing for the whole file
        target = self.root / name if name else self.root / "default.json"
        return json.loads(target.read_text())

    def names(self):
        return sorted(map(lambda item: item.upper(), self._raw()))

    def _raw(self):
        return ["a", "b"]


def build_store(root):
    return WidgetStore(root)


if __name__ == "__main__":
    build_store(".")
'''

SAMPLE_DOC = """\
# Sample Project

A short description of what the project does.

## Setup

Install it first:

```bash
pip install sample
```

Then run `python -m sample`.

## Features

- Stores widgets on disk
- Reads them back by name

See [the docs](https://example.invalid/docs) for more.
"""


@pytest.fixture
def snapshot(tmp_path):
    """A small on-disk repository snapshot."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "store.py").write_text(SAMPLE_MODULE, encoding="utf-8")
    (tmp_path / "README.md").write_text(SAMPLE_DOC, encoding="utf-8")
    (tmp_path / "config.json").write_text('{"limit": 5, "name": "sample"}', encoding="utf-8")
    (tmp_path / "notes.bin").write_bytes(b"\x00\x01")
    # classified as code, but no indexer handles it yet - only .py and .json do
    (tmp_path / "widget.js").write_text("export const widget = 1;\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def manifest(snapshot):
    files = ["src/store.py", "README.md", "config.json", "notes.bin", "widget.js"]
    return SourceManifest(
        source_type="url_git_repo",
        repo_url="https://example.invalid/owner/original-project.git",
        branch="main",
        commit_hash="0123456789abcdef",
        repo_local_path=str(snapshot),
        **classify_files(files),
    )


@pytest.fixture
def reader(snapshot):
    return Reader(snapshot)


@pytest.fixture
def storage(tmp_path):
    return Storage(artifacts_dir=tmp_path / "artifacts", run_name="test-run")

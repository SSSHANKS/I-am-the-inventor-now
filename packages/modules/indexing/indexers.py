"""Build the code and documentation indexes for one snapshot.

Both indexers share the same contract: walk the files the manifest lists, hand each
to a per-format indexer, and never let one bad file abort the run - an unparseable
file becomes an entry under `errors` so later stages can see the gap instead of
silently inheriting a short index.
"""

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from packages.modules.indexing.config_file import index_config_file
from packages.modules.indexing.document_file import index_document_file
from packages.modules.indexing.json_file import index_json_file
from packages.modules.indexing.models import SourceCodeIndex, SourceDocIndex
from packages.modules.indexing.notebook_file import index_notebook_file
from packages.modules.indexing.python_file import index_python_file
from packages.modules.indexing.source_file import PROFILE_BY_SUFFIX, index_source_file
from packages.modules.ingesting import SourceManifest
from packages.modules.skills.reading import Reader

log = logging.getLogger(__name__)

FileHandler = Callable[[str, dict[str, Any], Reader], None]

#: Build / dependency filenames that carry no useful suffix (or share one with docs).
_NAMED_CONFIG_HANDLERS: dict[str, FileHandler] = {
    "dockerfile": index_config_file,
    "makefile": index_config_file,
    "requirements.txt": index_config_file,
    "pyproject.toml": index_config_file,
}

CODE_HANDLERS: dict[str, FileHandler] = {
    ".py": index_python_file,
    ".json": index_json_file,
    ".ipynb": index_notebook_file,
    ".toml": index_config_file,
    **{suffix: index_source_file for suffix in PROFILE_BY_SUFFIX},
}

DOC_SUFFIXES = frozenset({".md", ".markdown", ".rst", ".txt", ".adoc", ""})


def resolve_code_handler(relative_path: str) -> FileHandler | None:
    """Pick the indexer for one code-list path, or None if nothing can handle it."""
    path = Path(relative_path)
    named = _NAMED_CONFIG_HANDLERS.get(path.name.lower())
    if named is not None:
        return named
    return CODE_HANDLERS.get(path.suffix.lower())


class _Indexer:
    """Shared walk-and-collect loop."""

    def __init__(self, source_reader: Reader):
        self.source_reader = source_reader

    def _walk(self, paths, result: dict, handler_for) -> dict:
        for relative_path in paths:
            handler = handler_for(relative_path)
            if handler is None:
                suffix = Path(relative_path).suffix.lower()
                result["files_skipped"].append(
                    {
                        "file": relative_path,
                        "reason": f"unsupported_extension:{suffix or '<none>'}",
                    }
                )
                continue
            try:
                handler(relative_path, result, self.source_reader)
            except Exception as exc:
                log.exception("Could not index %s", relative_path)
                result["errors"].append(
                    {
                        "file": relative_path,
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    }
                )
        return result


class SourceCodeIndexer(_Indexer):
    """Indexes source and configuration files."""

    def index(self, manifest: SourceManifest) -> dict:
        result = SourceCodeIndex(
            repo_url=manifest.repo_url,
            branch=manifest.branch,
            commit_hash=manifest.commit_hash,
        ).to_dict()
        return self._walk(manifest.code, result, resolve_code_handler)


class SourceDocIndexer(_Indexer):
    """Indexes documentation files."""

    def index(self, manifest: SourceManifest) -> dict:
        result = SourceDocIndex(
            repo_url=manifest.repo_url,
            branch=manifest.branch,
            commit_hash=manifest.commit_hash,
        ).to_dict()
        return self._walk(
            manifest.documentation,
            result,
            lambda relative_path: (
                index_document_file
                if Path(relative_path).suffix.lower() in DOC_SUFFIXES
                else None
            ),
        )

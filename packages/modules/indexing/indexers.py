"""Build the code and documentation indexes for one snapshot.

Both indexers share the same contract: walk the files the manifest lists, hand each
to a per-format indexer, and never let one bad file abort the run - an unparseable
file becomes an entry under `errors` so later stages can see the gap instead of
silently inheriting a short index.
"""

import logging
from pathlib import Path

from packages.modules.indexing.document_file import index_document_file
from packages.modules.indexing.json_file import index_json_file
from packages.modules.indexing.models import SourceCodeIndex, SourceDocIndex
from packages.modules.indexing.python_file import index_python_file
from packages.modules.ingesting import SourceManifest
from packages.modules.skills.reading import Reader

log = logging.getLogger(__name__)

CODE_HANDLERS = {".py": index_python_file, ".json": index_json_file}
DOC_SUFFIXES = frozenset({".md", ".markdown", ".rst", ".txt", ".adoc", ""})


class _Indexer:
    """Shared walk-and-collect loop."""

    def __init__(self, source_reader: Reader):
        self.source_reader = source_reader

    def _walk(self, paths, result: dict, handler_for) -> dict:
        for relative_path in paths:
            suffix = Path(relative_path).suffix.lower()
            handler = handler_for(suffix)
            if handler is None:
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
        return self._walk(manifest.code, result, CODE_HANDLERS.get)


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
            lambda suffix: index_document_file if suffix in DOC_SUFFIXES else None,
        )

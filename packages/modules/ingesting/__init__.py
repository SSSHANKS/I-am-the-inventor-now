import logging

from packages.modules.ingesting.base import BaseIngestor
from packages.modules.ingesting.git_source import GitRepoIngestor
from packages.modules.ingesting.manifest import (
    CODE_EXTENSIONS,
    CONFIG_FILES,
    DOCUMENTATION_EXTENSIONS,
    IngestingError,
    SourceManifest,
    SourceType,
    classify_files,
    remove_tree,
)

log = logging.getLogger(__name__)

__all__ = [
    "CODE_EXTENSIONS",
    "CONFIG_FILES",
    "DOCUMENTATION_EXTENSIONS",
    "BaseIngestor",
    "GitRepoIngestor",
    "IngestingError",
    "SourceManifest",
    "SourceType",
    "classify_files",
    "provide_source_ingestor",
    "remove_tree",
]


def provide_source_ingestor(source: str, config) -> BaseIngestor:
    """Pick an ingestor for `source`."""
    if _looks_like_git_url(source):
        return GitRepoIngestor(config=config)
    raise IngestingError(f"Unsupported source: {source!r}. Expected an http(s) git repository URL.")


def _looks_like_git_url(source: object) -> bool:
    return isinstance(source, str) and source.startswith(("http://", "https://"))

from abc import ABC, abstractmethod

from packages.modules.ingesting.manifest import SourceManifest


class BaseIngestor(ABC):
    """Turns some source location into a `SourceManifest`."""

    def __init__(self, config):
        self.config = config

    @abstractmethod
    def ingest(self, source: str, branch: str | None = None) -> SourceManifest:
        """Fetch `source` and describe the resulting snapshot."""

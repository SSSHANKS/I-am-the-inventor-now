"""Controlled read access to a cloned analysis target.

Reading is a *skill* (CLAUDE.md section 3): a shared capability agents and both
indexers use, not a standalone module. Every read is confined to the snapshot root,
so a crafted relative path cannot walk out of the clone.
"""

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)


class ReadingError(Exception):
    """Raised when a requested read cannot be served."""


class Reader:
    """Reads files from one repository snapshot, and nothing outside it."""

    def __init__(self, repo_path: str | Path):
        self.repo_path = Path(repo_path)
        self._root = self.repo_path.resolve()

    def _safe_path(self, relative_path: str) -> Path:
        """Resolve `relative_path` inside the snapshot, or refuse."""
        candidate = (self._root / relative_path).resolve()
        if not candidate.is_relative_to(self._root):
            raise ReadingError(f"Path escapes the repository snapshot: {relative_path}")
        return candidate

    def file_exists(self, relative_path: str) -> bool:
        try:
            return self._safe_path(relative_path).is_file()
        except ReadingError:
            return False

    def read_file(self, relative_path: str) -> str:
        file_path = self._safe_path(relative_path)
        if not file_path.is_file():
            raise ReadingError(f"File does not exist: {relative_path}")
        return file_path.read_text(encoding="utf-8", errors="replace")

    def read_lines(self, relative_path: str, start_line: int, end_line: int) -> str:
        """Return a 1-based inclusive line range."""
        if start_line < 1:
            raise ReadingError(f"start_line must be >= 1, received {start_line}")
        if end_line < start_line:
            raise ReadingError(
                f"end_line must be >= start_line, received {end_line} < {start_line}"
            )
        lines = self.read_file(relative_path).splitlines()
        return "\n".join(lines[start_line - 1 : end_line])

    def search(self, relative_path: str, pattern: str) -> list[dict[str, object]]:
        """Return every matching line with its 1-based line number.

        The legacy version logged "Pattern not found" before it had searched, which
        made every successful search look like a failure in the logs.
        """
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            raise ReadingError(f"Invalid search pattern {pattern!r}: {exc}") from exc

        matches = [
            {"line_number": number, "line": line}
            for number, line in enumerate(self.read_file(relative_path).splitlines(), start=1)
            if regex.search(line)
        ]

        if not matches:
            log.info("No match for %r in %s", pattern, relative_path)
        return matches

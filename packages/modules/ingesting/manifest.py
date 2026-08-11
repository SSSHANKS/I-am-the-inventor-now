"""The manifest: what a snapshot contains and where it came from.

This is a DIRTY-SIDE artifact. It names the real repository and its real files, so it
must never cross to the clean team as-is - see `packages.modules.boundary`, which
derives the neutral version that does cross (CLAUDE.md section 2).
"""

import os
import stat
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path

DOCUMENTATION_EXTENSIONS = frozenset({"md", "rst", "txt"})
CODE_EXTENSIONS = frozenset({"py", "ipynb", "cpp", "c", "h", "hpp", "js", "ts", "java"})
CONFIG_FILES = frozenset(
    {
        "Dockerfile",
        "Makefile",
        "requirements.txt",
        "pyproject.toml",
        "package.json",
        "settings.json",
        "config.json",
    }
)


class IngestingError(Exception):
    """Raised when a source cannot be ingested."""


class SourceType(StrEnum):
    URL_GIT_REPO = "url_git_repo"


@dataclass(frozen=True)
class SourceManifest:
    """One immutable repository snapshot.

    Replaces the legacy NamedTuple, whose `documentation`/`code`/`ignored` fields
    defaulted to a *shared mutable list* - every manifest built without those
    arguments aliased the same three lists.
    """

    source_type: str
    repo_url: str
    branch: str | None = None
    commit_hash: str | None = None
    repo_local_path: str | None = None
    documentation: tuple[str, ...] = field(default_factory=tuple)
    code: tuple[str, ...] = field(default_factory=tuple)
    ignored: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, object]:
        """Plain dict for schema validation and storage."""
        payload = asdict(self)
        for key in ("documentation", "code", "ignored"):
            payload[key] = list(payload[key])
        return payload

    @property
    def analysable_files(self) -> tuple[str, ...]:
        return tuple(self.documentation) + tuple(self.code)

    def __str__(self) -> str:
        return (
            f"SourceManifest({self.source_type})\n"
            f"  branch: {self.branch}  commit: {self.commit_hash}\n"
            f"  local:  {self.repo_local_path}\n"
            f"  documentation: {len(self.documentation)} file(s)\n"
            f"  code:          {len(self.code)} file(s)\n"
            f"  ignored:       {len(self.ignored)} file(s)"
        )


def classify_files(files: list[str]) -> dict[str, tuple[str, ...]]:
    """Split repository paths into documentation, code, and ignored."""
    documentation: list[str] = []
    code: list[str] = []
    ignored: list[str] = []

    for file in files:
        path = Path(file)
        extension = path.suffix.lower().lstrip(".")

        if path.name in CONFIG_FILES:
            code.append(file)
        elif extension in DOCUMENTATION_EXTENSIONS:
            documentation.append(file)
        elif extension in CODE_EXTENSIONS:
            code.append(file)
        else:
            ignored.append(file)

    return {
        "documentation": tuple(documentation),
        "code": tuple(code),
        "ignored": tuple(ignored),
    }


def remove_tree(path: Path) -> None:
    """Delete a directory tree, including read-only files.

    Git marks objects under `.git` read-only, which makes a plain rmtree fail on
    Windows - the reason the legacy notebook carried its own onerror handler.
    """
    import shutil

    if not path.exists():
        return

    def _on_error(func, target, _exc_info):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    shutil.rmtree(path, onexc=_on_error)

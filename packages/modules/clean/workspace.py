from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any


class WorkspaceError(Exception):
    """A generated path or write would violate the Clean workspace boundary."""


MAX_FILES = 100
MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024


_WINDOWS_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}


def validate_relative_path(value: str) -> str:
    """Return a canonical POSIX path or reject a cross-platform unsafe path."""
    if not isinstance(value, str) or not value or value == "." or "\x00" in value:
        raise WorkspaceError("Generated path must be a non-empty text value")
    if "\\" in value:
        raise WorkspaceError(f"Backslashes are not allowed in generated path {value!r}")
    if value.startswith("/") or ":" in value:
        raise WorkspaceError(f"Absolute or drive-qualified path is not allowed: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise WorkspaceError(f"Unsafe generated path: {value!r}")
    canonical = path.as_posix()
    if canonical != value:
        raise WorkspaceError(f"Generated path is not canonical: {value!r}")
    for part in path.parts:
        if part.endswith((" ", ".")):
            raise WorkspaceError(f"Unsafe Windows path component: {part!r}")
        if part.split(".", 1)[0].casefold() in _WINDOWS_RESERVED:
            raise WorkspaceError(f"Reserved Windows path component: {part!r}")
    return canonical


class CleanWorkspace:
    """The only writer for reconstructed project files and Clean metadata."""

    def __init__(
        self,
        output_root: str | Path,
        *,
        max_files: int = MAX_FILES,
        max_file_bytes: int = MAX_FILE_BYTES,
        max_total_bytes: int = MAX_TOTAL_BYTES,
    ) -> None:
        root = Path(output_root)
        self.require_available(root)
        root.mkdir(parents=True, exist_ok=True)
        self.root = root.resolve()
        self.project_root = self.root / "project"
        self.metadata_root = self.root / "_clean"
        self.project_root.mkdir()
        self.metadata_root.mkdir()
        self.max_files = max_files
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes

    @staticmethod
    def require_available(output_root: str | Path) -> None:
        """Preflight an output path without changing it or consuming a model call."""
        root = Path(output_root)
        if root.exists():
            if root.is_symlink() or not root.is_dir():
                raise WorkspaceError("Clean output must be a real directory")
            if any(root.iterdir()):
                raise WorkspaceError("Clean output directory must be empty")

    def write_generated_file(self, relative_path: str, content: str) -> None:
        self.write_generated_files([{"path": relative_path, "content": content}])

    def write_generated_files(
        self,
        files: Iterable[Mapping[str, Any]],
        *,
        allowed_paths: set[str] | None = None,
    ) -> None:
        batch: list[tuple[str, bytes]] = []
        seen: set[str] = set()
        for item in files:
            path = validate_relative_path(str(item.get("path", "")))
            content = item.get("content")
            if not isinstance(content, str):
                raise WorkspaceError(f"Generated content for {path!r} must be text")
            if path in seen:
                raise WorkspaceError(f"Duplicate generated path in batch: {path!r}")
            if allowed_paths is not None and path not in allowed_paths:
                raise WorkspaceError(f"Generated path is outside the allowed task: {path!r}")
            encoded = content.encode("utf-8")
            if len(encoded) > self.max_file_bytes:
                raise WorkspaceError(f"Generated file exceeds size limit: {path!r}")
            seen.add(path)
            batch.append((path, encoded))

        current = set(self.list_generated_files())
        resulting = current | seen
        if len(resulting) > self.max_files:
            raise WorkspaceError("Generated project exceeds file-count limit")
        replacement_sizes = {path: len(data) for path, data in batch}
        total = sum(
            replacement_sizes[path]
            if path in replacement_sizes
            else len(self._target(path).read_bytes())
            for path in resulting
        )
        if total > self.max_total_bytes:
            raise WorkspaceError("Generated project exceeds total-size limit")

        targets = [(self._target(path), data) for path, data in batch]
        for target, _ in targets:
            self._require_safe_destination(target)
        temporary_files: list[Path] = []
        try:
            for index, (target, data) in enumerate(targets):
                target.parent.mkdir(parents=True, exist_ok=True)
                self._require_safe_destination(target)
                temporary = target.with_name(f".{target.name}.clean-{index}.tmp")
                if temporary.exists() or temporary.is_symlink():
                    raise WorkspaceError(f"Unsafe temporary path for {target.name!r}")
                temporary.write_bytes(data)
                temporary_files.append(temporary)
            for temporary, (target, _) in zip(temporary_files, targets, strict=True):
                os.replace(temporary, target)
        finally:
            for temporary in temporary_files:
                if temporary.exists() and not temporary.is_symlink():
                    temporary.unlink()

    def read_generated_file(self, relative_path: str) -> str:
        target = self._target(validate_relative_path(relative_path))
        self._require_existing_regular_file(target)
        try:
            return target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspaceError(f"Generated file is not UTF-8: {relative_path!r}") from exc

    def list_generated_files(self) -> list[str]:
        files: list[str] = []
        for item in self.project_root.rglob("*"):
            if item.is_symlink():
                raise WorkspaceError(f"Symlink found in generated project: {item.name!r}")
            if item.is_file():
                files.append(item.relative_to(self.project_root).as_posix())
        return sorted(files)

    def content_hash(self, relative_path: str) -> str:
        target = self._target(validate_relative_path(relative_path))
        self._require_existing_regular_file(target)
        return hashlib.sha256(target.read_bytes()).hexdigest()

    def write_metadata_json(self, relative_path: str, value: Any) -> Path:
        canonical = validate_relative_path(relative_path)
        if not canonical.endswith(".json"):
            raise WorkspaceError("Clean metadata must be JSON")
        target = self.metadata_root.joinpath(*PurePosixPath(canonical).parts)
        resolved_parent = target.parent.resolve()
        if not resolved_parent.is_relative_to(self.metadata_root):
            raise WorkspaceError("Clean metadata path escaped its root")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            raise WorkspaceError("Clean metadata target cannot be a symlink")
        encoded = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        temporary = target.with_name(f".{target.name}.tmp")
        try:
            temporary.write_bytes(encoded)
            os.replace(temporary, target)
        finally:
            if temporary.exists() and not temporary.is_symlink():
                temporary.unlink()
        return target

    def _target(self, canonical_path: str) -> Path:
        target = self.project_root.joinpath(*PurePosixPath(canonical_path).parts)
        if not target.resolve(strict=False).is_relative_to(self.project_root):
            raise WorkspaceError(f"Generated path escaped project root: {canonical_path!r}")
        return target

    def _require_safe_destination(self, target: Path) -> None:
        if target.exists() and (target.is_symlink() or not target.is_file()):
            raise WorkspaceError(f"Generated target is not a regular file: {target.name!r}")
        parent = target.parent
        while parent != self.project_root:
            if parent.exists() and (parent.is_symlink() or not parent.is_dir()):
                raise WorkspaceError(f"Unsafe generated path parent: {parent.name!r}")
            parent = parent.parent
        if self.project_root.is_symlink():
            raise WorkspaceError("Generated project root cannot be a symlink")

    @staticmethod
    def _require_existing_regular_file(target: Path) -> None:
        if target.is_symlink() or not target.is_file():
            raise WorkspaceError(f"Generated file does not exist: {target.name!r}")

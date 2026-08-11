"""Artifact storage.

Every artifact is written under `artifacts/<run>/`. Structured artifacts must pass a
marshmallow schema *before* they are written (CLAUDE.md section 6) - an invalid
artifact never reaches disk, so a later stage cannot pick one up and build on it.
"""

import json
import logging
from pathlib import Path
from typing import Any

from marshmallow import Schema
from marshmallow import ValidationError as MarshmallowValidationError

log = logging.getLogger(__name__)

PRIVATE_DIR_NAME = "_private"


class StorageError(Exception):
    """Raised when an artifact cannot be stored or read back."""


class ArtifactValidationError(StorageError):
    """Raised when an artifact fails its schema and is therefore not written."""

    def __init__(self, file_name: str, errors: dict[str, Any]):
        self.file_name = file_name
        self.errors = errors
        super().__init__(f"Refusing to store {file_name!r}: it does not match its schema. {errors}")


class Storage:
    """Reads and writes the artifacts of a single pipeline run."""

    def __init__(self, artifacts_dir: str | Path = "artifacts", run_name: str = "run"):
        self.storage_path = Path(artifacts_dir) / _safe_name(run_name)
        self.storage_path.mkdir(parents=True, exist_ok=True)

    # --- writing -------------------------------------------------------------

    def save_text(self, file_name: str, content: str) -> Path:
        """Write an unstructured artifact, such as the final Markdown specification."""
        path = self._path(file_name)
        path.write_text(content, encoding="utf-8")
        log.info("Stored artifact: %s", path)
        return path

    def save_artifact(self, file_name: str, data: Any, schema: Schema) -> Path:
        """Validate against `schema`, then write. Invalid data is never written."""
        payload = _as_payload(data)
        try:
            schema.load(payload)
        except MarshmallowValidationError as exc:
            log.error("Artifact %s failed validation: %s", file_name, exc.messages)
            raise ArtifactValidationError(file_name, exc.messages) from exc
        return self._write_json(file_name, payload)

    def save_json(self, file_name: str, data: Any) -> Path:
        """Write structured data that has no schema of its own.

        Prefer `save_artifact`. This exists for intermediates the pipeline produces
        deterministically (indexes), where the producing code is the contract.
        """
        return self._write_json(file_name, _as_payload(data))

    def save_private(self, file_name: str, data: Any) -> Path:
        """Write a DIRTY-SIDE-ONLY artifact.

        Anything under `_private/` maps neutral identifiers back to the original and
        must never cross to the clean team (CLAUDE.md section 2).
        """
        return self._write_json(f"{PRIVATE_DIR_NAME}/{file_name}", _as_payload(data))

    # --- reading -------------------------------------------------------------

    def read_text(self, file_name: str) -> str:
        path = self._path(file_name)
        if not path.is_file():
            raise StorageError(f"Artifact not found: {path}")
        return path.read_text(encoding="utf-8")

    def read_json(self, file_name: str) -> Any:
        """Read a JSON artifact.

        The legacy `read_file` called itself for any `.json` path instead of reading
        the file, so every JSON read recursed until the stack ran out.
        """
        text = self.read_text(file_name)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise StorageError(f"Artifact {file_name} is not valid JSON: {exc}") from exc

    def exists(self, file_name: str) -> bool:
        return self._path(file_name).is_file()

    # --- internals -----------------------------------------------------------

    def _path(self, file_name: str) -> Path:
        path = (self.storage_path / file_name).resolve()
        if not path.is_relative_to(self.storage_path.resolve()):
            raise StorageError(f"Artifact path escapes the run directory: {file_name}")
        return path

    def _write_json(self, file_name: str, payload: Any) -> Path:
        path = self._path(file_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log.info("Stored artifact: %s", path)
        return path


def _as_payload(data: Any) -> Any:
    """Accept a JSON string, a dataclass-ish object, or a plain structure."""
    if isinstance(data, str):
        try:
            return json.loads(data)
        except json.JSONDecodeError as exc:
            raise StorageError(f"Expected JSON text, got something else: {exc}") from exc
    if hasattr(data, "to_dict"):
        return data.to_dict()
    return data


def _safe_name(value: str) -> str:
    """Keep run names usable as a single path component on Windows."""
    cleaned = "".join("_" if ch in '<>:"/\\|?*[]' else ch for ch in value)
    return cleaned.strip(" .") or "run"

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from packages.modules.clean.workspace import CleanWorkspace


class RuntimeAdapterError(ValueError):
    """A runtime adapter cannot safely validate the proposed project artifacts."""


class ReadinessExecutor(Protocol):
    def validate(
        self,
        project_root: Path,
        architecture: dict[str, Any],
        manifest: dict[str, Any],
    ) -> list[dict[str, Any]]: ...


class RuntimeAdapter(Protocol):
    adapter_id: str
    policy_version: int
    supports_behavior_probes: bool

    def configure_execution(self, *, timeout_seconds: float, agent_options: dict[str, Any]) -> tuple[Any, Any]: ...

    def planning_policy(self, *, syntax_checks: bool) -> dict[str, Any]: ...

    def normalise_manifest(
        self,
        architecture: dict[str, Any],
        manifest: dict[str, Any],
    ) -> dict[str, Any]: ...

    def normalise_generated_content(
        self,
        path: str,
        content: str,
        architecture: dict[str, Any],
        manifest: dict[str, Any],
    ) -> str: ...

    def validate_manifest(
        self,
        architecture: dict[str, Any],
        manifest: dict[str, Any],
        *,
        syntax_checks: bool,
    ) -> None: ...

    def validate_project(
        self,
        workspace: CleanWorkspace,
        plan: dict[str, Any],
        *,
        syntax_checks: bool,
        architecture: dict[str, Any] | None = None,
        manifest: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]: ...

    def validate_readiness(
        self,
        workspace: CleanWorkspace,
        architecture: dict[str, Any],
        manifest: dict[str, Any],
    ) -> list[dict[str, Any]]: ...

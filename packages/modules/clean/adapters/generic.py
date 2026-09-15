from __future__ import annotations

from typing import Any

from packages.modules.clean.adapters.base import RuntimeAdapterError
from packages.modules.clean.manifest import normalise_manifest
from packages.modules.clean.validation import validate_project
from packages.modules.clean.workspace import CleanWorkspace


class GenericRuntimeAdapter:
    """Structural fallback for runtimes without a specialized local adapter."""

    adapter_id = "generic-structure"
    policy_version = 1
    supports_behavior_probes = False

    def configure_execution(self, *, timeout_seconds: float, agent_options: dict[str, Any]) -> tuple[None, None]:
        # Unsupported runtimes must never receive a Python executable or prober.
        return None, None
    _validators = frozenset({"manifest-integrity", "utf8-text"})

    def planning_policy(self, *, syntax_checks: bool) -> dict[str, Any]:
        del syntax_checks
        return {
            "adapter": self.adapter_id,
            "policy_version": self.policy_version,
            "supported_languages": ["*"],
            "readiness_ceiling": "structure",
            "available_checks": sorted(self._validators),
            "adapter_file_generation": False,
        }

    def normalise_manifest(
        self,
        architecture: dict[str, Any],
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        if not architecture.get("components"):
            return manifest
        return normalise_manifest(manifest, architecture)

    def normalise_generated_content(
        self,
        path: str,
        content: str,
        architecture: dict[str, Any],
        manifest: dict[str, Any],
    ) -> str:
        del path, architecture, manifest
        return content

    def validate_manifest(
        self,
        architecture: dict[str, Any],
        manifest: dict[str, Any],
        *,
        syntax_checks: bool,
    ) -> None:
        del architecture
        del syntax_checks
        _validate_declared_operations(
            manifest,
            available_checks=self._validators,
            adapter_file_generation=False,
            adapter_id=self.adapter_id,
        )

    def validate_project(
        self,
        workspace: CleanWorkspace,
        plan: dict[str, Any],
        *,
        syntax_checks: bool,
        architecture: dict[str, Any] | None = None,
        manifest: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        del syntax_checks
        del architecture
        del manifest
        return validate_project(workspace, plan, syntax_checks=False)

    def validate_readiness(
        self,
        workspace: CleanWorkspace,
        architecture: dict[str, Any],
        manifest: dict[str, Any],
    ) -> list[dict[str, Any]]:
        del workspace
        del architecture
        del manifest
        return [
            {
                "name": "executable-validation",
                "status": "skipped",
                "message": "The generic adapter cannot prove executable readiness",
                "paths": [],
            }
        ]


def _validate_declared_operations(
    manifest: dict[str, Any],
    *,
    available_checks: frozenset[str],
    adapter_file_generation: bool,
    adapter_id: str,
) -> None:
    adapter_owned = sorted(
        item["path"]
        for item in manifest["files"]
        if item["generation_owner"] == "adapter"
    )
    if adapter_owned and not adapter_file_generation:
        raise RuntimeAdapterError(
            f"Runtime adapter {adapter_id!r} cannot generate declared files: "
            + ", ".join(adapter_owned)
        )
    unsupported: dict[str, list[str]] = {}
    for item in manifest["files"]:
        requested = set(item["local_validators"]) | set(item["integration_checks"])
        missing = sorted(requested - available_checks)
        if missing:
            unsupported[item["path"]] = missing
    if unsupported:
        rendered = "; ".join(
            f"{path}: {', '.join(checks)}"
            for path, checks in sorted(unsupported.items())
        )
        raise RuntimeAdapterError(
            f"Manifest requests checks unsupported by {adapter_id!r}: {rendered}"
        )

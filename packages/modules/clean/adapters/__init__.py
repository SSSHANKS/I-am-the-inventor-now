from __future__ import annotations

from typing import Any

from packages.modules.clean.adapters.base import (
    ReadinessExecutor,
    RuntimeAdapter,
    RuntimeAdapterError,
)
from packages.modules.clean.adapters.generic import GenericRuntimeAdapter
from packages.modules.clean.adapters.python import PythonRuntimeAdapter


def project_first_planning_policy(*, syntax_checks: bool) -> dict[str, Any]:
    adapters = (
        PythonRuntimeAdapter().planning_policy(syntax_checks=syntax_checks),
        GenericRuntimeAdapter().planning_policy(syntax_checks=syntax_checks),
    )
    return {
        "schema_version": 1,
        "generated_tests": False,
        "network_installation": False,
        "executable_validation": False,
        "adapters": list(adapters),
    }


def select_runtime_adapter(
    architecture: dict[str, Any],
    *,
    readiness_executor: ReadinessExecutor | None = None,
) -> RuntimeAdapter:
    language = str(architecture["project_profile"]["language"])
    if "python" in language.casefold():
        return PythonRuntimeAdapter(readiness_executor=readiness_executor)
    return GenericRuntimeAdapter()


__all__ = [
    "GenericRuntimeAdapter",
    "PythonRuntimeAdapter",
    "ReadinessExecutor",
    "RuntimeAdapter",
    "RuntimeAdapterError",
    "project_first_planning_policy",
    "select_runtime_adapter",
]

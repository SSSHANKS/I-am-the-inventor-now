from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

_CLASS_KINDS = frozenset({"class", "exception", "protocol"})


def python_contract_module(
    contract: Mapping[str, Any],
    contracts: Iterable[Mapping[str, Any]],
) -> str:
    """Return the import module that provides a Python symbol contract."""
    qualified_name = str(contract["qualified_name"])
    owner = _containing_class(qualified_name, contracts)
    provider_name = owner if owner is not None else qualified_name
    module, separator, _ = provider_name.rpartition(".")
    return module if separator else provider_name


def python_contract_attributes(
    contract: Mapping[str, Any],
    contracts: Iterable[Mapping[str, Any]],
) -> tuple[str, ...]:
    """Return the binding path within a contract's provider module."""
    qualified_name = str(contract["qualified_name"])
    module = python_contract_module(contract, contracts)
    relative = qualified_name.removeprefix(f"{module}.")
    return tuple(part for part in relative.split(".") if part)


def python_contract_target(
    contract: Mapping[str, Any],
    contracts: Iterable[Mapping[str, Any]],
) -> str:
    """Return a ``module:attribute.path`` target for a Python contract."""
    module = python_contract_module(contract, contracts)
    attributes = ".".join(python_contract_attributes(contract, contracts))
    return f"{module}:{attributes}" if attributes else module


def _containing_class(
    qualified_name: str,
    contracts: Iterable[Mapping[str, Any]],
) -> str | None:
    candidates = [
        str(item["qualified_name"])
        for item in contracts
        if item.get("kind") in _CLASS_KINDS
        and qualified_name.startswith(f"{item['qualified_name']}.")
    ]
    return max(candidates, key=len, default=None)

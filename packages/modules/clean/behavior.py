from __future__ import annotations

import ast
import hashlib
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from packages.modules.clean.diagnostics import CleanDiagnostic
from packages.modules.clean.probe_contracts import (
    ProbeArchitectureError,
    ProbeContractError,
    validate_probe_contracts,
)

MAX_BEHAVIOR_PROBES = 64
MAX_PROBE_CHARS = 32_000
MAX_SUITE_CHARS = 256_000

_FORBIDDEN_IMPORT_ROOTS = frozenset(
    {
        "ctypes",
        "ftplib",
        "http",
        "importlib",
        "multiprocessing",
        "requests",
        "runpy",
        "smtplib",
        "socket",
        "urllib",
        "webbrowser",
    }
)
_FORBIDDEN_CALLS = frozenset(
    {
        "__import__",
        "compile",
        "eval",
        "exec",
        "input",
        "os.popen",
        "os.startfile",
        "os.system",
        "subprocess.Popen",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "subprocess.getoutput",
        "subprocess.getstatusoutput",
        "subprocess.run",
    }
)
_ABSOLUTE_WINDOWS_PATH = re.compile(r"^[A-Za-z]:[\\/]")


class BehaviorProbeError(ValueError):
    """A behavior probe suite is unsafe or inconsistent with its architecture."""


class BehaviorProbeExecutor(Protocol):
    def validate(
        self,
        project_root: Path,
        architecture: dict[str, Any],
        manifest: dict[str, Any],
        suite: dict[str, Any],
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class _ProcessResult:
    returncode: int | None
    output: str
    timed_out: bool = False


def validate_behavior_probe_suite(
    suite: dict[str, Any],
    architecture: dict[str, Any],
    *,
    require_complete: bool = True,
) -> dict[str, Any]:
    """Validate coverage and a conservative execution policy before any probe runs."""
    probes = suite["probes"]
    if len(probes) > MAX_BEHAVIOR_PROBES:
        raise BehaviorProbeError(
            f"Behavior probe suite exceeds the {MAX_BEHAVIOR_PROBES}-probe limit"
        )
    if sum(len(item["code"]) for item in probes) > MAX_SUITE_CHARS:
        raise BehaviorProbeError("Behavior probe suite exceeds the text-size limit")

    identifiers = [item["probe_id"] for item in probes]
    if len(identifiers) != len(set(identifiers)):
        raise BehaviorProbeError("Behavior probe suite contains duplicate probe IDs")

    capabilities = {
        item["capability_id"]: item for item in architecture["capabilities"]
    }
    allowed_import_roots = set(sys.stdlib_module_names) | {
        str(item["qualified_name"]).partition(".")[0]
        for item in architecture["contracts"]
        if "." in str(item["qualified_name"])
    }
    probes_by_capability: dict[str, list[dict[str, Any]]] = {}
    assigned_requirements: set[tuple[str, str]] = set()
    for probe in probes:
        capability_id = probe["capability_id"]
        if capability_id not in capabilities:
            raise BehaviorProbeError(
                f"Probe {probe['probe_id']} references unknown capability {capability_id}"
            )
        capability = capabilities[capability_id]
        if len(probe["requirement_ids"]) != 1:
            raise BehaviorProbeError(
                f"Probe {probe['probe_id']} must cover exactly one requirement"
            )
        requirement_id = probe["requirement_ids"][0]
        assignment = (capability_id, requirement_id)
        if assignment in assigned_requirements:
            raise BehaviorProbeError(
                f"Behavior requirement {requirement_id} is assigned to multiple "
                f"{capability_id} probes"
            )
        assigned_requirements.add(assignment)
        unknown_requirements = set(probe["requirement_ids"]) - set(
            capability["requirement_ids"]
        )
        if unknown_requirements:
            raise BehaviorProbeError(
                f"Probe {probe['probe_id']} claims requirements outside {capability_id}: "
                + ", ".join(sorted(unknown_requirements))
            )
        unknown_scenarios = set(probe["scenario_ids"]) - set(
            capability["scenario_ids"]
        )
        if unknown_scenarios:
            raise BehaviorProbeError(
                f"Probe {probe['probe_id']} claims scenarios outside {capability_id}: "
                + ", ".join(sorted(unknown_scenarios))
            )
        _validate_probe_code(
            probe["probe_id"],
            probe["code"],
            allowed_import_roots,
            requires_boundary_mock=_requires_boundary_mock(capability),
        )
        _validate_probe_semantics(probe, capability, architecture)
        try:
            validate_probe_contracts(probe['code'], architecture['contracts'])
        except ProbeArchitectureError as exc:
            raise ProbeArchitectureError(
                f"Probe {probe['probe_id']} ({requirement_id}): {exc}"
            ) from exc
        except ProbeContractError as exc:
            raise BehaviorProbeError(f"Probe {probe['probe_id']}: {exc}") from exc
        probes_by_capability.setdefault(capability_id, []).append(probe)

    if not require_complete:
        return suite
    coverage_errors: list[str] = []
    for capability_id, capability in capabilities.items():
        assigned = probes_by_capability.get(capability_id, [])
        covered_requirements = {
            item for probe in assigned for item in probe["requirement_ids"]
        }
        missing_requirements = set(capability["requirement_ids"]) - covered_requirements
        if missing_requirements:
            coverage_errors.append(
                f"Behavior probes do not cover {capability_id} requirements: "
                + ", ".join(sorted(missing_requirements))
            )
    required_scenarios = {
        scenario_id
        for capability in capabilities.values()
        for scenario_id in capability["scenario_ids"]
    }
    covered_scenarios = {
        scenario_id for probe in probes for scenario_id in probe["scenario_ids"]
    }
    missing_scenarios = required_scenarios - covered_scenarios
    if missing_scenarios:
        coverage_errors.append(
            "Behavior probes do not use required scenarios: "
            + ", ".join(sorted(missing_scenarios))
        )
    if coverage_errors:
        raise BehaviorProbeError("; ".join(coverage_errors))
    return suite


def _validate_probe_code(
    probe_id: str,
    code: str,
    allowed_import_roots: set[str],
    *,
    requires_boundary_mock: bool,
) -> None:
    if len(code) > MAX_PROBE_CHARS:
        raise BehaviorProbeError(f"Probe {probe_id} exceeds the text-size limit")
    try:
        tree = ast.parse(code, filename=f"{probe_id}.py")
    except SyntaxError as exc:
        raise BehaviorProbeError(f"Probe {probe_id} is not valid Python") from exc
    try:
        compile(tree, filename=f"{probe_id}.py", mode="exec")
    except SyntaxError as exc:
        raise BehaviorProbeError(f"Probe {probe_id} is not executable Python: {exc.msg}") from exc
    assertions = _executed_assertions(tree)
    if not assertions:
        raise BehaviorProbeError(
            f"Probe {probe_id} contains no module-executed assertions"
        )
    if requires_boundary_mock and not _uses_unittest_mock(tree):
        raise BehaviorProbeError(
            f"Probe {probe_id} must mock external boundary effects with unittest.mock"
        )
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots = {alias.name.partition(".")[0] for alias in node.names}
            forbidden = roots & _FORBIDDEN_IMPORT_ROOTS
            if forbidden:
                raise BehaviorProbeError(
                    f"Probe {probe_id} imports forbidden module {sorted(forbidden)[0]!r}"
                )
            unknown = roots - allowed_import_roots
            if unknown:
                raise BehaviorProbeError(
                    f"Probe {probe_id} imports undeclared module {sorted(unknown)[0]!r}"
                )
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").partition(".")[0]
            if root in _FORBIDDEN_IMPORT_ROOTS:
                raise BehaviorProbeError(
                    f"Probe {probe_id} imports forbidden module {root!r}"
                )
            if not root or root not in allowed_import_roots:
                raise BehaviorProbeError(
                    f"Probe {probe_id} imports undeclared module {root!r}"
                )
        elif isinstance(node, ast.Call):
            name = _call_name(node.func)
            if name in _FORBIDDEN_CALLS or name.startswith(("os.exec", "os.spawn")):
                raise BehaviorProbeError(
                    f"Probe {probe_id} calls forbidden operation {name!r}"
                )
        elif isinstance(node, ast.ExceptHandler):
            if node.type is None or _call_name(node.type) in {"Exception", "BaseException"}:
                raise BehaviorProbeError(
                    f"Probe {probe_id} catches an unspecific exception"
                )
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value.strip()
            normalized = value.replace("\\", "/")
            if (
                _ABSOLUTE_WINDOWS_PATH.match(value)
                or value.startswith(("/etc/", "/home/"))
                or normalized == ".."
                or normalized.startswith("../")
                or "/../" in normalized
            ):
                raise BehaviorProbeError(
                    f"Probe {probe_id} contains a host-absolute path"
                )
    # Resolve import aliases, including ``from sys import path as search``.
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                aliases[item.asname or item.name.partition('.')[0]] = item.name
        elif isinstance(node, ast.ImportFrom):
            for item in node.names:
                aliases[item.asname or item.name] = f"{node.module}.{item.name}"
    protected = {f"sys.{name}" for name in (
        "path", "modules", "meta_path", "path_hooks", "path_importer_cache",
    )}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Name, ast.Attribute)):
            name = _call_name(node)
            head, dot, tail = name.partition('.')
            resolved = aliases.get(head, head) + (dot + tail if dot else '')
            if any(resolved == item or resolved.startswith(item + '.') for item in protected):
                raise BehaviorProbeError(
                    f"Probe {probe_id} accesses import state {resolved!r}; "
                    "import the generated project directly, without replacing it"
                )
    if all(_weak_assertion(item.test) for item in assertions):
        raise BehaviorProbeError(
            f"Probe {probe_id} contains only type, existence, or non-null assertions "
            "or always-true assertions; assert an observable result of the real project"
        )


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _validate_probe_semantics(
    probe: dict[str, Any],
    capability: dict[str, Any],
    architecture: dict[str, Any],
) -> None:
    """Reject plausible-looking assertions that contradict or exceed the oracle."""
    tree = ast.parse(probe["code"], filename=f"{probe['probe_id']}.py")
    requirement_id = probe["requirement_ids"][0]
    component_ids = set(capability["component_ids"])
    relevant_contracts = [
        contract
        for contract in architecture["contracts"]
        if contract.get("component_id") in component_ids
    ]
    own_text = " ".join(
        _rule_text(rule)
        for contract in relevant_contracts
        for rule in contract.get("behavior_rules", [])
        if requirement_id in rule.get("requirement_ids", [])
    ).casefold()

    imported_constants = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if (alias.asname or alias.name).isupper()
    }
    non_null_names = {
        node.left.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Name)
        and len(node.ops) == 1
        and isinstance(node.ops[0], ast.IsNot)
        and len(node.comparators) == 1
        and isinstance(node.comparators[0], ast.Constant)
        and node.comparators[0].value is None
    }
    unsupported = imported_constants & non_null_names
    non_null_grounding = (
        "non-null",
        "not none",
        "sentinel",
        "unique object",
        "distinct object",
        "identity",
    )
    if unsupported and not any(term in own_text for term in non_null_grounding):
        raise BehaviorProbeError(
            f"Probe {probe['probe_id']} invents a non-null value for public constant "
            f"{sorted(unsupported)[0]!r}; its requirement only supports observable "
            "behavior explicitly stated by the architecture"
        )

    _reject_disposable_weak_callbacks(probe["probe_id"], tree)
    _reject_unapplied_decorator_factories(
        probe["probe_id"], tree, relevant_contracts
    )
    _validate_initialization_probe_focus(probe["probe_id"], tree, own_text)
    _validate_lifecycle_observer_path(probe["probe_id"], tree, own_text)

    async_names = {
        node.name for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
    }
    if not async_names:
        return
    registration_terms = {"add", "attach", "bind", "connect", "register", "subscribe"}
    dispatch_terms = {"call", "dispatch", "emit", "invoke", "notify", "publish", "send"}
    registers_async = any(
        isinstance(node, ast.Call)
        and _call_name(node.func).rsplit(".", 1)[-1].casefold() in registration_terms
        and any(isinstance(arg, ast.Name) and arg.id in async_names for arg in node.args)
        for node in ast.walk(tree)
    )
    directly_dispatches = any(
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Call)
        and _call_name(statement.value.func).rsplit(".", 1)[-1].casefold()
        in dispatch_terms
        for statement in tree.body
    )
    constraining_requirements = {
        rule_requirement
        for contract in relevant_contracts
        for rule in contract.get("behavior_rules", [])
        if _is_sync_async_error_rule(_rule_text(rule))
        for rule_requirement in rule.get("requirement_ids", [])
    }
    if (
        registers_async
        and directly_dispatches
        and constraining_requirements
        and requirement_id not in constraining_requirements
    ):
        raise BehaviorProbeError(
            f"Probe {probe['probe_id']} contradicts synchronous/asynchronous error "
            "requirements " + ", ".join(sorted(constraining_requirements))
        )


def _rule_text(rule: dict[str, Any]) -> str:
    evidence = " ".join(
        str(item.get("excerpt", "")) for item in rule.get("evidence", [])
    )
    return f"{rule.get('statement', '')} {evidence}"


def _is_sync_async_error_rule(text: str) -> bool:
    folded = text.casefold()
    return (
        any(term in folded for term in ("synchronous", "sync ", "sync dispatch"))
        and any(term in folded for term in ("asynchronous", "async", "coroutine"))
        and any(term in folded for term in ("error", "raise", "reject", "runtimeerror"))
    )


def _reject_disposable_weak_callbacks(probe_id: str, tree: ast.Module) -> None:
    registration_terms = {"add", "attach", "bind", "connect", "register", "subscribe"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _call_name(node.func).rsplit(".", 1)[-1].casefold() not in registration_terms:
            continue
        weak_is_false = any(
            keyword.arg == "weak"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is False
            for keyword in node.keywords
        )
        if not weak_is_false and any(isinstance(argument, ast.Lambda) for argument in node.args):
            raise BehaviorProbeError(
                f"Probe {probe_id} registers a disposable lambda through a potentially "
                "weak connection; retain a named callback or request strong ownership"
            )


def _reject_unapplied_decorator_factories(
    probe_id: str,
    tree: ast.Module,
    contracts: list[dict[str, Any]],
) -> None:
    factory_methods: set[str] = set()
    for contract in contracts:
        declaration = str(contract.get("declaration", ""))
        try:
            parsed = ast.parse(declaration)
        except SyntaxError:
            continue
        for node in ast.walk(parsed):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            parameters = [argument.arg for argument in node.args.args]
            if parameters and parameters[0] in {"self", "cls"}:
                parameters = parameters[1:]
            if not parameters or parameters[0].casefold() in {
                "callback",
                "callable",
                "function",
                "handler",
                "listener",
                "receiver",
                "target",
            }:
                continue
            if _call_name(node.returns).casefold() in {"callable", "decorator"}:
                factory_methods.add(node.name)
    if not factory_methods:
        return
    defined_callables = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for statement in tree.body:
        if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
            continue
        call = statement.value
        if _call_name(call.func).rsplit(".", 1)[-1] not in factory_methods:
            continue
        if call.args and isinstance(call.args[0], ast.Name) and call.args[0].id in defined_callables:
            raise BehaviorProbeError(
                f"Probe {probe_id} passes a callback to decorator factory "
                f"{_call_name(call.func)!r} without applying the returned decorator"
            )


def _validate_initialization_probe_focus(
    probe_id: str,
    tree: ast.Module,
    own_text: str,
) -> None:
    if "initializ" not in own_text:
        return
    # Initialization may be observable without state inspection: importing a
    # package exposes its public surface and a factory can initialize a cached
    # instance. Require attribute inspection only when the requirement itself
    # promises an attribute-like value. Private bookkeeping is rejected by the
    # architecture-contract validator independently.
    attribute_terms = ("attribute", "property", "field")
    if not any(term in own_text for term in attribute_terms):
        return
    assertions = _executed_assertions(tree)
    if any(any(isinstance(node, ast.Attribute) for node in ast.walk(item.test)) for item in assertions):
        return
    raise BehaviorProbeError(
        f"Probe {probe_id} is assigned to an initialization requirement but does not "
        "assert initialized state"
    )


def _validate_lifecycle_observer_path(
    probe_id: str,
    tree: ast.Module,
    own_text: str,
) -> None:
    lifecycle_terms = ("lifecycle", "observer", "tracking notification", "notification event")
    if not any(term in own_text for term in lifecycle_terms):
        return
    registration_receivers = {
        _call_name(node.func.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and any(term in node.func.attr.casefold() for term in ("observer", "lifecycle"))
        and any(term in node.func.attr.casefold() for term in ("add", "attach", "connect", "register", "subscribe"))
    }
    if not registration_receivers:
        return
    producer_terms = {"connect", "disconnect", "emit", "invoke", "notify", "publish", "send"}
    producer_receivers = {
        _call_name(node.func.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr.casefold() in producer_terms
    }
    if registration_receivers & producer_receivers:
        return
    if any(term in own_text for term in ("global", "shared registry", "singleton")):
        return
    raise BehaviorProbeError(
        f"Probe {probe_id} registers a disconnected lifecycle observer; register through "
        "the producing object or an explicitly shared public observer channel"
    )


def _executed_assertions(tree: ast.Module) -> list[ast.Assert]:
    """Return assertions reachable at module execution, excluding uncalled definitions."""
    assertions: list[ast.Assert] = []

    def visit_statement(statement: ast.stmt) -> None:
        if isinstance(statement, ast.Assert):
            assertions.append(statement)
            return
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return
        for child in ast.iter_child_nodes(statement):
            if isinstance(child, ast.stmt):
                visit_statement(child)

    for statement in tree.body:
        visit_statement(statement)
    return assertions


def _weak_assertion(expression: ast.expr) -> bool:
    if _constant_truth(expression) is True:
        return True
    if isinstance(expression, ast.BoolOp):
        weak = [_weak_assertion(value) for value in expression.values]
        # An OR can pass via its weakest alternative; an AND requires all terms.
        return any(weak) if isinstance(expression.op, ast.Or) else all(weak)
    if isinstance(expression, ast.Call):
        return _call_name(expression.func) in {"callable", "hasattr", "isinstance", "issubclass"}
    if isinstance(expression, ast.Compare) and len(expression.ops) == 1:
        comparator = expression.comparators[0]
        if _nonnegative_measure_tautology(
            expression.left, expression.ops[0], comparator
        ):
            return True
        if isinstance(expression.ops[0], (ast.Is, ast.IsNot)) and isinstance(
            comparator, ast.Constant
        ) and comparator.value is None:
            return True
        if isinstance(expression.left, ast.Call) and _call_name(expression.left.func) == "type":
            return True
    return False


def _nonnegative_measure_tautology(
    left: ast.expr, operator: ast.cmpop, right: ast.expr
) -> bool:
    """Detect assertions such as ``len(events) >= 0`` that cannot fail."""
    def is_len(node: ast.expr) -> bool:
        return (
            isinstance(node, ast.Call)
            and _call_name(node.func) == "len"
            and len(node.args) == 1
            and not node.keywords
        )

    def is_zero(node: ast.expr) -> bool:
        return isinstance(node, ast.Constant) and node.value == 0

    return (
        is_len(left)
        and is_zero(right)
        and isinstance(operator, (ast.GtE, ast.NotEq))
    ) or (
        is_zero(left)
        and is_len(right)
        and isinstance(operator, (ast.LtE, ast.NotEq))
    )


def _constant_truth(expression: ast.expr) -> bool | None:
    """Recognize literal tautologies without executing probe expressions."""
    if isinstance(expression, ast.BoolOp):
        values = [_constant_truth(value) for value in expression.values]
        if isinstance(expression.op, ast.Or):
            return True if True in values else (False if all(v is False for v in values) else None)
        return False if False in values else (True if all(v is True for v in values) else None)
    if isinstance(expression, ast.UnaryOp) and isinstance(expression.op, ast.Not):
        value = _constant_truth(expression.operand)
        return None if value is None else not value
    try:
        if isinstance(expression, ast.Compare) and len(expression.ops) == 1:
            left = ast.literal_eval(expression.left)
            right = ast.literal_eval(expression.comparators[0])
            op = expression.ops[0]
            if isinstance(op, ast.Eq):
                return left == right
            if isinstance(op, ast.NotEq):
                return left != right
            if isinstance(op, ast.In):
                return left in right
            if isinstance(op, ast.NotIn):
                return left not in right
            if isinstance(op, ast.Lt):
                return left < right
            if isinstance(op, ast.LtE):
                return left <= right
            if isinstance(op, ast.Gt):
                return left > right
            if isinstance(op, ast.GtE):
                return left >= right
            return None
        return bool(ast.literal_eval(expression))
    except (ValueError, TypeError, SyntaxError):
        return None


def _uses_unittest_mock(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            alias.name == "unittest.mock" for alias in node.names
        ):
            return True
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            "unittest.mock"
        ):
            return True
        if isinstance(node, ast.ImportFrom) and node.module == "unittest" and any(
            alias.name == "mock" for alias in node.names
        ):
            return True
    return False


def _requires_boundary_mock(capability: dict[str, Any]) -> bool:
    words = set(re.findall(r"[a-z]+", str(capability.get("purpose", "")).casefold()))
    return bool(words & {"editor", "external", "http", "network", "process", "spawn"})


class LocalPythonBehaviorProbeExecutor:
    """Execute stable behavior probes in disposable project copies."""

    def __init__(
        self,
        *,
        python_executable: str | Path | None = None,
        timeout_seconds: float = 15.0,
        max_output_chars: int = 4_000,
    ) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("behavior probe timeout must be finite and positive")
        if max_output_chars < 256:
            raise ValueError("behavior probe output limit must be at least 256 characters")
        self.python_executable = str(python_executable or sys.executable)
        self.timeout_seconds = timeout_seconds
        self.max_output_chars = max_output_chars

    def validate(
        self,
        project_root: Path,
        architecture: dict[str, Any],
        manifest: dict[str, Any],
        suite: dict[str, Any],
    ) -> list[dict[str, Any]]:
        validate_behavior_probe_suite(suite, architecture, require_complete=False)
        coverage = []
        try:
            validate_behavior_probe_suite(suite, architecture)
        except BehaviorProbeError as exc:
            coverage.append({
                "name": "python-behavior-coverage",
                "status": "skipped",
                "message": str(exc),
                "paths": [],
            })
        root = project_root.resolve()
        if not root.is_dir():
            return [_failed_suite("Generated project directory is missing", [], [])]

        failures: list[CleanDiagnostic] = []
        rejected = 0
        with tempfile.TemporaryDirectory(prefix="cb-") as temporary:
            temp_root = Path(temporary)
            for probe in suite["probes"]:
                probe_root = temp_root / probe["probe_id"]
                copied_project = probe_root / "project"
                shutil.copytree(root, copied_project)
                probe_file = probe_root / "probe.py"
                probe_file.write_text(probe["code"], encoding="utf-8")
                before = _snapshot(copied_project)
                result = self._run(
                    probe_file,
                    cwd=copied_project,
                    environment=_environment(probe_root),
                    import_roots=_import_roots(copied_project, architecture),
                    project_root=root,
                    probe_root=probe_root,
                )
                changed = _changed_paths(before, _snapshot(copied_project))
                if result.returncode != 0 and "PROBE_INTEGRITY:" in result.output:
                    rejected += 1
                    coverage.append({
                        "name": "python-behavior-coverage",
                        "status": "skipped",
                        "message": f"{probe['probe_id']} rejected: " + result.output.strip(),
                        "paths": [],
                    })
                    continue
                if result.returncode == 0 and not changed:
                    continue
                paths = _capability_paths(
                    probe["capability_id"], architecture, manifest
                )
                reason = (
                    "timed out"
                    if result.timed_out
                    else (
                        "modified generated project files: " + ", ".join(changed)
                        if changed
                        else f"exited {result.returncode}"
                    )
                )
                message = f"{probe['probe_id']} failed: {reason}"
                if result.output.strip():
                    message += "\n" + result.output.strip()
                failures.append(
                    CleanDiagnostic(
                        stage="behavior-validation",
                        check_id=f"clean.behavior.{probe['probe_id']}",
                        message=message,
                        paths=tuple(paths),
                        contract_ids=tuple(
                            _capability_contracts(
                                probe["capability_id"], architecture
                            )
                        ),
                        requirement_ids=tuple(probe["requirement_ids"]),
                        expected="all asserted observable behaviors pass",
                        actual=reason,
                        repair_hint=(
                            "Repair the implicated production implementation; preserve "
                            "the behavior probe as the independent oracle."
                        ),
                    )
                )

        if not failures:
            return [
                *coverage,
                {
                    "name": "python-behavior-probes",
                    "status": "pass",
                    "message": f"Passed {len(suite['probes']) - rejected} behavior probe(s)",
                    "paths": [],
                }
            ]
        paths = sorted({path for item in failures for path in item.paths})
        return [
            *coverage,
            {
                "name": "python-behavior-probes",
                "status": "fail",
                "message": f"Failed {len(failures)} of {len(suite['probes'])} behavior probe(s)",
                "paths": paths,
                "diagnostics": [item.to_dict() for item in failures],
            }
        ]

    def _run(
        self,
        probe_file: Path,
        *,
        cwd: Path,
        environment: dict[str, str],
        import_roots: list[Path],
        project_root: Path,
        probe_root: Path,
    ) -> _ProcessResult:
        bootstrap = (
            "import runpy,sys;"
            f"sys.path[:0]={[str(item) for item in import_roots]!r};"
            f"runpy.run_path({str(Path(__file__).with_name('probe_runtime.py').resolve())!r},"
            f"init_globals={{'PROBE_FILE': {str(probe_file)!r}, "
            f"'IMPORT_ROOTS': {[str(item) for item in import_roots]!r}}})"
        )
        try:
            completed = subprocess.run(
                [self.python_executable, "-I", "-B", "-c", bootstrap],
                cwd=cwd,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            return _ProcessResult(
                None,
                self._bounded(
                    _joined_output(exc.stdout, exc.stderr), project_root, probe_root
                ),
                timed_out=True,
            )
        return _ProcessResult(
            completed.returncode,
            self._bounded(
                _joined_output(completed.stdout, completed.stderr),
                project_root,
                probe_root,
            ),
        )

    def _bounded(self, output: str, project_root: Path, probe_root: Path) -> str:
        sanitized = output.replace(str(project_root), "<project>")
        sanitized = sanitized.replace(str(project_root).replace("\\", "/"), "<project>")
        sanitized = sanitized.replace(str(probe_root), "<probe>")
        sanitized = sanitized.replace(str(probe_root).replace("\\", "/"), "<probe>")
        python_root = str(Path(self.python_executable).resolve().parent)
        sanitized = sanitized.replace(python_root, "<python-root>")
        sanitized = sanitized.replace(python_root.replace("\\", "/"), "<python-root>")
        sanitized = sanitized.replace(self.python_executable, "<python>")
        if len(sanitized) <= self.max_output_chars:
            return sanitized
        omitted = len(sanitized) - self.max_output_chars
        return f"[... {omitted} characters omitted ...]\n{sanitized[-self.max_output_chars:]}"


def _import_roots(project_root: Path, architecture: dict[str, Any]) -> list[Path]:
    layout = str(architecture["project_profile"]["layout"]).strip().casefold()
    roots = [project_root]
    if layout == "src":
        roots.insert(0, project_root / "src")
    return roots


def _capability_paths(
    capability_id: str,
    architecture: dict[str, Any],
    manifest: dict[str, Any],
) -> list[str]:
    capability = next(
        item for item in architecture["capabilities"] if item["capability_id"] == capability_id
    )
    component_ids = set(capability["component_ids"])
    requirement_ids = set(capability["requirement_ids"])
    return sorted(
        item["path"]
        for item in manifest["files"]
        if item["component_id"] in component_ids
        and item["category"] in {"source", "configuration"}
        and requirement_ids.intersection(item["requirement_ids"])
    )


def _capability_contracts(
    capability_id: str,
    architecture: dict[str, Any],
) -> list[str]:
    capability = next(
        item for item in architecture["capabilities"] if item["capability_id"] == capability_id
    )
    component_ids = set(capability["component_ids"])
    requirement_ids = set(capability["requirement_ids"])
    return sorted(
        item["contract_id"]
        for item in architecture["contracts"]
        if item["component_id"] in component_ids
        and requirement_ids.intersection(item["requirement_ids"])
    )


def _environment(temp_root: Path) -> dict[str, str]:
    environment = {
        key: os.environ[key]
        for key in ("SYSTEMROOT", "WINDIR")
        if key in os.environ
    }
    environment.update(
        {
            "TEMP": str(temp_root),
            "TMP": str(temp_root),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONNOUSERSITE": "1",
            "PYTHONUTF8": "1",
        }
    )
    return environment


def _snapshot(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }


def _changed_paths(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(
        path
        for path in before.keys() | after.keys()
        if before.get(path) != after.get(path)
    )


def _joined_output(stdout: str | bytes | None, stderr: str | bytes | None) -> str:
    def render(value: str | bytes | None) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value or ""

    return "\n".join(item for item in (render(stdout), render(stderr)) if item)


def _failed_suite(
    message: str,
    paths: list[str],
    requirement_ids: list[str],
) -> dict[str, Any]:
    diagnostic = CleanDiagnostic(
        stage="behavior-validation",
        check_id="clean.python-behavior-probes",
        message=message,
        paths=tuple(paths),
        requirement_ids=tuple(requirement_ids),
    )
    return {
        "name": "python-behavior-probes",
        "status": "fail",
        "message": message,
        "paths": paths,
        "diagnostics": [diagnostic.to_dict()],
    }

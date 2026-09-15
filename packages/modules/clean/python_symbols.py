from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True)
class PythonSymbolIssue:
    message: str
    paths: tuple[str, ...]


@dataclass(frozen=True)
class _ModuleFacts:
    path: str
    module: str
    is_package: bool
    bound_names: frozenset[str]
    declared_exports: frozenset[str] | None
    dynamic_attributes: bool
    tree: ast.Module


def find_python_symbol_issues(contents: dict[str, str]) -> list[PythonSymbolIssue]:
    """Find deterministic local-module, imported-symbol, and export mismatches."""
    facts = [_module_facts(path, content) for path, content in contents.items() if _is_python(path)]
    by_module: dict[str, _ModuleFacts] = {}
    issues: list[PythonSymbolIssue] = []
    for item in facts:
        previous = by_module.get(item.module)
        if previous is not None:
            issues.append(
                PythonSymbolIssue(
                    f"Python module {item.module!r} is provided by more than one file",
                    tuple(sorted((previous.path, item.path))),
                )
            )
            continue
        by_module[item.module] = item

    local_roots = {name.partition(".")[0] for name in by_module}
    for item in facts:
        issues.extend(_import_issues(item, by_module, local_roots))
        issues.extend(_export_issues(item))
    return issues


def _module_facts(path: str, content: str) -> _ModuleFacts:
    pure = PurePosixPath(path)
    is_package = pure.name == "__init__.py"
    parts = pure.parent.parts if is_package else pure.with_suffix("").parts
    module = ".".join(parts)
    tree = ast.parse(content, filename=path)
    bound_names: set[str] = set()
    declared_exports: frozenset[str] | None = None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound_names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                bound_names.update(_assigned_names(target))
            if any("__all__" in _assigned_names(target) for target in targets):
                value = node.value
                exports = _literal_string_collection(value) if value is not None else None
                if exports is not None:
                    declared_exports = frozenset(exports)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                bound_names.add(alias.asname or alias.name.partition(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    bound_names.add(alias.asname or alias.name)
    return _ModuleFacts(
        path=path,
        module=module,
        is_package=is_package,
        bound_names=frozenset(bound_names),
        declared_exports=declared_exports,
        dynamic_attributes="__getattr__" in bound_names,
        tree=tree,
    )


def _import_issues(
    source: _ModuleFacts,
    modules: dict[str, _ModuleFacts],
    local_roots: set[str],
) -> list[PythonSymbolIssue]:
    issues: list[PythonSymbolIssue] = []
    for node in ast.walk(source.tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_local(alias.name, local_roots) and alias.name not in modules:
                    issues.append(
                        PythonSymbolIssue(
                            f"{source.module!r} imports missing local module {alias.name!r}",
                            (source.path,),
                        )
                    )
        elif isinstance(node, ast.ImportFrom):
            target_name = _resolve_import_from(source, node)
            if target_name is None:
                issues.append(
                    PythonSymbolIssue(
                        f"{source.module!r} contains a relative import beyond its package",
                        (source.path,),
                    )
                )
                continue
            if not _is_local(target_name, local_roots):
                continue
            target = modules.get(target_name)
            if target is None:
                issues.append(
                    PythonSymbolIssue(
                        f"{source.module!r} imports missing local module {target_name!r}",
                        (source.path,),
                    )
                )
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                submodule = f"{target_name}.{alias.name}"
                if (
                    alias.name not in target.bound_names
                    and submodule not in modules
                    and not target.dynamic_attributes
                ):
                    issues.append(
                        PythonSymbolIssue(
                            (
                                f"{source.module!r} imports missing symbol {alias.name!r} "
                                f"from local module {target_name!r}"
                            ),
                            tuple(sorted((source.path, target.path))),
                        )
                    )
    return issues


def _export_issues(module: _ModuleFacts) -> list[PythonSymbolIssue]:
    if module.declared_exports is None or module.dynamic_attributes:
        return []
    return [
        PythonSymbolIssue(
            f"{module.module!r} declares missing __all__ symbol {name!r}",
            (module.path,),
        )
        for name in sorted(module.declared_exports - module.bound_names)
    ]


def _resolve_import_from(source: _ModuleFacts, node: ast.ImportFrom) -> str | None:
    if node.level == 0:
        return node.module or ""
    package = source.module.split(".") if source.is_package else source.module.split(".")[:-1]
    parents_to_remove = node.level - 1
    if parents_to_remove >= len(package):
        return None
    base = package[: len(package) - parents_to_remove] if parents_to_remove else package
    if node.module:
        base.extend(node.module.split("."))
    return ".".join(base)


def _assigned_names(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, (ast.Tuple, ast.List)):
        return {name for item in node.elts for name in _assigned_names(item)}
    return set()


def _literal_string_collection(node: ast.AST) -> list[str] | None:
    if not isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return None
    if not all(isinstance(item, ast.Constant) and isinstance(item.value, str) for item in node.elts):
        return None
    return [item.value for item in node.elts]


def _is_local(module: str, local_roots: set[str]) -> bool:
    return bool(module) and module.partition(".")[0] in local_roots


def _is_python(path: str) -> bool:
    return PurePosixPath(path).suffix.casefold() == ".py"

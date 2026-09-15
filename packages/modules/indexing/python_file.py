import ast
from typing import Any

from packages.modules.indexing.models import evidence
from packages.modules.skills.reading import Reader

_MAX_ANALYSIS_EVIDENCE_LINES = 80


def index_python_file(relative_path: str, result: dict[str, Any], source_reader: Reader) -> None:
    content = source_reader.read_file(relative_path)
    lines = content.splitlines()
    index_python_source(relative_path, result, content, lines)


def index_python_source(
    relative_path: str,
    result: dict[str, Any],
    content: str,
    lines: list[str],
) -> None:
    """Index Python source already loaded in memory (files and notebook cells)."""
    tree = ast.parse(content, filename=relative_path)

    if relative_path not in result["files_indexed"]:
        result["files_indexed"].append(relative_path)
    result["imports"].extend(_extract_imports(relative_path, tree, lines))
    result["classes"].extend(_extract_classes(relative_path, tree, lines))
    result["functions"].extend(_extract_functions(relative_path, tree, lines))
    result["entrypoints"].extend(_extract_entrypoints(relative_path, tree, lines))
    result["calls"].extend(_extract_calls(relative_path, tree, lines))
    result["analysis_targets"].extend(_build_python_targets(relative_path, tree, lines))


def _extract_imports(relative_path: str, tree: ast.AST, lines: list[str]) -> list[dict[str, Any]]:
    imports = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(
                    {
                        "file": relative_path,
                        "kind": "import",
                        "module": alias.name,
                        "name": alias.name,
                        "alias": alias.asname,
                        "line_start": node.lineno,
                        "line_end": node.end_lineno or node.lineno,
                        "evidence": evidence(relative_path, lines, node.lineno),
                    }
                )
        elif isinstance(node, ast.ImportFrom):
            module = "." * node.level + (node.module or "")
            for alias in node.names:
                imports.append(
                    {
                        "file": relative_path,
                        "kind": "from_import",
                        "module": module,
                        "name": alias.name,
                        "alias": alias.asname,
                        "line_start": node.lineno,
                        "line_end": node.end_lineno or node.lineno,
                        "evidence": evidence(relative_path, lines, node.lineno),
                    }
                )

    return sorted(imports, key=lambda item: (item["file"], item["line_start"], item["name"]))


def _extract_classes(relative_path: str, tree: ast.AST, lines: list[str]) -> list[dict[str, Any]]:
    visitor = _DefinitionVisitor(relative_path, lines)
    visitor.visit(tree)
    return sorted(
        visitor.classes,
        key=lambda item: (item["file"], item["line_start"], item["qualified_name"]),
    )


def _extract_functions(relative_path: str, tree: ast.AST, lines: list[str]) -> list[dict[str, Any]]:
    visitor = _DefinitionVisitor(relative_path, lines)
    visitor.visit(tree)
    return sorted(
        visitor.functions,
        key=lambda item: (item["file"], item["line_start"], item["qualified_name"]),
    )


def _extract_entrypoints(
    relative_path: str, tree: ast.AST, lines: list[str]
) -> list[dict[str, Any]]:
    entrypoints = []

    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _is_main_guard(node.test):
            entrypoints.append(
                {
                    "file": relative_path,
                    "kind": "main_guard",
                    "line_start": node.lineno,
                    "line_end": node.end_lineno or node.lineno,
                    "evidence": _node_evidence(relative_path, lines, node),
                }
            )

    return sorted(entrypoints, key=lambda item: (item["file"], item["line_start"]))


def _extract_calls(relative_path: str, tree: ast.AST, lines: list[str]) -> list[dict[str, Any]]:
    visitor = _CallVisitor(relative_path, lines)
    visitor.visit(tree)
    return visitor.calls


def _build_python_targets(
    relative_path: str, tree: ast.AST, lines: list[str]
) -> list[dict[str, Any]]:
    targets = []

    for entrypoint in _extract_entrypoints(relative_path, tree, lines):
        targets.append(
            {
                "file": relative_path,
                "target": relative_path,
                "target_type": "file",
                "reason": "file contains a Python main guard",
                "evidence": entrypoint["evidence"],
            }
        )

    for class_item in _extract_classes(relative_path, tree, lines):
        if class_item["definition_scope"] == "function":
            continue
        targets.append(
            {
                "file": relative_path,
                "target": class_item["qualified_name"],
                "target_type": "class",
                "reason": "class definition found in code",
                "evidence": class_item["evidence"],
            }
        )

    for function_item in _extract_functions(relative_path, tree, lines):
        if function_item["owner"] == "module":
            targets.append(
                {
                    "file": relative_path,
                    "target": function_item["qualified_name"],
                    "target_type": "function",
                    "reason": "module-level function found in code",
                    "evidence": function_item["evidence"],
                }
            )

    return targets


class _CallVisitor(ast.NodeVisitor):
    def __init__(self, relative_path: str, lines: list[str]):
        self.relative_path = relative_path
        self.lines = lines
        self.scope: list[str] = []
        self.calls: list[dict[str, Any]] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> Any:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        self.visit_FunctionDef(node)

    def visit_Call(self, node: ast.Call) -> Any:
        callee = _call_name(node.func)
        if callee:
            self.calls.append(
                {
                    "file": self.relative_path,
                    "caller": ".".join(self.scope) if self.scope else "module",
                    "callee": callee,
                    "line_start": node.lineno,
                    "line_end": node.end_lineno or node.lineno,
                    "evidence": _node_evidence(self.relative_path, self.lines, node),
                }
            )
        self.generic_visit(node)


class _DefinitionVisitor(ast.NodeVisitor):
    """Collect definitions with their complete lexical owner chain."""

    def __init__(self, relative_path: str, lines: list[str]):
        self.relative_path = relative_path
        self.lines = lines
        self.scope: list[tuple[str, str]] = []
        self.classes: list[dict[str, Any]] = []
        self.functions: list[dict[str, Any]] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> Any:
        self.classes.append(
            {
                "file": self.relative_path,
                "name": node.name,
                "qualified_name": self._qualified_name(node.name),
                "owner": self._owner(),
                "definition_scope": self._definition_scope(),
                "bases": [_safe_unparse(base) for base in node.bases],
                "decorators": [_safe_unparse(decorator) for decorator in node.decorator_list],
                "line_start": node.lineno,
                "line_end": node.end_lineno or node.lineno,
                "methods": [
                    child.name
                    for child in node.body
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                ],
                "evidence": _node_evidence(self.relative_path, self.lines, node),
            }
        )
        self.scope.append((node.name, "class"))
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.functions.append(
            {
                "file": self.relative_path,
                "name": node.name,
                "qualified_name": self._qualified_name(node.name),
                "owner": self._owner(),
                "definition_scope": self._definition_scope(),
                "kind": "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function",
                "args": _function_args(node.args),
                "decorators": [_safe_unparse(decorator) for decorator in node.decorator_list],
                "line_start": node.lineno,
                "line_end": node.end_lineno or node.lineno,
                "evidence": _node_evidence(self.relative_path, self.lines, node),
            }
        )
        self.scope.append((node.name, "function"))
        self.generic_visit(node)
        self.scope.pop()

    def _qualified_name(self, name: str) -> str:
        parts = [scope_name for scope_name, _kind in self.scope]
        return ".".join([*parts, name])

    def _owner(self) -> str:
        return ".".join(name for name, _kind in self.scope) or "module"

    def _definition_scope(self) -> str:
        return self.scope[-1][1] if self.scope else "module"


def _node_evidence(relative_path: str, lines: list[str], node: ast.AST) -> dict[str, Any]:
    """Capture enough of a definition/call to analyze behavior without unbounded prompts."""
    start = int(getattr(node, "lineno", 1))
    declared_end = int(getattr(node, "end_lineno", start) or start)
    end = min(declared_end, start + _MAX_ANALYSIS_EVIDENCE_LINES - 1)
    return evidence(relative_path, lines, start, end)


def _function_args(arguments: ast.arguments) -> list[str]:
    args = []
    args.extend(arg.arg for arg in arguments.posonlyargs)
    args.extend(arg.arg for arg in arguments.args)
    if arguments.vararg:
        args.append(f"*{arguments.vararg.arg}")
    args.extend(arg.arg for arg in arguments.kwonlyargs)
    if arguments.kwarg:
        args.append(f"**{arguments.kwarg.arg}")
    return args


def _is_main_guard(test: ast.AST) -> bool:
    if not isinstance(test, ast.Compare):
        return False
    if len(test.ops) != 1 or not isinstance(test.ops[0], ast.Eq):
        return False
    if not isinstance(test.left, ast.Name) or test.left.id != "__name__":
        return False
    if len(test.comparators) != 1:
        return False
    comparator = test.comparators[0]
    return isinstance(comparator, ast.Constant) and comparator.value == "__main__"


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        owner = _call_name(node.value)
        return f"{owner}.{node.attr}" if owner else node.attr
    return None


def _safe_unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return type(node).__name__

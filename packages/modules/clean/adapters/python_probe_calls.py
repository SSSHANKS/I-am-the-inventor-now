"""Bind statically known probe arguments without executing project declarations."""
from __future__ import annotations

import ast
import inspect


class _DeclaredDefault:
    """Render a default accurately in diagnostics without evaluating it."""
    def __init__(self, expression: ast.expr) -> None:
        self.text = ast.unparse(expression)

    def __repr__(self) -> str:
        return self.text


def call_binding_error(call: ast.Call, declaration: ast.FunctionDef | ast.AsyncFunctionDef,
                       *, bound: bool) -> str | None:
    # Dynamic unpacking requires runtime evidence; do not guess its contents.
    if any(isinstance(arg, ast.Starred) for arg in call.args) or any(
        keyword.arg is None for keyword in call.keywords
    ):
        return None
    decorators = [ast.unparse(item) for item in declaration.decorator_list]
    if any(item not in {'staticmethod', 'classmethod'} for item in decorators):
        return None  # Wrappers may change the effective call signature.
    args = declaration.args
    positional = [*args.posonlyargs, *args.args]
    defaults_start = len(positional) - len(args.defaults)
    parameters = []
    for index, argument in enumerate(positional):
        parameters.append(inspect.Parameter(
            argument.arg,
            inspect.Parameter.POSITIONAL_ONLY if index < len(args.posonlyargs)
            else inspect.Parameter.POSITIONAL_OR_KEYWORD,
            default=(_DeclaredDefault(args.defaults[index - defaults_start])
                     if index >= defaults_start else inspect.Parameter.empty),
        ))
    if bound and 'staticmethod' not in decorators and parameters:
        parameters.pop(0)
    if args.vararg:
        parameters.append(inspect.Parameter(args.vararg.arg, inspect.Parameter.VAR_POSITIONAL))
    for argument, default in zip(args.kwonlyargs, args.kw_defaults):
        parameters.append(inspect.Parameter(argument.arg, inspect.Parameter.KEYWORD_ONLY,
            default=_DeclaredDefault(default) if default is not None else inspect.Parameter.empty))
    if args.kwarg:
        parameters.append(inspect.Parameter(args.kwarg.arg, inspect.Parameter.VAR_KEYWORD))
    try:
        signature = inspect.Signature(parameters)
    except ValueError:
        return None  # Malformed declarations belong to architecture validation.
    names = [keyword.arg for keyword in call.keywords]
    if len(names) != len(set(names)):
        return 'duplicate keyword arguments'
    try:
        signature.bind(*[None for _ in call.args], **{name: None for name in names})
    except TypeError as exc:
        return f'{exc}; declared call signature {signature}'
    return None


def expects_binding_typeerror(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """Leave deliberately invalid calls in explicit TypeError tests to execution."""
    return expects_exception(node, parents, "TypeError")


def expects_exception(
    node: ast.AST,
    parents: dict[ast.AST, ast.AST],
    exception_name: str,
) -> bool:
    """Whether a call is inside an explicit test for the named exception."""
    child = node
    while child in parents:
        parent = parents[child]
        if isinstance(parent, ast.Try) and child in parent.body:
            for handler in parent.handlers:
                kinds = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
                if any(isinstance(kind, ast.Name) and kind.id == exception_name for kind in kinds):
                    return True
        if isinstance(parent, ast.With) and child in parent.body:
            for item in parent.items:
                expr = item.context_expr
                if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute) and (
                    expr.func.attr in {'assertRaises', 'assertRaisesRegex'}
                ) and expr.args and isinstance(expr.args[0], ast.Name) and expr.args[0].id == exception_name:
                    return True
        child = parent
    return False

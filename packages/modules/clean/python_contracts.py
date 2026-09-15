from __future__ import annotations

import ast
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

_CALLABLE_KINDS = frozenset({"function", "decorator"})
_CLASS_KINDS = frozenset({"class", "exception", "protocol"})
_TYPING_NAMES = frozenset(
    {
        "Any",
        "AnyStr",
        "BinaryIO",
        "ClassVar",
        "Final",
        "IO",
        "Literal",
        "Never",
        "NoReturn",
        "Optional",
        "Protocol",
        "Self",
        "TextIO",
        "TypeAlias",
        "TypeGuard",
        "TypeVar",
        "Union",
    }
)
_TYPING_MODULE_ALIASES = frozenset({"typing", "t"})


@dataclass(frozen=True)
class PythonContractIssue:
    message: str
    paths: tuple[str, ...]
    symbol_id: str | None = None
    requirement_ids: tuple[str, ...] = ()
    expected: str | None = None
    actual: str | None = None


def python_contract_declaration_error(contract: dict[str, Any]) -> str | None:
    """Return why a Python contract signature is invalid, or None."""
    name = str(contract["qualified_name"]).rsplit(".", 1)[-1]
    kind = contract["kind"]
    signature = contract["signature"]
    if kind in _CLASS_KINDS:
        class_declaration = _parse_class_declaration_signature(signature)
        if class_declaration is not None:
            if class_declaration.name != name:
                return (
                    f"signature name {class_declaration.name!r} does not match qualified "
                    f"name {name!r}"
                )
            initializer = _stub_initializer(class_declaration)
            if initializer is not None and _annotation(initializer.returns) not in {
                None,
                "None",
            }:
                return "class constructor signatures must return None"
            return None
        declaration = _parse_callable_signature(signature)
        if declaration is None:
            return f"invalid class signature {signature!r}"
        if declaration.name != name:
            return (
                f"signature name {declaration.name!r} does not match qualified "
                f"name {name!r}"
            )
        if _annotation(declaration.returns) not in {None, "None"}:
            return "constructor-style class signatures may only return None"
        return None
    if kind in _CALLABLE_KINDS:
        declaration = _parse_callable_signature(signature)
        if declaration is None:
            return f"invalid callable signature {signature!r}"
        if declaration.name != name:
            return (
                f"signature name {declaration.name!r} does not match qualified "
                f"name {name!r}"
            )
        return None
    if kind == "constant":
        declaration = _parse_constant_signature(signature)
        if declaration is None:
            return f"invalid constant signature {signature!r}; expected 'NAME: type'"
        if declaration.target.id != name:
            return (
                f"signature name {declaration.target.id!r} does not match qualified "
                f"name {name!r}"
            )
    return None


def find_python_contract_issues(
    contents: dict[str, str],
    plan: dict[str, Any],
) -> list[PythonContractIssue]:
    """Check generated provider definitions against authoritative plan contracts."""
    providers = {
        symbol_id: item["path"]
        for item in plan.get("files", [])
        for symbol_id in item.get("provides", [])
    }
    issues: list[PythonContractIssue] = []
    for contract in plan.get("symbol_contracts", []):
        symbol_id = contract["symbol_id"]
        path = providers.get(symbol_id)
        if path is None or path not in contents:
            continue
        declaration_error = python_contract_declaration_error(contract)
        if declaration_error:
            issues.append(
                PythonContractIssue(
                    f"{symbol_id} has {declaration_error}",
                    (path,),
                    symbol_id=symbol_id,
                    requirement_ids=tuple(contract["requirement_ids"]),
                    expected=contract["signature"],
                )
            )
            continue
        tree = ast.parse(contents[path], filename=path)
        name = str(contract["qualified_name"]).rsplit(".", 1)[-1]
        binding = _top_level_bindings(tree).get(name)
        if binding is None:
            issues.append(
                PythonContractIssue(
                    f"{path!r} does not define contracted symbol {name!r} ({symbol_id})",
                    (path,),
                    symbol_id=symbol_id,
                    requirement_ids=tuple(contract["requirement_ids"]),
                    expected=contract["signature"],
                    actual="missing",
                )
            )
            continue
        kind = contract["kind"]
        if not _binding_matches_kind(binding, kind):
            issues.append(
                PythonContractIssue(
                    f"{path!r} defines {name!r} with the wrong kind for {kind} contract {symbol_id}",
                    (path,),
                    symbol_id=symbol_id,
                    requirement_ids=tuple(contract["requirement_ids"]),
                    expected=contract["signature"],
                    actual=type(binding).__name__,
                )
            )
            continue
        mismatch = _signature_mismatch(binding, contract)
        if mismatch:
            issues.append(
                PythonContractIssue(
                    f"{symbol_id} {mismatch}",
                    (path,),
                    symbol_id=symbol_id,
                    requirement_ids=tuple(contract["requirement_ids"]),
                    expected=contract["signature"],
                    actual=_actual_contract_declaration(binding, contract),
                )
            )
    return issues


def _top_level_bindings(tree: ast.Module) -> dict[str, ast.AST]:
    bindings: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bindings[node.name] = node
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            bindings[node.target.id] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                for name in _assigned_names(target):
                    bindings[name] = node
    return bindings


def _binding_matches_kind(binding: ast.AST, kind: str) -> bool:
    if kind in _CALLABLE_KINDS:
        return isinstance(binding, (ast.FunctionDef, ast.AsyncFunctionDef))
    if kind in _CLASS_KINDS:
        return isinstance(binding, ast.ClassDef)
    return kind == "constant" and isinstance(binding, (ast.Assign, ast.AnnAssign))


def _signature_mismatch(binding: ast.AST, contract: dict[str, Any]) -> str | None:
    expected = contract["signature"]
    kind = contract["kind"]
    if kind in _CALLABLE_KINDS:
        declaration = _parse_callable_signature(expected)
        assert declaration is not None
        assert isinstance(binding, (ast.FunctionDef, ast.AsyncFunctionDef))
        if _arguments_key(binding.args) != _arguments_key(declaration.args):
            return f"arguments do not match contracted signature {expected!r}"
        if _annotation(binding.returns) != _annotation(declaration.returns):
            return f"return annotation does not match contracted signature {expected!r}"
    elif kind in _CLASS_KINDS:
        assert isinstance(binding, ast.ClassDef)
        class_declaration = _parse_class_declaration_signature(expected)
        if class_declaration is not None:
            expected_bases = _class_bases_key(class_declaration)
            if expected_bases != ((), ()) and _class_bases_key(binding) != expected_bases:
                return f"base classes do not match contracted declaration {expected!r}"
            expected_initializer = _stub_initializer(class_declaration)
            if expected_initializer is not None:
                actual_arguments, drop_first = _class_arguments(binding)
                if _arguments_key(
                    actual_arguments,
                    drop_first=drop_first,
                ) != _arguments_key(expected_initializer.args, drop_first=True):
                    return (
                        "constructor arguments do not match contracted declaration "
                        f"{expected!r}"
                    )
                if _annotation(expected_initializer.returns) != _initializer_return(binding):
                    return (
                        "constructor return annotation does not match contracted "
                        f"declaration {expected!r}"
                    )
            method_mismatch = _class_method_mismatch(binding, class_declaration)
            if method_mismatch:
                return f"{method_mismatch} in contracted declaration {expected!r}"
            return None
        declaration = _parse_callable_signature(expected)
        assert declaration is not None
        actual_arguments, drop_first = _class_arguments(binding)
        if _arguments_key(actual_arguments, drop_first=drop_first) != _arguments_key(declaration.args):
            return f"constructor arguments do not match contracted signature {expected!r}"
    else:
        declaration = _parse_constant_signature(expected)
        assert declaration is not None
        actual_annotation = binding.annotation if isinstance(binding, ast.AnnAssign) else None
        if _annotation(actual_annotation) != _annotation(declaration.annotation):
            return f"annotation does not match contracted signature {expected!r}"
    return None


def _actual_contract_declaration(
    binding: ast.AST,
    contract: dict[str, Any],
) -> str:
    name = str(contract["qualified_name"]).rsplit(".", 1)[-1]
    if isinstance(binding, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return _render_callable_declaration(name, binding.args, binding.returns)
    if isinstance(binding, ast.ClassDef):
        if _parse_class_declaration_signature(contract["signature"]) is not None:
            bases = [ast.unparse(base) for base in binding.bases]
            bases.extend(
                f"{keyword.arg}={ast.unparse(keyword.value)}"
                for keyword in binding.keywords
                if keyword.arg is not None
            )
            suffix = f"({', '.join(bases)})" if bases else ""
            return f"class {name}{suffix}"
        arguments, drop_first = _class_arguments(binding)
        return _render_callable_declaration(
            name,
            _without_first_argument(arguments) if drop_first else arguments,
            None,
        )
    if isinstance(binding, ast.AnnAssign):
        return f"{name}: {_annotation(binding.annotation)}"
    return f"{name}: unannotated"


def _render_callable_declaration(
    name: str,
    arguments: ast.arguments,
    returns: ast.expr | None,
) -> str:
    declaration = ast.FunctionDef(
        name=name,
        args=deepcopy(arguments),
        body=[ast.Pass()],
        decorator_list=[],
        returns=deepcopy(returns),
        type_comment=None,
    )
    ast.fix_missing_locations(declaration)
    header = ast.unparse(declaration).splitlines()[0]
    return header.removeprefix("def ").removesuffix(":")


def _without_first_argument(arguments: ast.arguments) -> ast.arguments:
    normalized = deepcopy(arguments)
    if normalized.posonlyargs:
        normalized.posonlyargs.pop(0)
    elif normalized.args:
        normalized.args.pop(0)
    return normalized


def _class_method_mismatch(
    actual_class: ast.ClassDef,
    expected_class: ast.ClassDef,
) -> str | None:
    actual_methods = {
        item.name: item
        for item in actual_class.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    expected_methods = {
        item.name: item
        for item in expected_class.body
        if isinstance(item, ast.FunctionDef) and item.name != "__init__"
    }
    for name, expected in expected_methods.items():
        actual = actual_methods.get(name)
        if actual is None:
            return f"class does not define contracted method {name!r}"
        if _arguments_key(actual.args) != _arguments_key(expected.args):
            return f"method {name!r} arguments do not match"
        if _annotation(actual.returns) != _annotation(expected.returns):
            return f"method {name!r} return annotation does not match"
    return None


def _arguments_key(arguments: ast.arguments, *, drop_first: bool = False) -> tuple[Any, ...]:
    positional = [
        ("positional-only", argument)
        for argument in arguments.posonlyargs
    ] + [("positional", argument) for argument in arguments.args]
    default_start = len(positional) - len(arguments.defaults)
    defaults: list[ast.expr | None] = [None] * default_start + list(arguments.defaults)
    entries = [
        (kind, argument.arg, _annotation(argument.annotation), _expression(default))
        for (kind, argument), default in zip(positional, defaults, strict=True)
    ]
    if drop_first and entries:
        entries = entries[1:]
    keyword_only = tuple(
        (
            argument.arg,
            _annotation(argument.annotation),
            _expression(default),
        )
        for argument, default in zip(arguments.kwonlyargs, arguments.kw_defaults, strict=True)
    )
    return (
        tuple(entries),
        _argument(arguments.vararg),
        keyword_only,
        _argument(arguments.kwarg),
    )


def _parse_callable_signature(signature: str) -> ast.FunctionDef | None:
    try:
        tree = ast.parse(f"def {_normalise_signature_markup(signature)}:\n    pass\n")
    except SyntaxError:
        return None
    node = tree.body[0] if len(tree.body) == 1 else None
    return node if isinstance(node, ast.FunctionDef) else None


def _parse_class_declaration_signature(signature: str) -> ast.ClassDef | None:
    normalized = _normalise_signature_markup(signature)
    candidate = re.sub(
        r":\s*(?=def\s+\w+\s*\()",
        "__CLEAN_CLASS_COLON__\n    ",
        normalized.strip(),
        count=1,
    )
    candidate = re.sub(
        r":\s*(?=def\s+\w+\s*\()",
        ":\n        ...\n    ",
        candidate,
    )
    candidate = re.sub(
        r"\.\.\.\s+(?=def\s+\w+\s*\()",
        "...\n    ",
        candidate,
    )
    candidate = candidate.replace("__CLEAN_CLASS_COLON__", ":")
    try:
        tree = ast.parse(candidate)
    except SyntaxError:
        return None
    node = tree.body[0] if len(tree.body) == 1 else None
    if not isinstance(node, ast.ClassDef) or not node.body:
        return None
    if len(node.body) == 1 and _is_class_placeholder(node.body[0]):
        return node
    methods = [item for item in node.body if isinstance(item, ast.FunctionDef)]
    if len(methods) != len(node.body):
        return None
    if len({method.name for method in methods}) != len(methods):
        return None
    return node if all(_is_stub_body(method.body) for method in methods) else None


def _parse_constant_signature(signature: str) -> ast.AnnAssign | None:
    try:
        tree = ast.parse(_normalise_signature_markup(signature))
    except SyntaxError:
        return None
    node = tree.body[0] if len(tree.body) == 1 else None
    if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
        return None
    return node


def _class_bases_key(node: ast.ClassDef) -> tuple[Any, ...]:
    return (
        tuple(ast.unparse(base) for base in node.bases),
        tuple((keyword.arg, ast.unparse(keyword.value)) for keyword in node.keywords),
    )


def _stub_initializer(node: ast.ClassDef) -> ast.FunctionDef | None:
    return next(
        (
            item
            for item in node.body
            if isinstance(item, ast.FunctionDef) and item.name == "__init__"
        ),
        None,
    )


def _is_class_placeholder(statement: ast.stmt) -> bool:
    return isinstance(statement, ast.Pass) or (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Constant)
        and statement.value.value is Ellipsis
    )


def _normalise_signature_markup(signature: str) -> str:
    """Undo narrowly scoped Markdown escaping commonly emitted inside JSON text."""
    normalized = signature.replace("**init**", "__init__")
    return re.sub(r"(?<=\w)\\_(?=\w)", "_", normalized)


def _is_stub_body(body: list[ast.stmt]) -> bool:
    if len(body) != 1:
        return False
    statement = body[0]
    return isinstance(statement, ast.Pass) or (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Constant)
        and statement.value.value is Ellipsis
    )


def _initializer_return(node: ast.ClassDef) -> str | None:
    initializer = next(
        (
            item
            for item in node.body
            if isinstance(item, ast.FunctionDef) and item.name == "__init__"
        ),
        None,
    )
    return _annotation(initializer.returns) if initializer is not None else None


def _empty_arguments() -> ast.arguments:
    return ast.arguments(
        posonlyargs=[],
        args=[],
        vararg=None,
        kwonlyargs=[],
        kw_defaults=[],
        kwarg=None,
        defaults=[],
    )


def _class_arguments(node: ast.ClassDef) -> tuple[ast.arguments, bool]:
    initializer = next(
        (
            item
            for item in node.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            and item.name == "__init__"
        ),
        None,
    )
    if initializer is not None:
        return initializer.args, True
    if not any(_decorator_name(decorator) == "dataclass" for decorator in node.decorator_list):
        return _empty_arguments(), False

    fields = [
        item
        for item in node.body
        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
    ]
    defaults = [item.value for item in fields if item.value is not None]
    return (
        ast.arguments(
            posonlyargs=[],
            args=[
                ast.arg(arg=item.target.id, annotation=item.annotation)
                for item in fields
            ],
            vararg=None,
            kwonlyargs=[],
            kw_defaults=[],
            kwarg=None,
            defaults=defaults,
        ),
        False,
    )


def _decorator_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _assigned_names(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, (ast.Tuple, ast.List)):
        return {name for item in node.elts for name in _assigned_names(item)}
    return set()


def _argument(argument: ast.arg | None) -> tuple[str, str | None] | None:
    if argument is None:
        return None
    return argument.arg, _annotation(argument.annotation)


def _annotation(node: ast.expr | None) -> str | None:
    if node is None:
        return None
    return ast.unparse(_normalise_annotation(node))


def _normalise_annotation(node: ast.expr) -> ast.expr:
    """Canonicalize common equivalent spellings used in generated type hints.

    Contracts are parsed outside the generated module, so they cannot share its
    imports. Treating ``typing.BinaryIO`` and ``BinaryIO`` as different APIs
    creates false failures even though both annotations resolve to the same
    typing object.
    """
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in _TYPING_MODULE_ALIASES
        and node.attr in _TYPING_NAMES
    ):
        return ast.copy_location(ast.Name(id=node.attr, ctx=ast.Load()), node)
    if isinstance(node, ast.Name):
        return ast.Name(id=node.id, ctx=ast.Load())
    if isinstance(node, ast.Subscript):
        return ast.copy_location(
            ast.Subscript(
                value=_normalise_annotation(node.value),
                slice=_normalise_annotation(node.slice),
                ctx=ast.Load(),
            ),
            node,
        )
    if isinstance(node, ast.Tuple):
        return ast.copy_location(
            ast.Tuple(
                elts=[_normalise_annotation(item) for item in node.elts],
                ctx=ast.Load(),
            ),
            node,
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return ast.copy_location(
            ast.BinOp(
                left=_normalise_annotation(node.left),
                op=ast.BitOr(),
                right=_normalise_annotation(node.right),
            ),
            node,
        )
    return node


def _expression(node: ast.expr | None) -> str | None:
    return ast.unparse(node) if node is not None else None

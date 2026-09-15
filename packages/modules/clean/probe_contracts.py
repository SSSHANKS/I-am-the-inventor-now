"""Conservative, contract-based checks for generated probe fixtures and interfaces.

Only statically resolved project instances are checked. This is not a Python type
checker and does not infer a missing API from the original implementation.
"""
from __future__ import annotations

import ast


class ProbeContractError(ValueError):
    """The probe must be corrected without changing production code."""


class ProbeArchitectureError(ProbeContractError):
    """An interface conflict requires specification-grounded architecture review."""


def validate_probe_contracts(code: str, contracts: list[dict]) -> None:
    tree = ast.parse(code)
    classes = {}
    for contract in contracts:
        try:
            declaration = ast.parse(contract.get('declaration', ''))
        except SyntaxError:
            continue  # Architecture validation owns malformed declarations.
        for item in declaration.body:
            if isinstance(item, ast.ClassDef):
                classes[contract['qualified_name']] = item

    aliases = {}
    instances = {}
    separate_members = {}
    for contract in contracts:
        parent, _, member_name = contract['qualified_name'].rpartition('.')
        if parent not in classes:
            continue
        try:
            declaration = ast.parse(contract.get('declaration', ''))
        except SyntaxError:
            continue
        if declaration.body:
            separate_members.setdefault(parent, {})[member_name] = declaration.body[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                aliases[item.asname or item.name.split('.')[0]] = (
                    item.name if item.asname else item.name.split('.')[0]
                )
        elif isinstance(node, ast.ImportFrom):
            for item in node.names:
                aliases[item.asname or item.name] = f'{node.module}.{item.name}'

    def name(node):
        if isinstance(node, ast.Name):
            return aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return name(node.value) + '.' + node.attr
        return ''

    def class_name(node):
        qualified = name(node)
        if qualified in classes:
            return qualified
        # Public re-exports may use a shorter module path; require a unique match.
        matches = [key for key in classes if key.split('.')[0] == qualified.split('.')[0]
                   and key.split('.')[-1] == qualified.split('.')[-1]]
        return matches[0] if len(matches) == 1 else None

    def members(key, visited=None):
        visited = set() if visited is None else visited
        if key in visited:
            return {}
        visited.add(key)
        result = {}
        for base in classes[key].bases:
            matches = [k for k in classes if k.split('.')[-1] == name(base).split('.')[-1]]
            if len(matches) == 1:
                result.update(members(matches[0], visited))
        for member in classes[key].body:
            if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                result[member.name] = member
            elif isinstance(member, ast.AnnAssign) and isinstance(member.target, ast.Name):
                result[member.target.id] = member
            elif isinstance(member, ast.Assign):
                for target in member.targets:
                    if isinstance(target, ast.Name):
                        result[target.id] = member
        result.update(separate_members.get(key, {}))
        return result

    # A probe must observe production behavior, not supply it in an overriding
    # test subclass and then assert that its own implementation ran.
    local_bases = {}
    for _ in range(len(tree.body) + 1):
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                bases = set()
                for base in node.bases:
                    key = class_name(base)
                    if key:
                        bases.add(key)
                    if isinstance(base, ast.Name):
                        bases.update(local_bases.get(base.id, set()))
                local_bases[node.name] = bases
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        declared = {member for key in local_bases.get(node.name, ()) for member in members(key)}
        for statement in node.body:
            assigned = []
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assigned = [statement.name]
            elif isinstance(statement, ast.Assign):
                assigned = [target.id for target in statement.targets if isinstance(target, ast.Name)]
            elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                assigned = [statement.target.id]
            overridden = declared.intersection(assigned)
            if overridden:
                raise ProbeContractError(
                    f'Probe subclass {node.name} replaces declared operation(s): '
                    + ', '.join(sorted(overridden))
                    + '; exercise the real implementation, not behavior supplied by the probe'
                )

    # Fixed point also resolves ordinary instance aliases, without executing code.
    for _ in range(3):
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            key = class_name(value.func) if isinstance(value, ast.Call) else (
                instances.get(value.id) if isinstance(value, ast.Name) else None
            )
            if key:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        instances[target.id] = key

    def owner(node):
        return instances.get(node.id) if isinstance(node, ast.Name) else None

    def method(node):
        if isinstance(node, ast.Attribute) and (key := owner(node.value)):
            member = members(key).get(node.attr)
            if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return member
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and (key := owner(node.value)):
            if node.attr.startswith('_') and node.attr not in members(key):
                raise ProbeContractError(
                    f'Undeclared private probe interface {key}.{node.attr}; use a declared '
                    'public contract, not an invented implementation hook'
                )
        if isinstance(node, ast.Call):
            if name(node.func) in {'setattr', 'getattr', 'hasattr'} and len(node.args) >= 2:
                key = owner(node.args[0])
                attr = node.args[1]
                if key and isinstance(attr, ast.Constant) and isinstance(attr.value, str):
                    if attr.value.startswith('_') and attr.value not in members(key):
                        raise ProbeContractError(f'Undeclared private probe interface {key}.{attr.value}')
            if isinstance(node.func, ast.Attribute) and (key := owner(node.func.value)):
                if node.func.attr not in members(key):
                    if node.func.attr.startswith('_'):
                        raise ProbeContractError(
                            f'Undeclared private probe interface {key}.{node.func.attr}'
                        )
                    raise ProbeArchitectureError(
                        f'Probe calls undeclared interface {key}.{node.func.attr}. Review the '
                        'approved requirement before adding a contract; do not invent an API to satisfy a probe.'
                    )
            signature = method(node.func)
            if signature:
                positional = signature.args.posonlyargs + signature.args.args
                names = [arg.arg for arg in positional]
                defaults = dict(zip(names[len(names) - len(signature.args.defaults):],
                                    signature.args.defaults))
                defaults.update({arg.arg: default for arg, default in
                                 zip(signature.args.kwonlyargs, signature.args.kw_defaults)})
                bound = dict(defaults)
                bound.update(zip(names[1:], node.args))
                bound.update({kw.arg: kw.value for kw in node.keywords if kw.arg})
                weak = bound.get('weak')
                if isinstance(weak, ast.Constant) and weak.value is True and any(
                    isinstance(value, ast.Lambda) for value in bound.values()
                ):
                    raise ProbeContractError(
                        'Temporary lambda passed to a weak-reference contract: keep a named '
                        'callback alive, or explicitly request weak=False for a strong-reference scenario'
                    )
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                signature = method(decorator)
                if signature and isinstance(signature.returns, ast.Constant) and signature.returns.value is None:
                    raise ProbeArchitectureError(
                        f'Decorator {name(decorator)} declares a None return. Review the approved '
                        'decorator requirement and its callable-preserving public contract before generation.'
                    )

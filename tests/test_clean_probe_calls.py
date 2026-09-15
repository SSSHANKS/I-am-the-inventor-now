import ast
from types import SimpleNamespace

import pytest

from packages.modules.clean.adapters.python_probe_calls import call_binding_error
from packages.modules.clean.probe_contracts import validate_probe_contracts, ProbeContractError, ProbeArchitectureError
from packages.modules.clean.runner import CleanRunner, _build_succeeded
from packages.modules.clean.behavior import LocalPythonBehaviorProbeExecutor
from packages.modules.clean.workspace import CleanWorkspace


@pytest.mark.parametrize('declaration,call,bound,invalid', [
    ('def f(self, sender=None, weak=True): ...', 'f(target=callback)', True, True),
    ('def f(self, sender=None, weak=True): ...', 'f(sender="s", weak=False)', True, False),
    ('def f(a, /, b, *, c): ...', 'f(1, 2, c=3)', False, False),
    ('def f(a, /, b, *, c): ...', 'f(a=1, b=2, c=3)', False, True),
    ('def f(a, /, b, *, c): ...', 'f(1, 2)', False, True),
    ('def f(a, *, b): ...', 'f(1, 2)', False, True),
    ('def f(a): ...', 'f(1, a=2)', False, True),
    ('def f(a): ...', 'f()', False, True),
    ('def f(a): ...', 'f(1, 2)', False, True),
    ('def f(a=None, *args, **kwargs): ...', 'f(1, 2, other=3)', False, False),
    ('def f(a): ...', 'f(*dynamic)', False, False),
    ('def f(a): ...', 'f(**dynamic)', False, False),
    ('@staticmethod\ndef f(value): ...', 'f(1)', True, False),
    ('@classmethod\ndef f(cls, value): ...', 'f(1)', True, False),
    ('async def f(self, value): ...', 'f()', True, True),
])
def test_binding_matches_python_parameter_kinds(declaration, call, bound, invalid):
    error = call_binding_error(ast.parse(call).body[0].value, ast.parse(declaration).body[0], bound=bound)
    assert (error is not None) == invalid


def test_diagnostic_preserves_defaults_without_evaluating_them():
    signature = ast.parse('def f(self, sender=..., weak=True, factory=side_effect()): ...').body[0]
    error = call_binding_error(ast.parse('f(target=callback)').body[0].value, signature, bound=True)
    assert 'sender=Ellipsis' in error or 'sender=...' in error
    assert 'weak=True' in error
    assert 'factory=side_effect()' in error


CONTRACT = {'qualified_name': 'sample.Emitter', 'kind': 'class', 'declaration': '''class Emitter:
    def __init__(self, doc=None): ...
    def connect(self, sender=None, weak=True): ...
'''}
PREFIX = 'from sample import Emitter as E\ne = E()\n'


@pytest.mark.parametrize('code', ['e.connect(target=callback)', 'alias = e\nalias.connect(1, 2, 3)',
                                  'E(unexpected=True)'])
def test_invalid_calls_are_probe_errors_not_architecture_repair_requests(code):
    with pytest.raises(ProbeContractError, match='incompatible') as error:
        validate_probe_contracts(PREFIX + code, [CONTRACT])
    assert not isinstance(error.value, ProbeArchitectureError)


def test_decorator_factory_protocol_is_preserved():
    validate_probe_contracts(PREFIX + 'e.connect(sender="s")(callback)', [CONTRACT])


@pytest.mark.parametrize('declaration', ['load(path)', 'load(path: str) -> dict', 'def load(path): ...'])
def test_free_function_aliases_are_checked(declaration):
    contract = {'qualified_name': 'sample.load', 'kind': 'function', 'declaration': declaration}
    with pytest.raises(ProbeContractError, match='incompatible'):
        validate_probe_contracts('from sample import load as read\nread(unexpected=1)', [contract])


def test_deliberate_typeerror_probe_is_not_rejected():
    validate_probe_contracts(PREFIX + '''
try:
    e.connect(target=callback)
    assert False
except TypeError:
    pass
''', [CONTRACT])


ASYNC_ERROR_CONTRACT = {
    'qualified_name': 'sample.Emitter',
    'kind': 'class',
    'declaration': '''class Emitter:
    def __init__(self): ...
    def connect(self, handler: callable): ...
    def emit(self, *args): ...
    async def emit_async(self, *args): ...
''',
    'behavior_rules': [{
        'aspect': 'errors',
        'statement': 'Synchronous dispatch against coroutine callables raises RuntimeError.',
        'requirement_ids': ['EH-001'],
        'source': 'specification',
    }],
}


def test_probe_cannot_ignore_explicit_sync_async_error_contract():
    code = '''from sample import Emitter
emitter = Emitter()
async def callback(value): return value
emitter.connect(callback)
assert emitter.emit(1) == [1]
'''
    with pytest.raises(ProbeContractError, match='requires RuntimeError'):
        validate_probe_contracts(code, [ASYNC_ERROR_CONTRACT])


def test_sync_async_error_contract_is_enforced_when_combined_with_results_rule():
    contract = {
        **ASYNC_ERROR_CONTRACT,
        'behavior_rules': [{
            **ASYNC_ERROR_CONTRACT['behavior_rules'][0],
            'aspect': 'results',
            'statement': (
                'Dispatch gathers results, and synchronous dispatch against coroutine '
                'callables raises RuntimeError.'
            ),
        }],
    }
    code = '''from sample import Emitter
emitter = Emitter()
async def callback(value): return value
emitter.connect(callback)
assert emitter.emit(1) == [1]
'''
    with pytest.raises(ProbeContractError, match='requires RuntimeError'):
        validate_probe_contracts(code, [contract])


def test_probe_can_assert_explicit_sync_async_error_contract():
    code = '''from sample import Emitter
emitter = Emitter()
async def callback(value): return value
emitter.connect(callback)
try:
    emitter.emit(1)
    assert False
except RuntimeError:
    pass
'''
    validate_probe_contracts(code, [ASYNC_ERROR_CONTRACT])


def test_probe_can_use_declared_async_counterpart():
    code = '''from sample import Emitter
emitter = Emitter()
async def callback(value): return value
emitter.connect(callback)
async def verify():
    assert await emitter.emit_async(1) == [1]
'''
    validate_probe_contracts(code, [ASYNC_ERROR_CONTRACT])


def test_invalid_probe_does_not_become_a_production_repair(tmp_path):
    from test_clean_behavior import _architecture, _manifest, _suite
    architecture = _architecture()
    architecture['contracts'][0].update(kind='function', declaration='greeting() -> str')
    suite = _suite()
    suite['probes'][1]['code'] = 'from greeting import greeting\nassert greeting(unexpected=1) == "hello"'
    runner = CleanRunner.__new__(CleanRunner)
    runner.max_repairs = 0
    runner.behavior_prober = SimpleNamespace(design=lambda *a, **kw: suite)
    retained = runner._design_behavior_probes('approved specification', architecture, _manifest())
    assert len(retained['probes']) == 1
    workspace = CleanWorkspace(tmp_path/'output')
    workspace.write_generated_file('greeting.py', 'def greeting(): return "hello"\n')
    checks = LocalPythonBehaviorProbeExecutor().validate(workspace.project_root, architecture, _manifest(), retained)
    assert all(check['status'] != 'fail' for check in checks)
    assert any(check['name'] == 'python-behavior-coverage' and check['status'] == 'skipped' for check in checks)
    assert not _build_succeeded([*checks, {'name': 'executable-validation', 'status': 'pass'}])

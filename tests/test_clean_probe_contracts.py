from types import SimpleNamespace

import pytest

from packages.modules.clean.probe_contracts import (
    ProbeArchitectureError, ProbeContractError, validate_probe_contracts,
)
from packages.modules.clean.runner import CleanRunner


CONTRACTS = [{
    'qualified_name': 'sample.core.Publisher',
    'declaration': '''class Publisher:
    def attach(self, callback, channel=None, weak=True) -> None: ...
    def dispatch(self) -> list: ...
    def _declared_hook(self): ...
''',
}]
PREFIX = 'from sample import Publisher as P\np = P()\n'


@pytest.mark.parametrize('code', [
    'p._invented = lambda: None',
    'alias = p\nalias._invented()',
    "setattr(p, '_invented', lambda: None)",
    "getattr(p, '_invented', None)",
    "hasattr(p, '_invented')",
])
def test_undeclared_private_interfaces_are_probe_errors(code):
    with pytest.raises(ProbeContractError, match='Undeclared private') as error:
        validate_probe_contracts(PREFIX + code, CONTRACTS)
    assert not isinstance(error.value, ProbeArchitectureError)


@pytest.mark.parametrize('code', [
    'p.attach(lambda: 1)',
    'p.attach(callback=lambda: 1)',
    'p.attach(lambda: 1, None, True)',
])
def test_temporary_weak_callback_is_a_fixture_error(code):
    with pytest.raises(ProbeContractError, match='Temporary lambda'):
        validate_probe_contracts(PREFIX + code, CONTRACTS)


@pytest.mark.parametrize('code', [
    'p.attach(lambda: 1, weak=False)',
    'p.attach(lambda: 1, None, False)',
    'callback = lambda: 1\np.attach(callback)',
    'p._declared_hook()',
    'fixture = object()\ngetattr(fixture, "_fixture_attribute", None)',
])
def test_declared_interfaces_and_live_fixtures_are_allowed(code):
    validate_probe_contracts(PREFIX + code, CONTRACTS)


def test_missing_public_operation_requires_architecture_review():
    with pytest.raises(ProbeArchitectureError, match='undeclared interface'):
        validate_probe_contracts(PREFIX + 'p.observe_changes()', CONTRACTS)


def test_none_return_decorator_requires_architecture_review():
    with pytest.raises(ProbeArchitectureError, match='None return'):
        validate_probe_contracts(PREFIX + '@p.attach\ndef callback(): pass', CONTRACTS)


def test_callable_return_decorator_is_allowed():
    contracts = [{**CONTRACTS[0], 'declaration': CONTRACTS[0]['declaration'].replace(
        'weak=True) -> None', 'weak=True) -> Callable'
    )}]
    validate_probe_contracts(PREFIX + '@p.attach\ndef callback(): pass', contracts)


def test_inherited_contract_methods_are_supported():
    contracts = [*CONTRACTS, {
        'qualified_name': 'sample.core.NamedPublisher',
        'declaration': 'class NamedPublisher(Publisher):\n    pass',
    }]
    validate_probe_contracts('from sample.core import NamedPublisher\np = NamedPublisher()\n'
                            'p.dispatch()', contracts)


def test_separately_declared_method_contract_is_supported():
    contracts = [*CONTRACTS, {
        'qualified_name': 'sample.core.Publisher.observe',
        'declaration': 'def observe(self): ...',
    }]
    validate_probe_contracts(PREFIX + 'p.observe()', contracts)


def test_probe_designer_routes_exhausted_interface_conflict_to_architect():
    from packages.modules.clean.behavior import validate_behavior_probe_suite
    from test_clean_behavior import _architecture, _suite

    architecture = _architecture()
    architecture['contracts'] = [{**architecture['contracts'][0], **CONTRACTS[0]}]
    suite = _suite(PREFIX + '@p.attach\ndef handler(): return 1\nassert p.dispatch() == [1]')
    runner = CleanRunner.__new__(CleanRunner)
    runner.max_repairs = 0
    runner.behavior_prober = SimpleNamespace(design=lambda *args, **kwargs: suite)
    with pytest.raises(ProbeArchitectureError, match='PROBE-001.*FR-001'):
        validate_behavior_probe_suite(suite, architecture)
    with pytest.raises(ProbeArchitectureError):
        runner._design_behavior_probes('approved specification', architecture, {})


def _runner_for_review(monkeypatch, max_repairs, conflict_count):
    runner = CleanRunner.__new__(CleanRunner)
    runner.max_repairs = max_repairs
    runner.readiness_executor = None
    runner.behavior_prober = object()
    events = []
    old, new = {'version': 'old'}, {'version': 'new'}
    monkeypatch.setattr('packages.modules.clean.runner.select_runtime_adapter', lambda *a, **k: 'adapter')
    def manifest(architecture, **kwargs):
        events.append(('manifest', architecture['version']))
        return {'version': architecture['version']}
    def probes(spec, architecture, manifest):
        nonlocal conflict_count
        events.append(('probes', architecture['version']))
        if conflict_count:
            conflict_count -= 1
            raise ProbeArchitectureError('missing public operation')
        return {'probes': []}
    def revise(spec, architecture, diagnostic, **kwargs):
        events.append(('review', diagnostic))
        assert spec == 'approved spec'
        return new
    runner._design_manifest = manifest
    runner._design_behavior_probes = probes
    runner.architect = SimpleNamespace(revise=revise)
    def validate_architecture(*args, candidate, **kwargs):
        events.append(('validate', candidate['version']))
        return candidate
    runner._design_architecture = validate_architecture
    return runner, old, new, events


def test_architecture_review_revalidates_and_regenerates_dependent_artifacts(monkeypatch):
    runner, old, new, events = _runner_for_review(monkeypatch, 1, 1)
    result = runner._design_probe_consistent_project(
        'approved spec', old, compatibility_mode='renamed', validation_policy={},
    )
    assert result == (new, {'version': 'new'}, 'adapter', {'probes': []}, None)
    assert [item[0] for item in events] == ['manifest', 'probes', 'review', 'validate', 'manifest', 'probes']
    assert 'specification' in events[2][1]


def test_architecture_review_exhaustion_preserves_degraded_validation(monkeypatch):
    runner, old, new, events = _runner_for_review(monkeypatch, 1, 2)
    result = runner._design_probe_consistent_project(
        'approved spec', old, compatibility_mode='renamed', validation_policy={},
    )
    assert result[3] is None
    assert 'review exhausted' in result[4]
    assert sum(item[0] == 'review' for item in events) == 1


def test_zero_budget_does_not_call_architect(monkeypatch):
    runner, old, new, events = _runner_for_review(monkeypatch, 0, 1)
    result = runner._design_probe_consistent_project(
        'approved spec', old, compatibility_mode='renamed', validation_policy={},
    )
    assert result[0] is old
    assert result[3] is None
    assert not any(item[0] == 'review' for item in events)


@pytest.mark.parametrize('body', [
    'class Observed(P):\n    def attach(self, *args, **kwargs):\n        super().attach(*args, **kwargs)\n        hook()',
    'class Observed(P):\n    async def dispatch(self): return [42]',
    'class Observed(P):\n    dispatch = lambda self: [42]',
    'class Base(P): pass\nclass Observed(Base):\n    def dispatch(self): return [42]',
])
def test_subclass_cannot_supply_the_declared_behavior(body):
    with pytest.raises(ProbeContractError, match='replaces declared operation') as error:
        validate_probe_contracts(PREFIX + body, CONTRACTS)
    assert not isinstance(error.value, ProbeArchitectureError)


def test_unrelated_callback_classes_and_non_overriding_subclasses_are_allowed():
    validate_probe_contracts(PREFIX + '''
class Callback:
    def attach(self): return 42
class Child(P):
    def describe_fixture(self): return "fixture"
''', CONTRACTS)


@pytest.mark.parametrize("kind", ["protocol", "class"])
def test_probe_may_implement_declared_observer_collaborator(kind):
    contracts = [{
        "qualified_name": "sample.core.EmitterObserver",
        "kind": kind,
        "declaration": (
            "class EmitterObserver:\n"
            "    def update(self, event: str) -> None: ..."
        ),
        "behavior_rules": [{
            "statement": "Lifecycle notifications are delivered to registered observers."
        }],
    }]
    code = (
        "from sample.core import EmitterObserver\n"
        "class CapturingObserver(EmitterObserver):\n"
        "    def update(self, event: str) -> None:\n"
        "        self.event = event\n"
    )

    validate_probe_contracts(code, contracts)

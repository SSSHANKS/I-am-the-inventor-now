from types import SimpleNamespace

import pytest

from packages.modules.clean.adapters import select_runtime_adapter, GenericRuntimeAdapter, PythonRuntimeAdapter
from packages.modules.clean.runner import CleanRunner, CleanBuildError


@pytest.mark.parametrize('language', ['Rust', 'JavaScript', 'TypeScript', 'Go', 'Java', 'C++', 'Python/Rust', 'not-python'])
def test_unsupported_language_never_receives_python_execution(language):
    adapter = select_runtime_adapter({'project_profile': {'language': language}})
    assert isinstance(adapter, GenericRuntimeAdapter)
    assert adapter.configure_execution(timeout_seconds=1, agent_options={}) == (None, None)
    assert not adapter.supports_behavior_probes
    checks = adapter.validate_readiness(None, {}, {})
    assert checks[0]['status'] == 'skipped'


@pytest.mark.parametrize('language', ['Python', 'python3', 'Python 3.12'])
def test_python_toolchain_is_constructed_by_selected_adapter(monkeypatch, language):
    created = []
    monkeypatch.setattr('packages.agents.clean_team.CleanBehaviorProbeAgent',
                        lambda **options: created.append(options) or 'prober')
    adapter = select_runtime_adapter({'project_profile': {'language': language}})
    assert isinstance(adapter, PythonRuntimeAdapter)
    assert adapter.readiness_executor is None
    prober, executor = adapter.configure_execution(timeout_seconds=3, agent_options={'model': 'stub'})
    assert prober == 'prober'
    assert executor.timeout_seconds == 3
    assert adapter.readiness_executor.timeout_seconds == 3
    assert created == [{'model': 'stub'}]


def runner(configurator):
    result = CleanRunner.__new__(CleanRunner)
    result.max_repairs = 0
    result.readiness_executor = None
    result.behavior_prober = None
    result.execution_configurator = configurator
    result._design_manifest = lambda *args, **kwargs: {'files': []}
    result._design_behavior_probes = lambda *args: pytest.fail('Python prober must not run')
    return result


def test_configuration_happens_after_language_selection_and_skips_probe_design():
    selected = []
    def configure(adapter):
        selected.append(adapter.adapter_id)
        return adapter.configure_execution(timeout_seconds=3, agent_options={})
    result = runner(configure)._design_probe_consistent_project(
        'approved', {'project_profile': {'language': 'Rust'}},
        compatibility_mode='renamed', validation_policy={},
    )
    assert selected == ['generic-structure']
    assert result[3] is None


@pytest.mark.parametrize('tools', [(object(), None),
    (SimpleNamespace(source_reader=object()), object())])
def test_invalid_or_source_aware_adapter_tools_are_rejected(tools):
    with pytest.raises(CleanBuildError):
        runner(lambda adapter: tools)._design_probe_consistent_project(
            'approved', {'project_profile': {'language': 'Python'}},
            compatibility_mode='renamed', validation_policy={},
        )

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from packages.modules.clean.adapters import PythonRuntimeAdapter
from packages.modules.clean.manifest import validate_manifest, CleanManifestError
from packages.modules.clean.runner import CleanRunner, CleanBuildError
from packages.modules.clean.workspace import WorkspaceError
from packages.modules.handoff import export_clean_handoff
from test_clean_manifest import _architecture, _manifest


def missing_provider():
    architecture = _architecture()
    manifest = _manifest(architecture)
    manifest['files'][0]['provides'] = []
    return architecture, manifest


@pytest.mark.parametrize('layout,path', [('src', 'src/greeting.py'), ('flat', 'greeting.py'),
                                         ('src', 'src/greeting/__init__.py')])
def test_missing_claim_recovered_only_on_exact_owned_module(layout, path):
    architecture, manifest = missing_provider()
    architecture['project_profile']['layout'] = layout
    manifest['files'][0]['path'] = path
    original = deepcopy(manifest)
    recovered = PythonRuntimeAdapter().normalise_manifest(architecture, manifest)
    assert recovered['files'][0]['provides'] == ['SYM-001']
    assert manifest == original
    assert PythonRuntimeAdapter().normalise_manifest(architecture, recovered) == recovered


@pytest.mark.parametrize('change', ['wrong-owner', 'wrong-module', 'documentation', 'ambiguous'])
def test_recovery_does_not_guess_or_change_ownership(change):
    architecture, manifest = missing_provider()
    task = manifest['files'][0]
    if change == 'wrong-owner':
        task['component_id'] = 'CMP-999'
    elif change == 'wrong-module':
        task['path'] = 'src/unrelated.py'
    elif change == 'documentation':
        task['category'] = 'documentation'
    else:
        duplicate = deepcopy(task)
        duplicate['path'] = 'src/greeting/__init__.py'
        duplicate['generation_order'] = 3
        manifest['files'].append(duplicate)
    result = PythonRuntimeAdapter().normalise_manifest(architecture, manifest)
    assert not any('SYM-001' in task['provides'] for task in result['files'])


def test_metadata_claim_is_not_retained_but_existing_source_can_recover_it():
    architecture, manifest = missing_provider()
    manifest['files'][1]['provides'] = ['SYM-001']
    result = PythonRuntimeAdapter().normalise_manifest(architecture, manifest)
    assert result['files'][0]['provides'] == ['SYM-001']
    assert result['files'][1]['provides'] == []
    validate_manifest(result, architecture)


def test_existing_claim_is_not_duplicated_elsewhere():
    architecture, manifest = missing_provider()
    duplicate = deepcopy(manifest['files'][0])
    duplicate.update(path='src/other.py', provides=['SYM-001'], generation_order=3)
    manifest['files'].append(duplicate)
    result = PythonRuntimeAdapter().normalise_manifest(architecture, manifest)
    assert sum('SYM-001' in task['provides'] for task in result['files']) == 1


def test_missing_provider_diagnostic_is_actionable():
    architecture, manifest = missing_provider()
    with pytest.raises(CleanManifestError) as error:
        validate_manifest(manifest, architecture)
    message = str(error.value)
    for expected in ['SYM-001', 'greeting.greet', 'CMP-001', 'greet(name: str) -> str',
                     'src/greeting.py', 'Metadata', 'Do not delete architecture contracts']:
        assert expected in message


def test_planning_failure_saves_candidates_without_generating_code(tmp_path):
    architecture, manifest = missing_provider()
    manifest['files'][0]['path'] = 'src/unrelated.py'
    manifest['entry_points'][0]['path'] = 'src/unrelated.py'
    for task in manifest['files']:
        task['depends_on'] = ['src/unrelated.py' if path == 'src/greeting.py' else path
                              for path in task['depends_on']]
    runner = CleanRunner.__new__(CleanRunner)
    runner.architect = object()
    runner.syntax_checks = True
    runner.max_repairs = 1
    runner.readiness_executor = None
    runner.behavior_prober = None
    runner._design_architecture = lambda *args, **kwargs: architecture
    runner.manifest_designer = SimpleNamespace(
        derive=lambda *args, **kwargs: deepcopy(manifest),
        revise=lambda *args, **kwargs: deepcopy(manifest),
    )
    handoff = export_clean_handoff(tmp_path / 'handoff',
        '# Requirements\n- FR-001: Return a greeting.\n- TC-001: Check a greeting.', {'status': 'pass'})
    output = tmp_path / 'output'
    with pytest.raises(CleanBuildError, match='planning diagnostics:'):
        runner.run(handoff, output)
    saved = json.loads((output / '_clean/planning_failure.json').read_text())
    assert saved['status'] == 'planning-failed'
    assert saved['stage'] == 'manifest'
    assert saved['architecture'] == architecture
    assert saved['manifest']['files'][0]['provides'] == []
    assert len(saved['attempts']) == 2
    assert all('SYM-001' in attempt['diagnostic'] for attempt in saved['attempts'])
    assert not list((output / 'project').iterdir())
    assert not (output / '_clean/clean_build_report.json').exists()
    previous = (output / '_clean/planning_failure.json').read_bytes()
    with pytest.raises(WorkspaceError, match='empty'):
        runner.run(handoff, output)
    assert (output / '_clean/planning_failure.json').read_bytes() == previous

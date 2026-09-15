import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from packages.modules.clean.runner import CleanRunner, CleanBuildError
from packages.modules.handoff import export_clean_handoff
from test_clean_behavior_rules import architecture_with_rule
from test_clean_architecture import SPECIFICATION


@pytest.mark.parametrize('failure', ['validation', 'schema', 'revision'])
def test_architecture_failure_saves_candidates_and_attempts(tmp_path, failure):
    first = architecture_with_rule(statement='unsupported first statement')
    second = architecture_with_rule(statement='unsupported revised statement')
    if failure == 'schema':
        first['unknown_field'] = 'preserved despite schema rejection'
        second['unknown_field'] = 'revised but still invalid'
    runner = CleanRunner.__new__(CleanRunner)
    runner.max_repairs = 1
    runner.syntax_checks = True
    def revise(*args, **kwargs):
        if failure == 'revision': raise ValueError('stub revision failed')
        return deepcopy(second)
    runner.architect = SimpleNamespace(design=lambda *a, **kw: deepcopy(first), revise=revise)
    runner.manifest_designer = object()  # Must never be called.
    handoff = export_clean_handoff(tmp_path/'handoff', SPECIFICATION, {'status': 'pass'})
    output = tmp_path/'output'
    with pytest.raises(CleanBuildError, match='planning diagnostics:'):
        runner.run(handoff, output)
    saved = json.loads((output/'_clean/planning_failure.json').read_text())
    assert saved['stage'] == 'architecture'
    assert saved['architecture'] is not None
    assert saved['manifest'] is None
    attempts = saved['attempts']
    assert len(attempts) == (1 if failure == 'revision' else 2)
    assert attempts[0]['input_to_validation'] == first
    if failure != 'revision': assert attempts[1]['input_to_validation'] == second
    assert all(attempt['diagnostic'] for attempt in attempts)
    assert saved['architecture']['contracts'][0]['behavior_rules'][0]['statement'] == (
        'unsupported first statement' if failure == 'revision' else 'unsupported revised statement'
    )
    assert not list((output/'project').iterdir())
    assert not (output/'_clean/clean_build_report.json').exists()

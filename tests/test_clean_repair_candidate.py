import pytest

from packages.modules.clean.repair_candidate import (
    evaluate_repair_candidate,
    repair_improvements,
    repair_regressions,
    select_non_regressing_repair_subset,
)
from packages.modules.clean.workspace import CleanWorkspace


def check(name, status, paths=None, ids=()):
    return {'name': name, 'status': status, 'message': 'Fixture validation result', 'paths': paths or [],
            'diagnostics': [{'check_id': identity} for identity in ids]}


@pytest.mark.parametrize('status', ['fail', 'skipped'])
def test_previously_passing_gate_must_stay_passing(status):
    assert repair_regressions([check('contracts', 'pass')], [check('contracts', status)])


def test_previously_passing_gate_cannot_disappear():
    assert repair_regressions([check('syntax', 'pass')], [])


def test_newly_exposed_failure_after_unblocking_is_not_a_regression():
    before = [check('syntax', 'fail'), check('imports', 'skipped')]
    after = [check('syntax', 'pass'), check('imports', 'fail')]
    assert repair_regressions(before, after) == []


def test_unresolved_failure_with_changed_content_can_continue():
    assert repair_regressions([check('syntax', 'fail')], [check('syntax', 'fail')]) == []


def test_progress_requires_a_stable_failure_obligation_to_be_resolved():
    before = [check('behavior-probes', 'fail', ids=['probe.a', 'probe.b'])]
    assert repair_improvements(before, [check('behavior-probes', 'fail', ids=['probe.a'])]) == ['probe.b']
    assert repair_improvements(before, [check('behavior-probes', 'fail', ids=['probe.a', 'probe.b'])]) == []
    assert repair_improvements([check('syntax', 'fail')], [check('syntax', 'pass')]) == ['syntax:']


def test_candidate_that_changes_content_without_resolving_a_failure_is_rejected(tmp_path):
    workspace = CleanWorkspace(tmp_path/'output')
    workspace.write_generated_file('main.rs', 'original')
    _, rejections = evaluate_repair_candidate(
        workspace, [{'path': 'main.rs', 'content': 'different'}], {'main.rs'},
        [check('compile', 'fail')], lambda _: [check('compile', 'fail')],
    )
    assert any('does not resolve any' in item for item in rejections)


def test_passing_path_is_protected_inside_mixed_gate():
    before = [check('compile', 'pass', ['a.rs']), check('compile', 'fail', ['b.rs'])]
    after = [check('compile', 'fail', ['a.rs']), check('compile', 'pass', ['b.rs'])]
    assert repair_regressions(before, after)


def test_all_passing_paths_cannot_hide_a_disappeared_check():
    before = [check('compile', 'pass', ['a.rs']), check('compile', 'pass', ['b.rs'])]
    assert repair_regressions(before, [check('compile', 'pass', ['a.rs'])])


@pytest.mark.parametrize('after', [[], [check('example-behavior-probes', 'skipped')],
                                  [check('example-behavior-probes', 'fail')]])
def test_executed_mixed_probe_suite_cannot_lose_validation(after):
    before = [check('example-behavior-probes', 'fail', ids=['probe.a'])]
    assert repair_regressions(before, after)


def test_executed_mixed_probe_suite_can_be_fully_repaired():
    before = [check('example-behavior-probes', 'fail', ids=['probe.a'])]
    assert repair_regressions(before, [check('example-behavior-probes', 'pass')]) == []


def test_failed_probe_count_alone_cannot_hide_a_regression():
    before = [check('example-behavior-probes', 'fail', ids=['probe.a', 'probe.b'])]
    after = [check('example-behavior-probes', 'fail', ids=['probe.c'])]
    assert 'probe.c' in repair_regressions(before, after)[0]
    assert repair_regressions(before, [check('example-behavior-probes', 'fail', ids=['probe.a'])]) == []


@pytest.mark.parametrize('status', ['skipped', 'fail'])
def test_unexecuted_or_unclassified_suite_does_not_imply_passed_probes(status):
    before = [check('example-behavior-probes', status)]
    after = [check('example-behavior-probes', 'fail', ids=['probe.a'])]
    assert repair_regressions(before, after) == []


@pytest.mark.parametrize('result', ['pass', 'fail', 'exception', 'mutation'])
def test_candidate_validation_never_changes_working_project(tmp_path, result):
    workspace = CleanWorkspace(tmp_path/'output')
    workspace.write_generated_file('main.rs', 'original')
    seen = []
    def validate(candidate):
        seen.append(candidate.root)
        assert candidate.read_generated_file('main.rs') == 'candidate'
        assert workspace.read_generated_file('main.rs') == 'original'
        if result == 'exception': raise ValueError('validator failed')
        if result == 'mutation': candidate.write_generated_file('extra.rs', 'unexpected')
        return [check('compile', 'fail' if result == 'fail' else 'pass')]
    def evaluate():
        return evaluate_repair_candidate(workspace, [{'path': 'main.rs', 'content': 'candidate'}],
                                         {'main.rs'}, [check('compile', 'pass')], validate)
    if result == 'exception':
        with pytest.raises(ValueError): evaluate()
    else:
        checks, regressions = evaluate()
        assert bool(regressions) == (result in {'fail', 'mutation'})
    assert workspace.read_generated_file('main.rs') == 'original'
    assert workspace.list_generated_files() == ['main.rs']
    assert not seen[0].exists()


def test_staged_missing_file_does_not_leak_to_working_project(tmp_path):
    workspace = CleanWorkspace(tmp_path/'output')
    evaluate_repair_candidate(workspace, [{'path': 'new.go', 'content': 'candidate'}], {'new.go'}, [], lambda _: [])
    assert workspace.list_generated_files() == []


def test_safe_file_is_retained_from_a_mixed_repair_batch(tmp_path):
    workspace = CleanWorkspace(tmp_path / "output")
    workspace.write_generated_files([
        {"path": "good.py", "content": "wrong"},
        {"path": "bad.py", "content": "stable"},
    ])
    before = [check("behavior-probes", "fail", ids=["probe.good"])]

    def validate(candidate):
        if candidate.read_generated_file("bad.py") != "stable":
            return [check("contracts", "fail")]
        if candidate.read_generated_file("good.py") == "fixed":
            return [check("behavior-probes", "pass")]
        return before

    accepted, checks = select_non_regressing_repair_subset(
        workspace,
        [
            {"path": "bad.py", "content": "regression"},
            {"path": "good.py", "content": "fixed"},
        ],
        {"bad.py", "good.py"},
        before,
        validate,
    )

    assert accepted == [{"path": "good.py", "content": "fixed"}]
    assert checks == [check("behavior-probes", "pass")]
    assert workspace.read_generated_file("good.py") == "wrong"


def test_correlated_safe_files_are_retained_from_a_mixed_repair_batch(tmp_path):
    workspace = CleanWorkspace(tmp_path / "output")
    workspace.write_generated_files([
        {"path": "api.py", "content": "old-api"},
        {"path": "model.py", "content": "old-model"},
        {"path": "unrelated.py", "content": "stable"},
    ])
    before = [check("behavior-probes", "fail", ids=["probe.api"])]

    def validate(candidate):
        api = candidate.read_generated_file("api.py")
        model = candidate.read_generated_file("model.py")
        unrelated = candidate.read_generated_file("unrelated.py")
        if unrelated != "stable" or (api == "new-api") != (model == "new-model"):
            return [check("contracts", "fail")]
        if api == "new-api":
            return [check("behavior-probes", "pass")]
        return before

    accepted, checks = select_non_regressing_repair_subset(
        workspace,
        [
            {"path": "api.py", "content": "new-api"},
            {"path": "model.py", "content": "new-model"},
            {"path": "unrelated.py", "content": "regression"},
        ],
        {"api.py", "model.py", "unrelated.py"},
        before,
        validate,
    )

    assert {item["path"] for item in accepted} == {"api.py", "model.py"}
    assert checks == [check("behavior-probes", "pass")]


def test_fewer_diagnostics_is_progress_when_check_ids_are_aggregate():
    before = [{
        "name": "python-contracts",
        "status": "fail",
        "paths": ["module.py"],
        "diagnostics": [
            {"check_id": "python.contract.declaration", "message": "return"},
            {"check_id": "python.contract.declaration", "message": "arguments"},
        ],
    }]
    after = [{
        "name": "python-contracts",
        "status": "fail",
        "paths": ["module.py"],
        "diagnostics": [
            {"check_id": "python.contract.declaration", "message": "arguments"},
        ],
    }]

    assert repair_improvements(before, after) == [
        "python-contracts:1-fewer-diagnostics"
    ]


def test_equal_aggregate_diagnostic_count_is_not_progress():
    before = [{
        "name": "python-contracts",
        "status": "fail",
        "paths": ["module.py"],
        "diagnostics": [
            {"check_id": "python.contract.declaration", "message": "return"},
        ],
    }]
    after = [{
        "name": "python-contracts",
        "status": "fail",
        "paths": ["module.py"],
        "diagnostics": [
            {"check_id": "python.contract.declaration", "message": "arguments"},
        ],
    }]

    assert repair_improvements(before, after) == []


@pytest.mark.parametrize('budget', [1, 2])
def test_runner_keeps_last_good_files_and_explains_rejection_to_next_attempt(tmp_path, budget):
    import json
    from packages.agents.clean_team import CleanPlannerAgent, CleanBuilderAgent
    from packages.modules.clean.runner import CleanRunner, _failed_check
    from packages.modules.handoff import export_clean_handoff
    from test_clean_runner import _agent, _plan, SPECIFICATION

    original = 'def greeting() -> str:\n    return "wrong"\n'
    repaired = 'def greeting() -> str:\n    return "hello"\n'
    class Repairer:
        source_reader = alias_map = artifact_verifier = None
        seen = []
        def repair_files(self, spec, plan, failures, current, allowed, **kwargs):
            self.seen.append((failures, dict(current)))
            return {'replacements': [{'path': 'greeting.py',
                     'content': 'def greeting(:' if len(self.seen) == 1 else repaired}],
                    'rationale': 'candidate repair'}
    repairer = Repairer()
    builder = _agent(CleanBuilderAgent, json.dumps({'files': [
        {'path': 'greeting.py', 'content': original, 'requirement_ids': ['FR-001']}
    ], 'notes': []}))
    runner = CleanRunner(_agent(CleanPlannerAgent, json.dumps(_plan())), builder, repairer, max_repairs=budget)
    ordinary_validate = runner._validate
    def validate(workspace, *args, **kwargs):
        checks = ordinary_validate(workspace, *args, **kwargs)
        if 'wrong' in workspace.read_generated_file('greeting.py'):
            checks.append(_failed_check('python-behavior-probes', 'wrong result', ['greeting.py'],
                                        stage='behavior-validation'))
        else:
            checks.append(check('python-behavior-probes', 'pass'))
        return checks
    runner._validate = validate
    handoff = export_clean_handoff(tmp_path/'handoff', SPECIFICATION, {'status': 'pass'})
    result = runner.run(handoff, tmp_path/'output')
    assert (result.project_root/'greeting.py').read_text() == (original if budget == 1 else repaired)
    record = json.loads((result.output_root/'_clean/iterations/repair-1.json').read_text())
    assert record['outcome'] == 'rejected'
    assert record['before_content_hashes'] == record['after_content_hashes']
    assert record['candidate_replacements'][0]['content'] == 'def greeting(:'
    if budget == 2:
        assert repairer.seen[1][1]['greeting.py'] == original
        assert any(c['name'] == 'repair-candidate-rejected' for c in repairer.seen[1][0])

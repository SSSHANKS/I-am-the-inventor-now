import sys
from pathlib import Path

import pytest

from packages.modules.clean.behavior import (
    BehaviorProbeError,
    LocalPythonBehaviorProbeExecutor,
    validate_behavior_probe_suite,
)
from packages.modules.clean.runner import CleanRunner, _build_succeeded
from packages.modules.clean.workspace import CleanWorkspace


def _architecture():
    return {
        "project_profile": {"layout": "flat"},
        "capabilities": [
            {
                "capability_id": "CAP-001",
                "purpose": "Return the configured greeting.",
                "requirement_ids": ["FR-001", "AC-001"],
                "scenario_ids": ["TC-001"],
                "component_ids": ["CMP-001"],
            }
        ],
        "contracts": [
            {
                "contract_id": "SYM-001",
                "component_id": "CMP-001",
                "qualified_name": "greeting.greeting",
                "requirement_ids": ["FR-001", "AC-001"],
            }
        ],
    }


def _manifest():
    return {
        "files": [
            {
                "path": "greeting.py",
                "category": "source",
                "component_id": "CMP-001",
                "requirement_ids": ["FR-001", "AC-001"],
            }
        ]
    }


def _suite(code="from greeting import greeting\nassert greeting() == 'hello'\n"):
    return {
        "schema_version": 1,
        "probes": [
            {
                "probe_id": "PROBE-001",
                "capability_id": "CAP-001",
                "requirement_ids": ["FR-001"],
                "scenario_ids": ["TC-001"],
                "code": code,
            },
            {
                "probe_id": "PROBE-002",
                "capability_id": "CAP-001",
                "requirement_ids": ["AC-001"],
                "scenario_ids": [],
                "code": code,
            },
        ],
    }


def _executor():
    return LocalPythonBehaviorProbeExecutor(
        python_executable=sys.executable,
        timeout_seconds=10,
    )


def test_behavior_probe_suite_requires_complete_requirement_coverage():
    suite = _suite()
    suite["probes"] = suite["probes"][:1]

    with pytest.raises(BehaviorProbeError, match="AC-001"):
        validate_behavior_probe_suite(suite, _architecture())


def test_cross_capability_scenario_needs_one_suite_level_execution():
    architecture = _architecture()
    architecture["capabilities"] = [
        {**architecture["capabilities"][0], "requirement_ids": ["FR-001"]},
        {
            **architecture["capabilities"][0],
            "capability_id": "CAP-002",
            "requirement_ids": ["AC-001"],
        },
    ]
    suite = _suite()
    suite["probes"][1]["capability_id"] = "CAP-002"

    validate_behavior_probe_suite(suite, architecture)


def test_incomplete_suite_reports_gaps_from_every_capability():
    architecture = _architecture()
    architecture["capabilities"] = [
        {**architecture["capabilities"][0], "requirement_ids": ["FR-001"]},
        {
            **architecture["capabilities"][0],
            "capability_id": "CAP-002",
            "requirement_ids": ["AC-001"],
        },
    ]

    with pytest.raises(BehaviorProbeError) as exc_info:
        validate_behavior_probe_suite(
            {"schema_version": 1, "probes": []}, architecture
        )

    message = str(exc_info.value)
    assert "CAP-001 requirements: FR-001" in message
    assert "CAP-002 requirements: AC-001" in message


@pytest.mark.parametrize("result", ["hello", "wrong"])
def test_partial_probes_execute_and_preserve_coverage_gap(tmp_path, result):
    workspace = CleanWorkspace(tmp_path / "output")
    workspace.write_generated_file("greeting.py", f"def greeting():\n    return {result!r}\n")
    suite = _suite()
    suite["probes"].pop()
    checks = _executor().validate(workspace.project_root, _architecture(), _manifest(), suite)
    by_name = {item["name"]: item for item in checks}
    assert by_name["python-behavior-probes"]["status"] == (
        "pass" if result == "hello" else "fail"
    )
    assert by_name["python-behavior-coverage"]["status"] == "skipped"
    assert "AC-001" in by_name["python-behavior-coverage"]["message"]
    assert not _build_succeeded([
        *checks, {"name": "executable-validation", "status": "pass"}
    ])


def test_design_retains_safe_probe_when_another_probe_is_unsafe():
    from types import SimpleNamespace

    suite = _suite()
    suite["probes"][1]["code"] = "import socket\nassert 1 == 1\n"
    runner = CleanRunner.__new__(CleanRunner)
    runner.max_repairs = 0
    runner.behavior_prober = SimpleNamespace(design=lambda *args, **kwargs: suite)
    retained = runner._design_behavior_probes("specification", _architecture(), _manifest())
    assert len(retained["probes"]) == 1
    assert retained["probes"][0]["requirement_ids"] == ["FR-001"]
    with pytest.raises(BehaviorProbeError, match="forbidden"):
        validate_behavior_probe_suite(suite, _architecture(), require_complete=False)


@pytest.mark.parametrize(
    "code, message",
    [
        ("import socket\nassert True\n", "forbidden module"),
        ("import subprocess\nsubprocess.run(['unsafe'])\nassert True\n", "forbidden operation"),
        ("import pytest\nassert True\n", "undeclared module"),
        ("from greeting import greeting\ngreeting()\n", "no module-executed assertions"),
        (
            "def test_later():\n    assert False\n",
            "no module-executed assertions",
        ),
        (
            "from greeting import greeting\nassert isinstance(greeting(), str)\n",
            "only type, existence, or non-null assertions",
        ),
        (
            "events = []\nassert len(events) >= 0\n",
            "only type, existence, or non-null assertions",
        ),
        (
            "try:\n    raise ValueError()\nexcept Exception:\n    pass\nassert True\n",
            "unspecific exception",
        ),
        ("import os\nos.system('echo unsafe')\nassert True\n", "forbidden operation"),
        (
            "called = False\ndef callback():\n    nonlocal called\nassert called is False\n",
            "not executable Python",
        ),
    ],
)
def test_behavior_probe_suite_rejects_unsafe_or_non_observing_code(code, message):
    with pytest.raises(BehaviorProbeError, match=message):
        validate_behavior_probe_suite(_suite(code), _architecture())


def test_behavior_probe_suite_requires_mocking_for_external_boundaries():
    architecture = _architecture()
    architecture["capabilities"][0]["purpose"] = "Spawn an external editor process."

    with pytest.raises(BehaviorProbeError, match="must mock external boundary"):
        validate_behavior_probe_suite(_suite(), architecture)


def test_python_behavior_executor_passes_observable_behavior(tmp_path):
    workspace = CleanWorkspace(tmp_path / "output")
    workspace.write_generated_file(
        "greeting.py", "def greeting() -> str:\n    return 'hello'\n"
    )

    checks = _executor().validate(
        workspace.project_root,
        _architecture(),
        _manifest(),
        _suite(),
    )

    assert checks == [
        {
            "name": "python-behavior-probes",
            "status": "pass",
            "message": "Passed 2 behavior probe(s)",
            "paths": [],
        }
    ]


def test_python_behavior_executor_maps_failure_to_production_requirements(tmp_path):
    workspace = CleanWorkspace(tmp_path / "output")
    workspace.write_generated_file(
        "greeting.py", "def greeting() -> str:\n    return 'wrong'\n"
    )

    check = _executor().validate(
        workspace.project_root,
        _architecture(),
        _manifest(),
        _suite(),
    )[0]

    assert check["status"] == "fail"
    assert check["paths"] == ["greeting.py"]
    diagnostic = check["diagnostics"][0]
    assert diagnostic["requirement_ids"] == ["FR-001"]
    assert diagnostic["contract_ids"] == ["SYM-001"]
    assert diagnostic["actual"] == "exited 1"


def test_python_behavior_executor_rejects_probe_project_mutation(tmp_path):
    workspace = CleanWorkspace(tmp_path / "output")
    workspace.write_generated_file(
        "greeting.py", "def greeting() -> str:\n    return 'hello'\n"
    )
    suite = _suite(
        "from pathlib import Path\n"
        "import greeting\n"
        "Path(greeting.__file__).write_text(\"changed\", encoding=\"utf-8\")\n"
        "assert Path(greeting.__file__).read_text() == 'changed'\n"
    )

    check = _executor().validate(
        workspace.project_root,
        _architecture(),
        _manifest(),
        suite,
    )[0]

    assert check["status"] == "fail"
    assert "modified generated project files" in check["diagnostics"][0]["actual"]
    assert workspace.read_generated_file("greeting.py").startswith("def greeting")


def test_python_behavior_executor_sanitizes_host_runtime_paths(tmp_path):
    executor = _executor()
    python_root = Path(sys.executable).resolve().parent
    project_root = tmp_path / "project"
    probe_root = tmp_path / "probe"
    output = f"{python_root / 'Lib' / 'runpy.py'}\n{probe_root / 'probe.py'}"

    sanitized = executor._bounded(output, project_root, probe_root)

    assert str(python_root) not in sanitized
    assert str(probe_root) not in sanitized
    assert "<python-root>" in sanitized
    assert "<probe>" in sanitized


@pytest.mark.parametrize("setup", [
    "import sys\nsys.path.insert(0, 'replacement')",
    "import sys as runtime\nruntime.modules['greeting'] = None",
    "from sys import path as search\nsearch.append('replacement')",
    "from sys import modules as loaded\nloaded.clear()",
    "import sys\nsys.meta_path = []",
])
def test_probe_rejects_import_state_access_including_aliases(setup):
    with pytest.raises(BehaviorProbeError, match="import state"):
        validate_behavior_probe_suite(_suite(setup + "\nassert 1 == 1"), _architecture())


@pytest.mark.parametrize("layout", ["flat", "src"])
def test_runtime_accepts_real_package_and_temporary_input_fixture(tmp_path, layout):
    workspace = CleanWorkspace(tmp_path / 'output')
    prefix = 'src/' if layout == 'src' else ''
    workspace.write_generated_file(prefix + 'greeting/__init__.py',
                                   'def greeting(value):\n    return value.upper()\n')
    architecture = _architecture()
    architecture['project_profile']['layout'] = layout
    code = (
        "from greeting import greeting\nfrom pathlib import Path\nimport tempfile\n"
        "with tempfile.TemporaryDirectory() as directory:\n"
        "    fixture = Path(directory) / 'input.txt'\n"
        "    fixture.write_text('hello')\n"
        "    assert greeting(fixture.read_text()) == 'HELLO'\n"
    )
    checks = _executor().validate(workspace.project_root, architecture, _manifest(), _suite(code))
    assert all(check['status'] == 'pass' for check in checks)


@pytest.mark.parametrize('code', [
    # Dynamic access deliberately bypasses the conservative static alias check.
    "import sys, tempfile\nfrom pathlib import Path\n"
    "with tempfile.TemporaryDirectory() as directory:\n"
    "    (Path(directory) / 'greeting.py').write_text(\"def greeting(): return 'hello'\")\n"
    "    search = getattr(sys, 'path')\n"
    "    search.insert(0, directory)\n"
    "    from greeting import greeting\n"
    "    search.pop(0)\n"
    "    assert greeting() == 'hello'\n",
    "import sys, types\n"
    "replacement = types.ModuleType('greeting')\n"
    "replacement.greeting = lambda: 'hello'\n"
    "getattr(sys, 'modules')['greeting'] = replacement\n"
    "from greeting import greeting\nassert greeting() == 'hello'\n",
    "values = [1]\nassert len(values) == 1\n",
])
def test_runtime_rejects_substitution_or_no_project_import_as_incomplete(tmp_path, code):
    workspace = CleanWorkspace(tmp_path / 'output')
    workspace.write_generated_file('greeting.py', "def greeting():\n    return 'wrong'\n")
    checks = _executor().validate(workspace.project_root, _architecture(), _manifest(), _suite(code))
    assert any(check['name'] == 'python-behavior-coverage'
               and check['status'] == 'skipped'
               and 'PROBE_INTEGRITY' in check['message'] for check in checks)
    assert checks[-1]['message'] == 'Passed 0 behavior probe(s)'
    assert not _build_succeeded([*checks, {'name': 'executable-validation', 'status': 'pass'}])
    assert "'wrong'" in workspace.read_generated_file('greeting.py')


def test_missing_project_import_remains_a_production_failure(tmp_path):
    workspace = CleanWorkspace(tmp_path / 'output')
    workspace.write_generated_file('placeholder.py', '')
    checks = _executor().validate(workspace.project_root, _architecture(), _manifest(), _suite())
    assert len(checks) == 1
    assert checks[0]['status'] == 'fail'
    assert 'ModuleNotFoundError' in checks[0]['diagnostics'][0]['message']
    assert checks[0]['diagnostics'][0]['requirement_ids'] == ['FR-001']


def test_external_boundary_mock_still_exercises_real_project(tmp_path):
    workspace = CleanWorkspace(tmp_path / 'output')
    workspace.write_generated_file('greeting.py',
        'import subprocess\ndef greeting():\n    return subprocess.run(["external"]).returncode\n')
    code = (
        'from greeting import greeting\nfrom unittest.mock import patch\n'
        'with patch("greeting.subprocess.run") as boundary:\n'
        '    boundary.return_value.returncode = 7\n'
        '    assert greeting() == 7\n'
        '    assert boundary.call_count == 1\n'
    )
    checks = _executor().validate(workspace.project_root, _architecture(), _manifest(), _suite(code))
    assert checks[0]['status'] == 'pass'


@pytest.mark.parametrize('expression', [
    'True', '1 == 1', '2 > 1', "'x' in ['x']", 'not False',
    "greeting() == 'hello' or True", "True or greeting() == 'hello'",
    "(greeting() == 'hello' and False) or True",
    "isinstance(greeting(), str) or greeting() == 'hello'",
])
def test_vacuous_or_weak_alternatives_do_not_establish_behavior(expression):
    with pytest.raises(BehaviorProbeError, match='always-true assertions'):
        validate_behavior_probe_suite(_suite('from greeting import greeting\nassert ' + expression),
                                      _architecture())


@pytest.mark.parametrize('expression', [
    "greeting() == 'hello' and True",
    "greeting() == 'hello' or greeting() == 'welcome'",
    "isinstance(greeting(), str) and greeting() == 'hello'",
])
def test_meaningful_boolean_assertions_remain_valid(expression):
    validate_behavior_probe_suite(_suite('from greeting import greeting\nassert ' + expression),
                                  _architecture())


def test_non_null_public_constant_cannot_be_invented_alongside_strong_assertion():
    architecture = _architecture()
    architecture["contracts"].append({
        "contract_id": "SYM-002",
        "component_id": "CMP-001",
        "qualified_name": "greeting.WILDCARD",
        "kind": "constant",
        "declaration": "WILDCARD = None",
        "visibility": "public",
        "requirement_ids": ["FR-001"],
        "behavior_rules": [{
            "aspect": "state",
            "statement": "The wildcard selector is publicly accessible.",
            "requirement_ids": ["FR-001"],
            "source": "specification",
            "evidence": [{"requirement_id": "FR-001", "excerpt": "publicly accessible"}],
        }],
    })
    code = (
        "from greeting import WILDCARD, greeting\n"
        "assert WILDCARD is not None\n"
        "assert greeting('Ada') == 'Hello, Ada!'\n"
    )

    with pytest.raises(BehaviorProbeError, match="invents a non-null value"):
        validate_behavior_probe_suite(_suite(code), architecture)


def test_general_result_probe_cannot_contradict_sync_async_error_rule():
    architecture = _architecture()
    architecture["contracts"][0]["declaration"] = (
        "class Greeter:\n"
        "    def connect(self, handler: callable) -> None: ...\n"
        "    def send(self, name: str) -> list[object]: ..."
    )
    architecture["contracts"][0]["behavior_rules"] = [{
        "aspect": "errors",
        "statement": "Synchronous dispatch raises RuntimeError for an async handler.",
        "requirement_ids": ["EH-001"],
        "source": "specification",
        "evidence": [{
            "requirement_id": "EH-001",
            "excerpt": "Synchronous dispatch raises RuntimeError for an async handler.",
        }],
    }]
    architecture["capabilities"][0]["requirement_ids"].append("EH-001")
    code = (
        "from greeting import Greeter\n"
        "greeter = Greeter()\n"
        "async def receiver(name):\n    return name\n"
        "greeter.connect(receiver)\n"
        "greeter.send('Ada')\n"
        "assert len([1]) == 1\n"
    )

    with pytest.raises(BehaviorProbeError, match="contradicts synchronous/asynchronous"):
        validate_behavior_probe_suite(_suite(code), architecture, require_complete=False)


def test_disposable_lambda_cannot_be_registered_through_default_weak_connection():
    code = (
        "from greeting import greeting\n"
        "greeting.connect(lambda value: value)\n"
        "assert greeting() == 'hello'\n"
    )

    with pytest.raises(BehaviorProbeError, match="disposable lambda"):
        validate_behavior_probe_suite(_suite(code), _architecture())


def test_initialization_requirement_must_assert_initialized_state():
    architecture = _architecture()
    architecture["contracts"][0]["behavior_rules"] = [{
        "aspect": "state",
        "statement": "The object initializes an optional documentation attribute.",
        "requirement_ids": ["FR-001"],
        "source": "specification",
        "evidence": [{
            "requirement_id": "FR-001",
            "excerpt": "initializes an optional documentation attribute",
        }],
    }]
    code = (
        "from greeting import greeting\n"
        "result = greeting()\n"
        "assert result == 'hello'\n"
    )

    with pytest.raises(BehaviorProbeError, match="initialization requirement"):
        validate_behavior_probe_suite(_suite(code), architecture)


def test_cached_factory_initialization_can_be_proved_by_public_identity():
    architecture = _architecture()
    architecture["contracts"][0]["behavior_rules"] = [{
        "aspect": "state",
        "statement": "A missing name initializes and caches a new instance.",
        "requirement_ids": ["FR-001"],
        "source": "specification",
        "evidence": [{
            "requirement_id": "FR-001",
            "excerpt": "initializes and caches a new instance",
        }],
    }]
    code = (
        "from greeting import greeting\n"
        "first = greeting()\n"
        "second = first\n"
        "assert first is second\n"
    )

    validate_behavior_probe_suite(_suite(code), architecture)


def test_lifecycle_probe_must_register_observer_on_connected_public_channel():
    architecture = _architecture()
    architecture["contracts"][0]["behavior_rules"] = [{
        "aspect": "state",
        "statement": "Connection emits a lifecycle event to registered observers.",
        "requirement_ids": ["FR-001"],
        "source": "specification",
        "evidence": [{
            "requirement_id": "FR-001",
            "excerpt": "emits a lifecycle event to registered observers",
        }],
    }]
    code = (
        "from greeting import Greeter, ObserverRegistry\n"
        "registry = ObserverRegistry()\n"
        "emitter = Greeter()\n"
        "events = []\n"
        "registry.add_lifecycle_observer(events.append)\n"
        "emitter.connect(strong_handler, weak=False)\n"
        "assert len(events) == 1\n"
    )

    with pytest.raises(BehaviorProbeError, match="disconnected lifecycle observer"):
        validate_behavior_probe_suite(_suite(code), architecture)


def test_decorator_factory_must_be_applied_to_the_callback():
    architecture = _architecture()
    architecture["contracts"][0]["declaration"] = (
        "class Greeter:\n"
        "    def connect(self, sender: object = ...) -> callable: ...\n"
        "    def greet(self) -> str: ..."
    )
    code = (
        "from greeting import Greeter\n"
        "greeter = Greeter()\n"
        "def handler(value):\n    return value\n"
        "greeter.connect(handler)\n"
        "assert greeter.greet() == 'hello'\n"
    )

    with pytest.raises(BehaviorProbeError, match="without applying the returned decorator"):
        validate_behavior_probe_suite(_suite(code), architecture)


def test_probe_revisions_accumulate_independently_valid_requirements():
    initial = _suite()
    initial["probes"][1]["code"] = "from greeting import greeting\nassert True"
    revised = _suite()
    revised["probes"][0]["code"] = "from greeting import greeting\nassert True"

    class ProgressiveProber:
        target_requirement_ids = None

        def design(self, *args, **kwargs):
            return initial

        def revise(self, *args, **kwargs):
            self.target_requirement_ids = kwargs["target_requirement_ids"]
            return revised

    runner = CleanRunner.__new__(CleanRunner)
    runner.max_repairs = 1
    runner.behavior_prober = ProgressiveProber()

    retained = runner._design_behavior_probes(
        "approved specification", _architecture(), _manifest()
    )

    assert {probe["requirement_ids"][0] for probe in retained["probes"]} == {
        "FR-001",
        "AC-001",
    }
    assert runner.behavior_prober.target_requirement_ids == ["AC-001"]
    assert runner._behavior_probe_audit[0]["missing_requirement_ids"] == []


def test_exception_failure_guard_remains_valid(tmp_path):
    workspace = CleanWorkspace(tmp_path / 'output')
    workspace.write_generated_file('greeting.py', 'def greeting():\n    raise ValueError("invalid")\n')
    code = ('from greeting import greeting\ntry:\n    greeting()\n'
            '    assert False, "expected ValueError"\nexcept ValueError:\n    pass\n')
    checks = _executor().validate(workspace.project_root, _architecture(), _manifest(), _suite(code))
    assert checks[0]['status'] == 'pass'


def test_vacuous_probe_is_discarded_without_claiming_full_coverage(tmp_path):
    from types import SimpleNamespace
    suite = _suite()
    suite['probes'][1]['code'] = 'from greeting import greeting\ngreeting()\nassert True'
    runner = CleanRunner.__new__(CleanRunner)
    runner.max_repairs = 0
    runner.behavior_prober = SimpleNamespace(design=lambda *args, **kwargs: suite)
    retained = runner._design_behavior_probes('approved specification', _architecture(), _manifest())
    assert len(retained['probes']) == 1
    workspace = CleanWorkspace(tmp_path / 'output')
    workspace.write_generated_file('greeting.py', 'def greeting():\n    return "hello"\n')
    checks = _executor().validate(workspace.project_root, _architecture(), _manifest(), retained)
    assert any(c['name'] == 'python-behavior-coverage' and c['status'] == 'skipped' for c in checks)
    assert not _build_succeeded([*checks, {'name': 'executable-validation', 'status': 'pass'}])

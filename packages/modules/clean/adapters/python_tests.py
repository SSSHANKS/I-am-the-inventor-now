from __future__ import annotations

import ast
import json
import re
from typing import Any


_TEST_PATH = "tests/test_reconstruction_behavior.py"


def materialise_python_behavior_tests(
    behavior_suite: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Publish validated module-style probes as an ordinary pytest module.

    The probe source remains an immutable string and is compiled at test time. This
    preserves module execution semantics (including future imports and ``__name__``)
    while giving every probe an independently reported pytest test case.
    """
    if behavior_suite is None or not behavior_suite.get("probes"):
        return []

    lines = [
        '"""Clean-room behavior tests derived from the approved specification.\n\n',
        "This file is adapter-generated after production planning. Each test executes\n",
        "one immutable behavior probe in an isolated module namespace.\n",
        '"""\n\n',
        "\n",
        "def _run_probe(probe_id: str, source: str) -> None:\n",
        "    namespace = {\n",
        '        "__name__": "__main__",\n',
        '        "__file__": f"<{probe_id}>",\n',
        "        \"__package__\": None,\n",
        "    }\n",
        '    exec(compile(source, f"<{probe_id}>", "exec"), namespace, namespace)\n',
    ]
    requirement_ids: list[str] = []
    scenario_ids: list[str] = []
    used_names: set[str] = set()
    for probe in behavior_suite["probes"]:
        probe_id = str(probe["probe_id"])
        source = str(probe["code"])
        # The suite validator already performs this check. Repeating it here keeps
        # this deterministic boundary safe when called independently by an adapter.
        ast.parse(source, filename=f"<{probe_id}>")
        requirement_ids.extend(str(item) for item in probe["requirement_ids"])
        scenario_ids.extend(str(item) for item in probe["scenario_ids"])
        suffix_parts = [probe_id, *probe["requirement_ids"], *probe["scenario_ids"]]
        suffix = re.sub(
            r"[^a-z0-9]+", "_", "_".join(suffix_parts).casefold()
        ).strip("_")
        name = f"test_{suffix}"
        if name in used_names:
            raise ValueError(f"Behavior probes produce duplicate test name {name!r}")
        used_names.add(name)
        source_name = f"_SOURCE_{probe_id.replace('-', '_')}"
        lines.extend(
            [
                "\n\n",
                f"# Capability: {probe['capability_id']}\n",
                f"# Requirements: {', '.join(probe['requirement_ids'])}\n",
                f"# Scenarios: {', '.join(probe['scenario_ids']) or 'none'}\n",
                f"{source_name} = {json.dumps(source, ensure_ascii=False)}\n\n",
                f"def {name}() -> None:\n",
                f'    _run_probe("{probe_id}", {source_name})\n',
            ]
        )

    content = "".join(lines)
    compile(content, _TEST_PATH, "exec")
    return [
        {
            "path": _TEST_PATH,
            "content": content,
            "requirement_ids": list(dict.fromkeys(requirement_ids)),
            "scenario_ids": list(dict.fromkeys(scenario_ids)),
        }
    ]

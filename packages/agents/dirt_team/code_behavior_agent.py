import json
import logging
from typing import Any

from packages.agents.base_agent import (
    BaseAgent,
)
from packages.agents.planning import format_mini_tasks, iter_mini_tasks, log_mini_tasks
from packages.modules.indexing import build_source_code_index_context
from packages.modules.ingesting import SourceManifest
from packages.modules.supervising.schemas import (
    BEHAVIOR_OUTPUT_FIELDS,
    BehaviorAnalyzerSchema,
    BehaviorNarrowBehaviorsSchema,
    BehaviorNarrowEdgeCasesSchema,
    BehaviorNarrowErrorHandlingSchema,
    BehaviorNarrowOpenQuestionsSchema,
    BehaviorNarrowRequirementsSchema,
    BehaviorNarrowTestCandidatesSchema,
)

log = logging.getLogger(__name__)

instruction = """
[Agent Role]
You are the Behavior Analyzer Agent.
You analyze runtime behavior from verified code facts, documentation facts, and selected source files.
You focus on behavior that can later become formal specification and tests.

[Allowed Inputs]
- code_facts_report: concrete facts extracted from code.
- documentation_report: facts extracted from documentation, when provided.
- source_code_index: deterministic code index for orientation.

[Source Of Truth]
- The provided reports and pre-read source sections are the only sources of truth.
- Do not execute code.
- Do not access files outside the repository snapshot.
- Do not invent expected behavior.
- Do not turn recommendations into observed behavior.

[What To Extract]
- user-visible behaviors
- inputs and outputs
- preconditions and postconditions
- state changes
- edge cases
- error handling
- testable scenarios
- gaps where expected behavior is unknown

[Mini Task Execution]
- If mini_tasks are provided, treat them as the required checklist.
- Execute each mini task using its input_refs, requirements, output_field, and success_criteria.
- Do not skip a mini task silently. If it cannot be completed, add a missing open question or missing behavior with evidence.

[Labels]
- observed: directly visible in code facts, documentation, or source lines
- inferred: likely but not directly proven
- missing: expected behavior information was not found

[Behavior Rules]
- Do not produce broad summaries. Produce atomic behaviors.
- Each behavior must be testable or explicitly marked as not testable.
- Separate current observed behavior from expected or recommended behavior.
- If expected behavior comes from documentation, set expected_source to "documentation".
- If expected behavior comes from code, set expected_source to "code".
- If expected behavior is your recommendation, set expected_source to "recommendation".

[Evidence Rules]
- Use code facts evidence whenever possible.
- If evidence in code facts has line numbers, preserve them.
- Every non-missing item must include evidence.
- Do not create fake file paths, fake line numbers, or fake excerpts.

[Output Rules]
- Return ONLY valid JSON.
- Do not use Markdown.
- Do not add text before or after JSON.

Use this exact JSON schema:

{
  "files_read": ["..."],
  "behaviors": [
    {
      "label": "observed | inferred | missing",
      "name": "...",
      "description": "...",
      "inputs": ["..."],
      "outputs": ["..."],
      "preconditions": ["..."],
      "postconditions": ["..."],
      "testability": "unit | integration | gui | manual | unknown",
      "evidence": {
        "source": "code | documentation | code_facts | inference",
        "file": "...",
        "line_start": 1,
        "line_end": 1,
        "excerpt": "..."
      }
    }
  ],
  "edge_cases": [
    {
      "label": "observed | inferred | missing",
      "behavior": "...",
      "case": "...",
      "current_result": "...",
      "expected_result": "...",
      "expected_source": "code | documentation | recommendation | unknown",
      "evidence": {
        "source": "code | documentation | code_facts | inference",
        "file": "...",
        "line_start": 1,
        "line_end": 1,
        "excerpt": "..."
      }
    }
  ],
  "error_handling": [
    {
      "label": "observed | inferred | missing",
      "behavior": "...",
      "error": "...",
      "handling": "...",
      "evidence": {
        "source": "code | documentation | code_facts | inference",
        "file": "...",
        "line_start": 1,
        "line_end": 1,
        "excerpt": "..."
      }
    }
  ],
  "test_candidates": [
    {
      "label": "observed | inferred | missing",
      "name": "...",
      "test_type": "unit | integration | gui | manual",
      "given": ["..."],
      "when": ["..."],
      "then": ["..."],
      "assertions": ["..."],
      "evidence": {
        "source": "code | documentation | code_facts | inference",
        "file": "...",
        "line_start": 1,
        "line_end": 1,
        "excerpt": "..."
      }
    }
  ],
  "specification_requirements": [
    {
      "label": "observed | inferred | missing",
      "requirement": "...",
      "source": "documentation | code | recommendation | unknown",
      "evidence": {
        "source": "code | documentation | code_facts | inference",
        "file": "...",
        "line_start": 1,
        "line_end": 1,
        "excerpt": "..."
      }
    }
  ],
  "open_questions": [
    {
      "label": "missing",
      "value": "...",
      "evidence": {
        "source": "code | documentation | code_facts | inference",
        "file": "...",
        "line_start": 1,
        "line_end": 1,
        "excerpt": "..."
      }
    }
  ]
}
""".strip()

mini_task_instruction = """
[Agent Role]
You are the Behavior Analyzer Agent. For ONE mini task you receive selected source
sections and verified artifacts. Extract small behavior-analysis items for the requested
output_field.

The controller has already selected exact source ranges when available and will attach
evidence to your output. Do NOT emit evidence, file, line_start, or line_end.

[Output Contract]
Return ONE JSON object:

{
  "items": [
    { "...": "..." }
  ]
}

Each item MUST include source_ref. Use the integer ID of the source section that best
supports the item. Use null only for missing open questions that have no related source.

[Allowed item shapes by output_field]

behaviors:
{
  "source_ref": 1,
  "label": "observed | inferred | missing",
  "name": "...",
  "description": "...",
  "inputs": ["..."],
  "outputs": ["..."],
  "preconditions": ["..."],
  "postconditions": ["..."],
  "testability": "unit | integration | gui | manual | unknown"
}

edge_cases:
{
  "source_ref": 1,
  "label": "observed | inferred | missing",
  "behavior": "...",
  "case": "...",
  "current_result": "...",
  "expected_result": "...",
  "expected_source": "code | documentation | recommendation | unknown"
}

error_handling:
{
  "source_ref": 1,
  "label": "observed | inferred | missing",
  "behavior": "...",
  "error": "...",
  "handling": "..."
}

test_candidates:
{
  "source_ref": 1,
  "label": "observed | inferred | missing",
  "name": "...",
  "test_type": "unit | integration | gui | manual",
  "given": ["..."],
  "when": ["..."],
  "then": ["..."],
  "assertions": ["..."]
}

specification_requirements:
{
  "source_ref": 1,
  "label": "observed | inferred | missing",
  "requirement": "...",
  "source": "documentation | code | recommendation | unknown"
}

open_questions:
{
  "source_ref": 1,
  "label": "missing",
  "value": "..."
}

[Rules]
- Use only source_sections and verified artifacts in the prompt.
- Do not invent expected behavior.
- Mark recommendation-only expectations as source="recommendation" or expected_source="recommendation".
- If output_field is not supported, return {"items": []}.
- Return ONLY the JSON object. No Markdown, no prose.
""".strip()

_NARROW_SCHEMAS = {
    "behaviors": BehaviorNarrowBehaviorsSchema,
    "edge_cases": BehaviorNarrowEdgeCasesSchema,
    "error_handling": BehaviorNarrowErrorHandlingSchema,
    "test_candidates": BehaviorNarrowTestCandidatesSchema,
    "specification_requirements": BehaviorNarrowRequirementsSchema,
    "open_questions": BehaviorNarrowOpenQuestionsSchema,
}


class BehaviorAnalyzerAgent(BaseAgent):
    agent_name = "Behavior Analyzer Agent"
    instruction = instruction

    def analyze(
        self,
        source_manifest: SourceManifest,
        code_facts_report: str | dict[str, Any],
        documentation_report: str | dict[str, Any] | None = None,
        source_code_index: str | dict[str, Any] | None = None,
        mini_tasks: str | dict[str, Any] | list[Any] | None = None,
    ) -> str:
        log_mini_tasks("Behavior Analyzer Agent", mini_tasks, phase="execution")
        code_files = source_manifest.code or []
        documentation_files = source_manifest.documentation or []
        mini_task_list = list(iter_mini_tasks(mini_tasks))

        if mini_task_list:
            return self.execute_mini_tasks(
                mini_task_list=mini_task_list,
                allowed_files=code_files + documentation_files,
                source_reader=self.source_reader,
                output_field_schemas=_NARROW_SCHEMAS,
                narrow_instruction=mini_task_instruction,
                build_narrow_prompt=_make_narrow_prompt_builder(
                    documentation_report=documentation_report,
                    code_facts_report=code_facts_report,
                    source_code_index=source_code_index,
                ),
                initial_payload=_initial_payload,
                handle_missing_sections=_handle_missing_sections,
                handle_narrow_error=_handle_narrow_error,
                handle_narrow_result=_handle_narrow_result,
                finalize_payload=_finalize_payload,
                final_instruction=instruction,
                final_task_instruction=(
                    "The controller aggregated behavior mini-task outputs into one JSON artifact. "
                    "If a validation repair is requested, rewrite only that JSON artifact. "
                    "Do not call tools and do not add new findings."
                ),
                final_agent_name="Behavior Analyzer Agent",
                final_schema=BehaviorAnalyzerSchema(),
                artifact_verifier=self.artifact_verifier,
                verifier_allowed_files=code_files + documentation_files,
                repo_local_path=self.source_reader.repo_path,
                recorder_scope="agents",
                recorder_sub_scope="Behavior Analyzer Agent [aggregated]",
                read_all_sections=True,
                run_with_empty_sections=True,
            )

        raise ValueError(
            "Behavior Analyzer Agent requires a plan: pass mini_tasks. The former "
            "single-shot fallback drove the model through source-reading tools, and there "
            "is no tool surface any more - AgentProvider is prompt to text (CLAUDE.md "
            "section 3). Source is pre-read from the plan's input_refs instead."
        )


def _initial_payload() -> dict[str, Any]:
    return {
        "files_read": [],
        **{field: [] for field in BEHAVIOR_OUTPUT_FIELDS},
    }


def _handle_missing_sections(
    aggregated: dict[str, Any],
    mini_task: dict[str, Any],
    output_field: str,
    task_id: str,
) -> None:
    aggregated["open_questions"].append(
        _missing_open_question(
            f"Mini task {task_id} had no valid source input_ref; behavior could not be grounded."
        )
    )


def _handle_narrow_error(
    aggregated: dict[str, Any],
    mini_task: dict[str, Any],
    output_field: str,
    task_id: str,
    sections: list[dict[str, Any]],
    reason: str,
) -> None:
    aggregated["open_questions"].append(
        _missing_open_question(
            f"Mini task {task_id} did not return a valid narrow {output_field} result ({reason}).",
            section=sections[0] if sections else None,
        )
    )


def _handle_narrow_result(
    aggregated: dict[str, Any],
    mini_task: dict[str, Any],
    output_field: str,
    task_id: str,
    sections: list[dict[str, Any]],
    narrow: dict[str, Any],
) -> None:
    items = narrow.get("items")
    if not isinstance(items, list) or not items:
        aggregated["open_questions"].append(
            _missing_open_question(
                f"Mini task {task_id} produced no {output_field} items for the selected evidence.",
                section=sections[0] if sections else None,
            )
        )
        return

    for item in items:
        if not isinstance(item, dict):
            continue
        entry = _compose_entry(output_field, item, sections)
        if entry is None:
            aggregated["open_questions"].append(
                _missing_open_question(
                    f"Mini task {task_id} produced an item without a valid source_ref.",
                    section=sections[0] if sections else None,
                )
            )
            continue
        aggregated[output_field].append(entry)


def _finalize_payload(aggregated: dict[str, Any], files_seen: set[str]) -> dict[str, Any]:
    aggregated["files_read"] = sorted(files_seen)
    for field in BEHAVIOR_OUTPUT_FIELDS:
        aggregated[field] = _dedupe_entries(aggregated[field])
    return aggregated


def _compose_entry(
    output_field: str,
    item: dict[str, Any],
    sections: list[dict[str, Any]],
) -> dict[str, Any] | None:
    section = _section_for_item(item, sections)
    if output_field != "open_questions" and section is None:
        return None

    evidence = _evidence(section, item.get("value") or "No related source section.")
    label = item.get("label")

    if output_field == "behaviors":
        return {
            "label": label,
            "name": item.get("name"),
            "description": item.get("description"),
            "inputs": item.get("inputs") or [],
            "outputs": item.get("outputs") or [],
            "preconditions": item.get("preconditions") or [],
            "postconditions": item.get("postconditions") or [],
            "testability": item.get("testability"),
            "evidence": evidence,
        }

    if output_field == "edge_cases":
        return {
            "label": label,
            "behavior": item.get("behavior"),
            "case": item.get("case"),
            "current_result": item.get("current_result"),
            "expected_result": item.get("expected_result"),
            "expected_source": item.get("expected_source"),
            "evidence": evidence,
        }

    if output_field == "error_handling":
        return {
            "label": label,
            "behavior": item.get("behavior"),
            "error": item.get("error"),
            "handling": item.get("handling"),
            "evidence": evidence,
        }

    if output_field == "test_candidates":
        return {
            "label": label,
            "name": item.get("name"),
            "test_type": item.get("test_type"),
            "given": item.get("given") or [],
            "when": item.get("when") or [],
            "then": item.get("then") or [],
            "assertions": item.get("assertions") or [],
            "evidence": evidence,
        }

    if output_field == "specification_requirements":
        return {
            "label": label,
            "requirement": item.get("requirement"),
            "source": item.get("source"),
            "evidence": evidence,
        }

    if output_field == "open_questions":
        return {
            "label": "missing",
            "value": item.get("value"),
            "evidence": evidence,
        }

    return None


def _section_for_item(
    item: dict[str, Any],
    sections: list[dict[str, Any]],
) -> dict[str, Any] | None:
    source_ref = item.get("source_ref")
    if isinstance(source_ref, int):
        for section in sections:
            if section["source_ref"] == source_ref:
                return section
    return None


def _evidence(section: dict[str, Any] | None, fallback: str) -> dict[str, Any]:
    if section is None:
        return {
            "source": "inference",
            "file": None,
            "line_start": None,
            "line_end": None,
            "excerpt": fallback,
        }
    return {
        "source": _evidence_source(section),
        "file": section["file"],
        "line_start": section["line_start"],
        "line_end": section["line_end"],
        "excerpt": section["text"].strip(),
    }


def _evidence_source(section: dict[str, Any]) -> str:
    ref_source = str(section.get("ref_source") or "").lower()
    if ref_source.startswith("code_facts_report."):
        return "code_facts"
    if ref_source.startswith("documentation_report.") or ref_source.startswith("source_doc_index."):
        return "documentation"
    if ref_source.startswith("source_code_index."):
        return "code"
    if section.get("file", "").lower().endswith((".md", ".rst", ".txt", ".adoc")):
        return "documentation"
    return "code"


def _missing_open_question(reason: str, section: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "label": "missing",
        "value": reason,
        "evidence": _evidence(section, reason),
    }


def _dedupe_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for entry in entries:
        key = json.dumps(entry, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entry)
    return deduped


def _make_narrow_prompt_builder(
    documentation_report: str | dict[str, Any] | None,
    code_facts_report: str | dict[str, Any] | None,
    source_code_index: str | dict[str, Any] | None,
):
    def _build_narrow_user_prompt(
        mini_task: dict[str, Any],
        output_field: str,
        task_id: str,
        sections: list[dict[str, Any]],
    ) -> str:
        requirements = mini_task.get("requirements") or []
        requirements_block = (
            "\n".join(f"- {r}" for r in requirements if isinstance(r, str) and r.strip())
            or "- (no specific requirements)"
        )
        sections_block = (
            "\n\n".join(_format_section(section) for section in sections)
            or "<no source sections were available; use verified artifacts and mark unsupported facts as missing>"
        )
        return f"""
[task_id]
{task_id}

[output_field]
{output_field}

[requirements]
{requirements_block}

[source_sections]
{sections_block}

[documentation_report]
{_format_artifact(documentation_report)}

[code_facts_report]
{_format_artifact(code_facts_report)}

[source_code_index]
{_format_artifact(source_code_index)}

Produce ONE JSON object with exactly one top-level key: items.
Each item must match the allowed shape for output_field={output_field!r}.
Do not include evidence, file, line_start, line_end, task_id, or output_field.
""".strip()

    return _build_narrow_user_prompt


def _format_section(section: dict[str, Any]) -> str:
    return f"""
[source_ref={section["source_ref"]} source={section.get("ref_source") or "unknown"} location={section["file"]}:{section["line_start"]}-{section["line_end"]}]
\"\"\"
{section["text"]}
\"\"\"
""".strip()


def build_task_instruction(
    source_manifest: SourceManifest,
    code_facts_report: str | dict[str, Any],
    documentation_report: str | dict[str, Any] | None = None,
    source_code_index: str | dict[str, Any] | None = None,
    mini_tasks: str | dict[str, Any] | list[Any] | None = None,
) -> str:
    code_files = source_manifest.code or []
    code_list = "\n".join(f"- {path}" for path in code_files) or "- <no code files found>"
    documentation = _format_artifact(documentation_report)
    code_index = _format_source_code_index(source_code_index)
    mini_task_text = format_mini_tasks(mini_tasks)

    return f"""
<task>
Analyze runtime behavior for this repository.
</task>

<repository_metadata>
- url: {source_manifest.repo_url}
- branch: {source_manifest.branch}
- commit: {source_manifest.commit_hash}
</repository_metadata>

<code_files>
{code_list}
</code_files>

<documentation_report>
{documentation}
</documentation_report>

<source_code_index>
{code_index}
</source_code_index>

<code_facts_report>
{_format_artifact(code_facts_report)}
</code_facts_report>

<mini_tasks>
{mini_task_text}
</mini_tasks>

<constraints>
- If <mini_tasks> is provided, execute those mini tasks as the primary scope of the analysis.
- Use source reader tools only when the provided reports are insufficient.
- Build test candidates as Given/When/Then scenarios.
- Do not invent expected behavior.
- If expected behavior is a recommendation, mark expected_source as "recommendation".
- Return the Behavior Analysis JSON.
</constraints>
""".strip()


def _format_source_code_index(source_code_index: str | dict[str, Any] | None) -> str:
    if source_code_index is None:
        return "<no source code index provided>"

    if isinstance(source_code_index, str):
        return source_code_index

    context = build_source_code_index_context(source_code_index)
    return json.dumps(context, ensure_ascii=False, indent=2)


def _format_artifact(value: str | dict[str, Any] | None) -> str:
    if value is None:
        return "<not provided>"
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2)

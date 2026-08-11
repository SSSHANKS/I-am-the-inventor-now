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
    CODE_FACTS_OUTPUT_FIELDS,
    CodeFactsAnalyzerSchema,
    CodeFactsNarrowCallsSchema,
    CodeFactsNarrowErrorsSchema,
    CodeFactsNarrowImportsSchema,
    CodeFactsNarrowOpenQuestionsSchema,
    CodeFactsNarrowStateSchema,
    CodeFactsNarrowSymbolsSchema,
)

log = logging.getLogger(__name__)

instruction = """
[Agent Role]
You are the Code Facts Agent.
You extract concrete, evidence-backed facts from source code.
You do not write a high-level overview and you do not speculate beyond visible source facts.

[Allowed Inputs]
- source_manifest: repository metadata and allowed code files.
- source_code_index: deterministic index containing files, entrypoints, symbols, imports, configs, calls, and analysis_targets.

[Source Of Truth]
- Source files and source_code_index are the only sources of truth.
- Do not execute code.
- Do not access files outside the repository snapshot.
- Do not convert recommendations, likely design intent, or naming guesses into facts.

[What To Extract]
- symbols: modules, classes, functions, methods, constants, and configs.
- imports: direct imports and dependency references.
- calls: direct call relationships between visible symbols.
- state_and_side_effects: state reads, state writes, mutation, GUI updates, I/O, process control, persistence, or other side effects.
- errors_and_exceptions: explicit exceptions, error branches, error handling, or missing handling visible in the selected source.
- open_questions: facts that cannot be established from the selected source.

[Labels]
- observed: directly visible in code you read.
- inferred: strongly implied by the selected code, but not directly stated.
- missing: expected information was not found.

[Evidence Rules]
- The controller attaches evidence for mini-task mode.
- Do not claim a class, function, method, import, call, state write, return value, exception, or dependency exists unless it is supported by source lines.

[Output Rules]
- Return ONLY valid JSON.
- Do not use Markdown.
- Do not add text before or after JSON.
""".strip()


mini_task_instruction = """
[Agent Role]
You are the Code Facts Agent. For ONE mini task you receive source code ranges already
read by the controller. Your job is to extract small code facts for the requested
output_field.

The controller has already:
- Selected allowed files and exact line ranges from the plan.
- Read those line ranges from the repository snapshot.
- Will attach evidence objects to your output.

You do NOT call tools. You do NOT choose files or line ranges. You do NOT emit evidence.

[Output Contract]
Return ONE JSON object:

{
  "items": [
    { "...": "..." }
  ]
}

Each item MUST include source_ref. source_ref is the integer ID of the source section
that supports the item. Use null only for missing open questions that have no related
source section.

[Allowed item shapes by output_field]

symbols:
{
  "source_ref": 1,
  "label": "observed | inferred | missing",
  "symbol": "...",
  "kind": "module | class | function | method | constant | config",
  "signature": "...",
  "inputs": ["..."],
  "returns": "...",
  "description": "..."
}

imports:
{
  "source_ref": 1,
  "label": "observed | inferred | missing",
  "import": "..."
}

calls:
{
  "source_ref": 1,
  "label": "observed | inferred | missing",
  "caller": "...",
  "callee": "..."
}

state_and_side_effects:
{
  "source_ref": 1,
  "label": "observed | inferred | missing",
  "symbol": "...",
  "state_read": ["..."],
  "state_written": ["..."],
  "side_effects": ["..."],
  "value": "..."
}

errors_and_exceptions:
{
  "source_ref": 1,
  "label": "observed | inferred | missing",
  "symbol": "...",
  "error": "...",
  "handling": "...",
  "value": "..."
}

open_questions:
{
  "source_ref": 1,
  "label": "missing",
  "value": "..."
}

[Rules]
- Use only the provided source_sections.
- If output_field is not supported by the source code you received, return an empty items list.
- For imports, use the exact import statement or module name visible in code.
- For symbols, one item equals one symbol. Do not merge multiple symbols into one item.
- For calls, one item equals one caller -> callee relation.
- Do not include file, line_start, line_end, evidence, task_id, output_field, or explanations.
- Return ONLY the JSON object. No Markdown, no code fences, no prose.
""".strip()


_NARROW_SCHEMAS = {
    "symbols": CodeFactsNarrowSymbolsSchema,
    "imports": CodeFactsNarrowImportsSchema,
    "calls": CodeFactsNarrowCallsSchema,
    "state_and_side_effects": CodeFactsNarrowStateSchema,
    "errors_and_exceptions": CodeFactsNarrowErrorsSchema,
    "open_questions": CodeFactsNarrowOpenQuestionsSchema,
}


class CodeFactsAgent(BaseAgent):
    agent_name = "Code Facts Agent"
    instruction = instruction

    def analyze(
        self,
        source_manifest: SourceManifest,
        source_code_index: str | dict[str, Any] | None = None,
        mini_tasks: str | dict[str, Any] | list[Any] | None = None,
    ) -> str:
        log_mini_tasks("Code Facts Agent", mini_tasks, phase="execution")
        code_files = source_manifest.code or []
        mini_task_list = list(iter_mini_tasks(mini_tasks))

        if not mini_task_list:
            raise ValueError(
                "Code Facts Agent requires a plan: pass mini_tasks. The former single-shot "
                "fallback drove the model through source-reading tools, and there is no "
                "tool surface any more - AgentProvider is prompt to text (CLAUDE.md "
                "section 3). Source is pre-read from the plan's input_refs instead."
            )

        return self.execute_mini_tasks(
            mini_task_list=mini_task_list,
            allowed_files=code_files,
            source_reader=self.source_reader,
            output_field_schemas=_NARROW_SCHEMAS,
            narrow_instruction=mini_task_instruction,
            build_narrow_prompt=_build_narrow_user_prompt,
            initial_payload=_initial_payload,
            handle_missing_sections=_handle_missing_sections,
            handle_narrow_error=_handle_narrow_error,
            handle_narrow_result=_handle_narrow_result,
            finalize_payload=_finalize_payload,
            final_instruction=instruction,
            final_task_instruction=(
                "The controller aggregated code-facts mini-task outputs into one JSON artifact. "
                "If a validation repair is requested, rewrite only that JSON artifact. "
                "Do not call tools and do not add new findings."
            ),
            final_agent_name="Code Facts Agent",
            final_schema=CodeFactsAnalyzerSchema(),
            artifact_verifier=self.artifact_verifier,
            verifier_allowed_files=code_files,
            repo_local_path=self.source_reader.repo_path,
            recorder_scope="agents",
            recorder_sub_scope="Code Facts Agent [aggregated]",
            read_all_sections=True,
            run_with_empty_sections=False,
        )


def _initial_payload() -> dict[str, Any]:
    return {
        "files_read": [],
        **{field: [] for field in CODE_FACTS_OUTPUT_FIELDS},
    }


def _handle_missing_sections(
    aggregated: dict[str, Any],
    mini_task: dict[str, Any],
    output_field: str,
    task_id: str,
) -> None:
    aggregated["open_questions"].append(
        _missing_open_question(
            f"Mini task {task_id} could not be executed because it had no valid code input_ref."
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
                f"Mini task {task_id} produced no {output_field} items for the selected source range.",
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
    for field in CODE_FACTS_OUTPUT_FIELDS:
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

    if output_field == "symbols":
        return {
            "label": label,
            "file": section["file"],
            "symbol": item.get("symbol"),
            "kind": item.get("kind"),
            "signature": item.get("signature"),
            "inputs": item.get("inputs") or [],
            "returns": item.get("returns"),
            "description": item.get("description"),
            "evidence": evidence,
        }

    if output_field == "imports":
        return {
            "label": label,
            "file": section["file"],
            "import": item.get("import"),
            "evidence": evidence,
        }

    if output_field == "calls":
        return {
            "label": label,
            "caller": item.get("caller"),
            "callee": item.get("callee"),
            "evidence": evidence,
        }

    if output_field == "state_and_side_effects":
        return {
            "label": label,
            "symbol": item.get("symbol"),
            "state_read": item.get("state_read") or [],
            "state_written": item.get("state_written") or [],
            "side_effects": item.get("side_effects") or [],
            "value": item.get("value"),
            "evidence": evidence,
        }

    if output_field == "errors_and_exceptions":
        return {
            "label": label,
            "symbol": item.get("symbol"),
            "error": item.get("error"),
            "handling": item.get("handling"),
            "value": item.get("value"),
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
            "file": None,
            "line_start": None,
            "line_end": None,
            "excerpt": fallback,
        }
    return {
        "file": section["file"],
        "line_start": section["line_start"],
        "line_end": section["line_end"],
        "excerpt": section["text"].strip(),
    }


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
    sections_block = "\n\n".join(_format_section(section) for section in sections)
    return f"""
[task_id]
{task_id}

[output_field]
{output_field}

[requirements]
{requirements_block}

[source_sections]
{sections_block}

Produce ONE JSON object with exactly one top-level key: items.
Each item must match the allowed shape for output_field={output_field!r}.
Do not include evidence, file, line_start, line_end, task_id, or output_field.
""".strip()


def _format_section(section: dict[str, Any]) -> str:
    return f"""
[source_ref={section["source_ref"]} location={section["file"]}:{section["line_start"]}-{section["line_end"]}]
\"\"\"
{section["text"]}
\"\"\"
""".strip()


def build_task_instruction(
    source_manifest: SourceManifest,
    source_code_index: str | dict[str, Any] | None = None,
    mini_tasks: str | dict[str, Any] | list[Any] | None = None,
) -> str:
    code_files = source_manifest.code or []
    code_list = "\n".join(f"- {path}" for path in code_files) or "- <no code files found>"
    code_index = _format_source_code_index(source_code_index)
    mini_task_text = format_mini_tasks(mini_tasks)

    return f"""
<task>
Extract concrete code facts for this repository.
</task>

<repository_metadata>
- url: {source_manifest.repo_url}
- branch: {source_manifest.branch}
- commit: {source_manifest.commit_hash}
</repository_metadata>

<code_files>
{code_list}
</code_files>

<source_code_index>
{code_index}
</source_code_index>

<mini_tasks>
{mini_task_text}
</mini_tasks>

<constraints>
- If <mini_tasks> is provided, execute those mini tasks as the primary scope of the analysis.
- Read only the files needed to extract concrete facts.
- Prefer files and symbols identified as important by the source code index.
- Do not inspect every file unless it is listed as an analysis target.
- If the source code index contains analysis_targets, treat them as the primary scope.
- Use the line numbers returned by tools or source_code_index evidence in every evidence object.
- Return the Code Facts JSON.
</constraints>
""".strip()


def _format_source_code_index(source_code_index: str | dict[str, Any] | None) -> str:
    if source_code_index is None:
        return "<no source code index provided>"

    if isinstance(source_code_index, str):
        return source_code_index

    context = build_source_code_index_context(source_code_index)
    return json.dumps(context, ensure_ascii=False, indent=2)

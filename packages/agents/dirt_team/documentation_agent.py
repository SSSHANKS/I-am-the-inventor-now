import json
import logging
from typing import Any

from packages.agents.base_agent import (
    BaseAgent,
)
from packages.agents.planning import format_mini_tasks, iter_mini_tasks, log_mini_tasks
from packages.modules.indexing import build_source_doc_index_context
from packages.modules.ingesting import SourceManifest
from packages.modules.supervising.schemas import DocAnalyzerSchema
from packages.modules.supervising.schemas.doc_analyzer import (
    DOC_KIND_LABELED,
    DOC_KIND_OPEN_QUESTION,
    DOC_OUTPUT_FIELDS,
    DOC_SINGLE_FIELDS,
    DocNarrowLabeledSchema,
    DocNarrowOpenQuestionSchema,
)

log = logging.getLogger(__name__)

instruction = """
[Agent Role]
You are the Documentation Agent.
You analyze documentation files from a Git repository source manifest or a deterministic documentation index.

[Scope]
Work only from the documentation index and the sections the controller supplies.
Do not invent documentation content. If something is missing or unreadable, report it in warnings.

[Source Of Truth]
- The supplied index entries and section text are the only sources of truth.
- Never use prior knowledge, repository names, README templates, package conventions, or memory from earlier runs.
- Never invent title text, feature lists, commands, dependencies, or README sections.
- If a fact is not visible in the documentation lines you read, mark it as missing or inferred.
- Prefer fewer correct findings over many weak findings.

[Tasks]
- Read README files, docs, API specs, and configuration docs.
- Identify the project purpose.
- Extract concrete setup/run instructions.
- Extract API information if present.
- Extract user-facing features.
- Note missing, outdated, or conflicting documentation.

[Mini Task Execution]
- If mini_tasks are provided, treat them as the required checklist.
- Execute each mini task using its input_refs, requirements, output_field, and success_criteria.
- Do not skip a mini task silently. If it cannot be completed, report a missing finding or open question with evidence.

[Extraction Workflow]
1. Inspect the supplied documentation index.
2. Identify candidate facts only from that index's evidence.
3. For each candidate fact, choose the smallest line range that directly supports it.
4. Before returning JSON, verify every evidence.excerpt against its file, line_start, and line_end.

Be specific about BEHAVIOUR, and never by copying the documentation's own words.
Describe what a step accomplishes and what it requires, not the literal text that expresses it.
Avoid generic summaries such as "setup instructions exist" or "contribution guidelines exist".
If documentation contains commands, describe what the command achieves - never reproduce the command itself.
If documentation mentions modules or files, describe their role rather than naming them.
If documentation mentions features, describe each documented capability in your own words.
Do not expand broad documentation phrases into unstated details. For example, if documentation says "basic mathematical functionality", do not rewrite it as "addition, subtraction, multiplication, and division" unless those operations are explicitly listed.
Evidence must include the file path and exact line range.

[Value Quality Rules]
- value must be useful for a later formal specification, not just a short label.
- summary.value must be 2 concise sentences and about 25 to 60 words.
- Items inside arrays must use 1 or 2 complete sentences and about 15 to 50 words.
- Every value must identify the subject it describes by its ROLE - the step being performed, the capability offered, the kind of file involved - never by quoting the documentation's literal text.
- Missing values must explain what is missing and why it matters for setup, usage, API understanding, testing, or contribution.
- Avoid vague values such as "Basic math operations", "Setup instructions", "No API docs", or "Contribution guidelines". Say concretely what the step does or what capability is offered.

[Clean-Room Rules For value - MANDATORY]
The value field is the ONLY part of this report that crosses to a team that must never
see the original project. Describe behaviour; never reproduce form. Inside value:
- NEVER reproduce a shell, git, package-manager, or build command, in whole or in part.
  Describe the step: "create a feature branch before making changes", not the command
  that creates one.
- NEVER reproduce branch names, file paths, directory names, URLs, or repository names.
- NEVER reproduce a phrase in the documentation's original language. Translate the
  meaning; keep nothing of the wording.
- NEVER quote a sentence from the documentation, even translated. Restate it.
Everything you must not put in value still belongs in evidence.excerpt, which stays
verbatim and does not cross. Losing the literal wording is the point, not a defect.

[Labels]
- documented: explicitly stated in the documentation files you read
- inferred: reasonably inferred from documentation context, but not directly stated
- missing: expected information that was not found in the documentation

Do not mix documented facts with inferred facts.
If you infer something, clearly mark it as inferred.
Every finding must include evidence as an object.
Use the line numbers supplied by the index.
For documented findings, evidence must include file, line_start, line_end, and excerpt.
For inferred or missing findings, evidence must include the closest relevant file/lines if available.
If no file/line evidence exists, use null for file, line_start, line_end, and explain in excerpt.

[Field Rules]
- summary: a concrete 2-sentence summary based only on documentation.
- documentation_files_read: exact relative paths of documentation files read.
- project_purpose: concrete documented purpose statements.
- setup_and_run: each item must describe what a setup or run step accomplishes, without reproducing the command that performs it.
- api_surface: describe the role of each documented module, class, function, endpoint, or file rather than naming it.
- features: each item must describe a documented capability in your own words, not an expanded guess and not the documentation's phrasing.
- warnings: only report issues directly supported by documentation content or clearly missing expected documentation.
- open_questions: questions that remain unanswered after reading the documentation.

[Evidence Rules]
- The evidence.excerpt must contain only exact text from the referenced file lines.
- Do not paraphrase, translate, summarize, normalize whitespace, or repair typos inside evidence.excerpt.
- Do not include line-number prefixes such as "<N>:" in evidence.excerpt.
- If the tool returns line-numbered content (e.g. each line prefixed with a number and a colon), strip that prefix before writing evidence.excerpt.
- line_start and line_end must point to the actual file lines containing the excerpt.
- Do not reuse evidence from a different section.
- The excerpt MUST be the verbatim supplied text for the chosen line range — never rewrite it to a more polished or canonical form.

[Evidence Field Hard Rules]
- For label "documented" and label "inferred":
  - evidence.file MUST be a non-empty string copied verbatim from the matching <source_doc_index> item. NEVER emit "file": "".
  - evidence.line_start MUST be an integer >= 1. NEVER emit 0.
  - evidence.line_end MUST be an integer >= line_start. NEVER emit 0.
  - evidence.excerpt MUST be a non-empty string copied verbatim from the indexed item.
- For label "missing":
  - If the missing fact has no related file at all, use null (literal JSON null) for file, line_start, line_end, and put a human-readable note in excerpt.
  - If the missing fact has a related but incomplete file region, copy that region's file/line_start/line_end/excerpt from the index.
- When <source_doc_index> is provided, copy evidence directly from the matching index item — do not invent line numbers or excerpts.
- For every section entry in <source_doc_index>, the matching evidence object already has file, line_start, line_end, excerpt. Copy those values verbatim into your finding's evidence — do not paraphrase, do not change line numbers, do not summarise the excerpt.

[Final Self-Check]
- Each documented or inferred value is supported by its evidence.
- Each evidence.excerpt is copied exactly from the referenced file lines, without line-number prefixes.
- Each evidence line range is minimal and relevant.
- Each value is descriptive enough for later specification writing.
- If any item fails this check, fix it or remove it before returning.


[Output Rules]
Return ONLY valid JSON.
Do not use Markdown.
Do not add headings.
Do not add text before or after the JSON.
Replace every "..." placeholder with a real value taken from <source_doc_index>.
NEVER leave evidence.file as "" or line_start/line_end as 0 for documented or inferred findings.

Use this exact JSON schema:

{
  "summary": {
    "label": "documented | inferred | missing",
    "value": "...",
    "evidence": {
      "file": "...",
      "line_start": 1,
      "line_end": 1,
      "excerpt": "..."
    }
  },
  "documentation_files_read": ["..."],
  "project_purpose": [
    {
      "label": "documented | inferred | missing",
      "value": "...",
      "evidence": {
        "file": "...",
        "line_start": 1,
        "line_end": 1,
        "excerpt": "..."
      }
    }
  ],
  "setup_and_run": [
    {
      "label": "documented | inferred | missing",
      "value": "...",
      "evidence": {
        "file": "...",
        "line_start": 1,
        "line_end": 1,
        "excerpt": "..."
      }
    }
  ],
  "api_surface": [
    {
      "label": "documented | inferred | missing",
      "value": "...",
      "evidence": {
        "file": "...",
        "line_start": 1,
        "line_end": 1,
        "excerpt": "..."
      }
    }
  ],
  "features": [
    {
      "label": "documented | inferred | missing",
      "value": "...",
      "evidence": {
        "file": "...",
        "line_start": 1,
        "line_end": 1,
        "excerpt": "..."
      }
    }
  ],
  "warnings": [
    {
      "label": "documented | inferred | missing",
      "value": "...",
      "evidence": {
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
You are the Documentation Agent. For ONE mini task you receive a section of repository
documentation already extracted by the controller. Your only job is to interpret that
section and produce TWO fields: a label and a short value statement in English.

The controller has already:
  - Read the file and the exact line range.
  - Will attach the evidence object (file, line_start, line_end, excerpt) for you.
You do NOT decide which lines to read. You do NOT produce evidence. You do NOT call tools.

[Inputs you receive]
- output_field: which report field this entry will be appended to (e.g. project_purpose).
- kind: which schema to follow (doc_labeled or doc_open_question).
- section_text: the actual lines from the repository, already fetched. THIS IS THE ONLY
  source of truth. If a fact is not in section_text, treat it as "missing".

[Your output — exact field names]

doc_labeled (used for summary, project_purpose, setup_and_run, api_surface, features, warnings):
  {
    "label": "documented" | "inferred" | "missing",
    "value": "<1-2 concise sentences IN ENGLISH summarising the finding for the chosen output_field>"
  }
  - "documented": the section_text states the fact explicitly in prose.
  - "inferred":   the section_text strongly implies the fact even though it does not state it directly. USE THIS LIBERALLY before falling back to "missing":
        * a code snippet that imports and calls a project class/function INFERS that class/function as part of the API.
        * a runnable example or screenshot description INFERS a capability the project provides.
        * a tutorial link to an external library INFERS that the project uses that library and exposes it indirectly.
  - "missing":    the section_text does NOT support the requested output_field at all — there is no explicit statement AND nothing to infer from code or examples.

doc_open_question:
  {
    "label": "missing",
    "value": "<one specific unanswered question IN ENGLISH that the documentation does not resolve>"
  }

[Language Rule — MANDATORY]
- value MUST be written IN ENGLISH, regardless of the language of section_text.
- If section_text is in Portuguese, Spanish, German, Polish, or any other language,
  TRANSLATE the meaning into clean English before writing value. Do not keep foreign-
  language phrases inside value.
- This applies to EVERY fragment, including ones that look like names. A branch name,
  a directory name, or a quoted argument written in the source language must be
  described, not carried over. Translating the sentence around a foreign fragment
  while keeping the fragment is exactly the failure this rule exists to prevent.

[Clean-Room Rule — MANDATORY]
value crosses to a team that must never see the original project. Describe behaviour;
never reproduce form.
- NEVER reproduce a shell, git, package-manager, or build command, whole or partial.
  Write "create a feature branch before making changes", NOT the command that does it.
- NEVER reproduce branch names, file paths, directory names, URLs, or repository names.
- NEVER quote a sentence from section_text, even translated - restate it.
- Naming a widely known third-party tool or language is fine ("the Python package
  installer"). Reproducing the project's OWN names or literal text is not.
If following this makes value less literal than section_text, that is correct. The
controller attaches the verbatim excerpt separately as evidence; value carries meaning.

[Critical Field Naming Rules]
- The finding text field is named "value". NEVER name it "content", "text", "summary",
  "description", or anything else.
- Allowed top-level keys: exactly {label, value}. Nothing else.
- Do NOT add "kind", "output_field", "evidence", "task_id", "task_type", "excerpt", or
  any meta-field. The controller will add evidence for you.
- Do NOT wrap the entry inside another object like {"content": {...}}.

[Output Rules]
- Return ONLY one valid JSON object with exactly two top-level keys: label and value.
- No Markdown, no code fences, no prose before or after the JSON.
""".strip()


_KIND_TO_NARROW_SCHEMA = {
    DOC_KIND_LABELED: DocNarrowLabeledSchema,
    DOC_KIND_OPEN_QUESTION: DocNarrowOpenQuestionSchema,
}

_DOC_NARROW_SCHEMAS_BY_FIELD = {
    field: _KIND_TO_NARROW_SCHEMA[kind] for field, kind in DOC_OUTPUT_FIELDS.items()
}


class DocumentationAgent(BaseAgent):
    agent_name = "Documentation Agent"
    instruction = instruction

    def analyze(
        self,
        source_manifest: SourceManifest,
        source_doc_index: str | dict[str, Any] | None = None,
        mini_tasks: str | dict[str, Any] | list[Any] | None = None,
    ) -> str:
        log_mini_tasks("Documentation Agent", mini_tasks, phase="execution")
        documentation_files = source_manifest.documentation or []

        mini_task_list = list(iter_mini_tasks(mini_tasks))
        if not mini_task_list:
            raise ValueError(
                "Documentation Agent requires a plan: pass mini_tasks. The former single-shot "
                "fallback drove the model through source-reading tools, and there is no "
                "tool surface any more - AgentProvider is prompt to text (CLAUDE.md "
                "section 3). Source is pre-read from the plan's input_refs instead."
            )

        return self.execute_mini_tasks(
            mini_task_list=mini_task_list,
            allowed_files=documentation_files,
            source_reader=self.source_reader,
            output_field_schemas=_DOC_NARROW_SCHEMAS_BY_FIELD,
            narrow_instruction=mini_task_instruction,
            build_narrow_prompt=_build_narrow_user_prompt,
            initial_payload=_initial_payload,
            handle_narrow_result=_handle_narrow_result,
            finalize_payload=_finalize_payload,
            final_instruction=instruction,
            final_task_instruction=(
                "The controller aggregated documentation mini-task outputs into one JSON artifact. "
                "If a validation repair is requested, rewrite only that JSON artifact. "
                "Do not call tools and do not add new findings."
            ),
            final_agent_name="Documentation Agent",
            final_schema=DocAnalyzerSchema(),
            artifact_verifier=self.artifact_verifier,
            verifier_allowed_files=documentation_files,
            repo_local_path=self.source_reader.repo_path,
            recorder_scope="agents",
            recorder_sub_scope="Documentation Agent [aggregated]",
            read_all_sections=False,
            run_with_empty_sections=True,
        )


def _initial_payload() -> dict[str, Any]:
    payload: dict[str, Any] = {field: [] for field in DOC_OUTPUT_FIELDS}
    for single_field in DOC_SINGLE_FIELDS:
        payload[single_field] = None
    return payload


def _handle_narrow_result(
    aggregated: dict[str, Any],
    mini_task: dict[str, Any],
    output_field: str,
    task_id: str,
    sections: list[dict[str, Any]],
    narrow: dict[str, Any],
) -> None:
    label = narrow.get("label")
    value = narrow.get("value")
    if not isinstance(label, str) or not isinstance(value, str) or not value.strip():
        log.warning("Documentation Agent %s returned malformed narrow entry; skipping", task_id)
        return

    section = sections[0] if sections else None
    entry = _compose_entry(
        label=label,
        value=value,
        section_file=section["file"] if section else None,
        section_start=section["line_start"] if section else None,
        section_end=section["line_end"] if section else None,
        section_text=section["text"] if section else "",
    )

    if output_field in DOC_SINGLE_FIELDS:
        if aggregated[output_field] is not None:
            log.warning(
                "Documentation Agent %s produced duplicate single-entry field %r; keeping first",
                task_id,
                output_field,
            )
        else:
            aggregated[output_field] = entry
    else:
        aggregated[output_field].append(entry)


def _finalize_payload(aggregated: dict[str, Any], files_seen: set[str]) -> dict[str, Any]:
    if aggregated.get("summary") is None:
        aggregated["summary"] = _missing_entry("No summary mini task was planned for this stage.")
    aggregated["documentation_files_read"] = sorted(files_seen)
    return aggregated


def _missing_entry(reason: str) -> dict[str, Any]:
    return {
        "label": "missing",
        "value": reason,
        "evidence": {
            "file": None,
            "line_start": None,
            "line_end": None,
            "excerpt": reason,
        },
    }


def _compose_entry(
    label: str,
    value: str,
    section_file: str | None,
    section_start: int | None,
    section_end: int | None,
    section_text: str,
) -> dict[str, Any]:
    """Build the full {label, value, evidence} entry from the LLM's narrow answer + the
    section the controller already fetched. For 'missing' findings without a section,
    evidence file/line_start/line_end are set to null so the artifact verifier accepts it."""
    if section_file is None or section_start is None or section_end is None:
        if label != "missing":
            value = (
                "This documentation mini task could not be completed because no valid "
                "source section was available."
            )
        label = "missing"
        evidence = {
            "file": None,
            "line_start": None,
            "line_end": None,
            "excerpt": value,
        }
    elif not section_text.strip():
        if label != "missing":
            value = (
                "This documentation mini task could not be completed because the selected "
                "documentation line range was empty."
            )
        label = "missing"
        evidence = {
            "file": None,
            "line_start": None,
            "line_end": None,
            "excerpt": value,
        }
    else:
        evidence = {
            "file": section_file,
            "line_start": section_start,
            "line_end": section_end,
            "excerpt": section_text.strip(),
        }
    return {"label": label, "value": value, "evidence": evidence}


def _build_narrow_user_prompt(
    mini_task: dict[str, Any],
    output_field: str,
    task_id: str,
    sections: list[dict[str, Any]],
) -> str:
    kind = DOC_OUTPUT_FIELDS[output_field]
    requirements = mini_task.get("requirements") or []
    section = sections[0] if sections else None
    requirements_block = (
        "\n".join(f"- {r}" for r in requirements if isinstance(r, str) and r.strip())
        or "- (no specific requirements)"
    )
    if section is None:
        section_block = (
            "<no section text was available for this task — there is no input_ref pointing\n"
            "at an allowed file with valid line numbers. Treat this finding as missing.>"
        )
        location_label = "<no source location>"
    else:
        section_file = section["file"]
        section_start = section["line_start"]
        section_end = section["line_end"]
        section_text = section["text"]
        section_block = (
            section_text if section_text.strip() else "<the requested line range is empty>"
        )
        location_label = f"{section_file}:{section_start}-{section_end}"

    return f"""
[task_id]
{task_id}

[output_field]
{output_field}   (only metadata — DO NOT include this key in your answer)

[kind]
{kind}           (only metadata — DO NOT include this key in your answer)

[requirements]
{requirements_block}

[section_location]
{location_label}

[section_text]
\"\"\"
{section_block}
\"\"\"

Produce ONE JSON object with exactly two fields: label, value.

Rules:
- label: pick the right enum value for {kind!r}. "documented" if the section_text states the fact for {output_field!r} explicitly; "inferred" if implied; "missing" otherwise. For doc_open_question, label is always "missing".
- value: 1-2 concise sentences IN ENGLISH that summarise the finding for {output_field!r}, based ONLY on the section_text above.
- value describes BEHAVIOUR, never form. Do NOT reproduce any command, branch name, file path, directory name, URL, repository name, or source-language fragment from section_text — describe what it does instead. Do NOT quote section_text, even translated.
- Do NOT add any other keys. Do NOT include evidence — the controller will add it.
""".strip()


def build_task_instruction(
    source_manifest: SourceManifest,
    source_doc_index: str | dict[str, Any] | None = None,
    mini_tasks: str | dict[str, Any] | list[Any] | None = None,
) -> str:
    documentation_files = source_manifest.documentation or []

    documentation_list = (
        "\n".join(f"- {path}" for path in documentation_files) or "- <no documentation files found>"
    )

    doc_index = _format_source_doc_index(source_doc_index)
    mini_task_text = format_mini_tasks(mini_tasks)

    return f"""
<task>
Analyze documentation for this repository and return the structured documentation report.
</task>

<repository_metadata>
- url: {source_manifest.repo_url}
- branch: {source_manifest.branch}
- commit: {source_manifest.commit_hash}
</repository_metadata>

<documentation_files>
{documentation_list}
</documentation_files>

<source_doc_index>
{doc_index}
</source_doc_index>

<mini_tasks>
{mini_task_text}
</mini_tasks>

<constraints>
- If <mini_tasks> is provided, execute those mini tasks as the primary scope of the analysis.
- If <source_doc_index> is provided, use it as the primary source of documentation facts and do not require source-reader tools.
- If <source_doc_index> is not provided, use source reader tools to read only the exact files listed in <documentation_files>.
- If <source_doc_index> is not provided, your first response must be a source-reader tool call, preferably read_file for the first listed documentation file.
- Repository metadata is context only. Do not use the repository URL, name, branch, or commit as evidence for project behavior.
- Do not analyze code files unless documentation explicitly points to them and the file is allowed by the source reader.
- If no documentation file contains a fact, report that fact as missing instead of guessing.
- Use exact line ranges and exact evidence excerpts from tool results.
</constraints>
""".strip()


def _format_source_doc_index(source_doc_index: str | dict[str, Any] | None) -> str:
    if source_doc_index is None:
        return "<no source doc index provided>"

    if isinstance(source_doc_index, str):
        return source_doc_index

    context = build_source_doc_index_context(source_doc_index)
    return json.dumps(context, ensure_ascii=False, indent=2)

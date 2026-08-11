import json
import logging
from typing import Any

from packages.agents.base_agent import (
    BaseAgent,
)
from packages.agents.planning import format_mini_tasks, iter_mini_tasks, log_mini_tasks
from packages.modules.boundary import (
    AliasMap,
    annotate_border_review,
    neutral_report,
    scan_content_leaks,
    scan_residual_originals,
)
from packages.modules.ingesting import SourceManifest
from packages.modules.supervising.schemas import SpecNarrowMarkdownSchema

log = logging.getLogger(__name__)

instruction = """
[Agent Role]
You are the Spec Synthesizer Agent.
You aggregate verified analysis artifacts into a formal Markdown specification.

[Allowed Inputs]
- documentation_findings: what the documentation established.
- code_findings: concrete facts established from the implementation.
- behavior_findings: behaviour analysis and test candidates.

Each finding carries an `evidence_id`. The findings have already been stripped of
locations and names - that is deliberate, and there is nothing else to draw on.

[Source Of Truth]
- Use only the provided artifacts.
- Do not inspect source files directly.
- Do not call tools.
- Do not invent behavior, APIs, requirements, dependencies, or test cases.
- Do not treat recommendations as implemented behavior.

[CRITICAL - This Document Crosses A Clean-Room Boundary]
The specification you write is handed to an independent team that will implement the
system from scratch. That team must never see, and must never be able to reconstruct,
the implementation the analysis artifacts came from. You are describing a system to be
BUILT, not a system that was READ.

Therefore the specification MUST NOT contain any of the following, in any section:
- File names, file paths, directory names, or module paths.
- Class, function, method, variable, constant, or parameter names taken from the analysis.
- Verbatim source code, code fragments, signatures, or excerpts of any kind.
- Comments, docstrings, log messages, or distinctive literal strings.
- Repository URLs, project names, commit identifiers, branch names, or author names.
- Any evidence reference of the form path:line.

Instead:
- Refer to a component by the neutral label supplied in the prompt, or by its role
  ("the component that validates incoming records"). Never by its original name.
- Refer to evidence ONLY by the opaque identifiers supplied in the prompt (for example
  EV-014). Never expand one into a path or a line number, even if you can infer it.
- Describe behaviour in your own words: what happens, under what conditions, with what
  result. Never reproduce how it was written.
- Where a name is genuinely required for the specification to be usable, invent a
  descriptive one and say that it is proposed.

If you cannot describe something without naming an original, write the behaviour and add
a line beginning "BORDER-REVIEW:" explaining what could not be expressed. A thinner,
clean specification is better than a rich, contaminated one.

[Synthesis Task]
- Synthesize a formal, implementation-aware specification in Markdown.
- Separate observed behavior from inferred behavior and missing or unknown behavior.
- Carry evidence across as the opaque identifiers you were given, nothing more.
- Convert behavior report items into formal requirements and acceptance criteria.
- Convert test candidates into testable scenarios.
- Surface gaps and open questions clearly.
- If an expected behavior comes only from a recommendation, mark it as recommended, not observed.

[Mini Task Execution]
- If mini_tasks are provided, use them as the required checklist for the specification.
- Preserve mini task IDs where useful in requirement IDs, acceptance criteria, or evidence references.
- Do not skip a mini task silently. If it cannot be represented, add it to Gaps And Open Questions.

[Markdown Output Rules]
- Write concise but useful Markdown.
- Use stable section headings.
- Return ONLY Markdown.
- Do not wrap the answer in code fences.

[Required Markdown Sections]
1. Title
2. Source Snapshot
3. Scope
4. Project Purpose
5. System Overview
6. Components And Interfaces
7. Functional Requirements
8. Behavioral Requirements
9. Error Handling
10. Configuration
11. Acceptance Criteria
12. Test Candidates
13. Gaps And Open Questions
14. Evidence References

[Requirement Style]
- Use IDs like FR-001, BR-001, EH-001, AC-001, TC-001.
- For each requirement include Status, Statement, and Evidence.
- Status must be one of: observed, inferred, missing, recommended.

[Evidence Style]
- Evidence is ONLY an opaque identifier supplied in the prompt, for example `Evidence: EV-014`.
- Several identifiers may be listed together: `Evidence: EV-003, EV-009`.
- NEVER write a path, a file name, or a line number. NEVER expand an identifier.
- If no identifier was supplied, write `Evidence: not available`.

[Section Rules That Depend On The Boundary]
- Source Snapshot: name only the project to be built, using the neutral project name
  given in the prompt. No repository URL, no commit, no branch, no original name.
- Components And Interfaces: one entry per component, using its neutral label and a
  description of the ROLE it plays and the contract it offers. No original names, no
  signatures, no file layout.
- Evidence References: list the opaque identifiers the specification cites and what each
  one supports, in behavioural terms. It is a table of claims, not of locations.
""".strip()

SPEC_OUTPUT_FIELDS: tuple[str, ...] = (
    "source_snapshot",
    "scope",
    "project_purpose",
    "system_overview",
    "components_and_interfaces",
    "functional_requirements",
    "behavioral_requirements",
    "error_handling",
    "configuration",
    "acceptance_criteria",
    "test_candidates",
    "gaps_and_open_questions",
    "evidence_references",
)

_SPEC_SECTION_TITLES = {
    "source_snapshot": "Source Snapshot",
    "scope": "Scope",
    "project_purpose": "Project Purpose",
    "system_overview": "System Overview",
    "components_and_interfaces": "Components And Interfaces",
    "functional_requirements": "Functional Requirements",
    "behavioral_requirements": "Behavioral Requirements",
    "error_handling": "Error Handling",
    "configuration": "Configuration",
    "acceptance_criteria": "Acceptance Criteria",
    "test_candidates": "Test Candidates",
    "gaps_and_open_questions": "Gaps And Open Questions",
    "evidence_references": "Evidence References",
}

_NARROW_SCHEMAS = {field: SpecNarrowMarkdownSchema for field in SPEC_OUTPUT_FIELDS}

mini_task_instruction = """
[Agent Role]
You are the Spec Synthesizer Agent. For ONE mini task you receive verified analysis
artifacts and must write one or more concise Markdown fragments for the requested
specification section.

[Output Contract]
Return ONE JSON object:

{
  "items": [
    {
      "source_ref": null,
      "heading": "...",
      "markdown": "..."
    }
  ]
}

[Rules]
- Use "source_ref": null. This agent receives verified artifacts, not numbered source sections.
- markdown must be valid Markdown text, but do not wrap it in code fences.
- Use only verified artifacts in the prompt.
- Do not invent behavior, APIs, requirements, dependencies, or tests.
- If the requested section cannot be supported, write a gap/open question fragment.
- Return ONLY the JSON object. No prose outside JSON.

[Clean-Room Rules - This Fragment Crosses To An Independent Team]
- Write about a system to be BUILT, never about one that was read.
- NO file names, paths, module names, class names, function names, or parameter names
  from the analysis. NO code, signatures, comments, or distinctive literal strings.
  NO repository URL, project name, commit, or branch.
- Cite evidence ONLY as the opaque identifiers supplied in the prompt (e.g. EV-014).
  Never expand one into a path or line number. If none was supplied, write
  "Evidence: not available".
- Name components only by the neutral labels supplied in the prompt, or by their role.
- If something cannot be said without naming an original, state the behaviour and add a
  line starting "BORDER-REVIEW:" describing what had to be left out.
""".strip()


class SpecSynthesizerAgent(BaseAgent):
    agent_name = "Spec Synthesizer Agent"
    instruction = instruction

    def synthesize(
        self,
        source_manifest: SourceManifest,
        alias_map: AliasMap,
        documentation_report: str | dict[str, Any] | None = None,
        source_code_index: str | dict[str, Any] | None = None,
        code_facts_report: str | dict[str, Any] | None = None,
        behavior_report: str | dict[str, Any] | None = None,
        mini_tasks: str | dict[str, Any] | list[Any] | None = None,
    ) -> str:
        """Assemble the specification that crosses to the clean team.

        `alias_map` is required, not optional. Every report is neutralised through it
        before it reaches a prompt: telling a model not to copy file paths achieves
        nothing while the prompt is still full of file paths to copy. `source_code_index`
        is deliberately NOT forwarded - it is a map of original locations and excerpts,
        and nothing in it survives neutralisation anyway (CLAUDE.md section 2).
        """
        log_mini_tasks("Spec Synthesizer Agent", mini_tasks, phase="execution")
        mini_task_list = list(iter_mini_tasks(mini_tasks))
        if mini_task_list:
            return self._reviewed(
                self.require_content(
                    self.execute_mini_tasks(
                        mini_task_list=mini_task_list,
                        allowed_files=[],
                        source_reader=None,
                        output_field_schemas=_NARROW_SCHEMAS,
                        narrow_instruction=mini_task_instruction,
                        build_narrow_prompt=_make_narrow_prompt_builder(
                            project_name=alias_map.register_project(source_manifest.repo_url),
                            documentation_report=_neutral(documentation_report, alias_map),
                            code_facts_report=_neutral(code_facts_report, alias_map),
                            behavior_report=_neutral(behavior_report, alias_map),
                        ),
                        initial_payload=_initial_payload,
                        handle_missing_sections=None,
                        handle_narrow_error=_handle_narrow_error,
                        handle_narrow_result=_handle_narrow_result,
                        on_task_failure=_handle_task_failure,
                        finalize_payload=_make_finalizer(
                            mini_task_list, _declared_not_applicable(mini_tasks)
                        ),
                        serialize_payload=_serialize_spec_payload,
                        final_instruction=instruction,
                        final_task_instruction=(
                            "The controller aggregated specification mini-task fragments into Markdown. "
                            "If a repair is requested, rewrite only the final Markdown."
                        ),
                        final_agent_name="Spec Synthesizer Agent",
                        final_schema=None,
                        artifact_verifier=None,
                        verifier_allowed_files=[],
                        repo_local_path=source_manifest.repo_local_path,
                        recorder_scope="agents",
                        recorder_sub_scope="Spec Synthesizer Agent [aggregated]",
                        read_all_sections=False,
                        run_with_empty_sections=True,
                    )
                ),
                alias_map,
                _evidence_excerpts(documentation_report, code_facts_report, behavior_report),
            )

        task_instruction = build_task_instruction(
            project_name=alias_map.register_project(source_manifest.repo_url),
            documentation_report=_neutral(documentation_report, alias_map),
            code_facts_report=_neutral(code_facts_report, alias_map),
            behavior_report=_neutral(behavior_report, alias_map),
            mini_tasks=mini_tasks,
        )
        return self._reviewed(
            self.require_content(
                self.run(
                    instruction=instruction,
                    task_instruction=task_instruction,
                    agent_name="Spec Synthesizer Agent",
                    repo_local_path=source_manifest.repo_local_path,
                    recorder_scope="agents",
                    recorder_sub_scope="Spec Synthesizer Agent",
                )
            ),
            alias_map,
            _evidence_excerpts(documentation_report, code_facts_report, behavior_report),
        )

    def _reviewed(
        self,
        markdown: str,
        alias_map: AliasMap,
        source_texts: tuple[str, ...] = (),
    ) -> str:
        """Flag anything original that survived into the finished specification.

        Advisory only. Step 1 does not gate on this - Border does, later (Q3). The
        findings are appended to the document so a leak is visible rather than silent.
        """
        # Two independent layers. The first knows what the original was called; the
        # second knows what copied content looks like and needs no map at all. A leak
        # that defeats one has no reason to defeat the other - the documentation
        # command leak was invisible to the first and obvious to the second.
        findings = scan_residual_originals(markdown, alias_map)
        findings += scan_content_leaks(markdown, source_texts)
        if findings:
            log.warning(
                "Specification carries %d possible clean-room leak(s); marked BORDER-REVIEW",
                len(findings),
            )
        return annotate_border_review(markdown, findings)


def _evidence_excerpts(*reports: str | dict[str, Any] | None) -> tuple[str, ...]:
    """Every verbatim source line the dirty-side reports carry.

    These excerpts never cross - `neutral_report` drops them before the prompt is built.
    That is exactly what makes them a usable corpus: anything in the finished
    specification that matches one word-for-word was carried across inside an agent's
    prose, which is the one surface neutralisation deliberately preserves.

    Only excerpts with a real source location count. A `missing` finding has no file to
    quote, so the agent's own English is stored as its excerpt; feeding that back in made
    the scanner flag the specification for restating our own words, which is a false
    positive on every run that reports something missing.
    """
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            excerpt = node.get("excerpt")
            if isinstance(excerpt, str) and excerpt.strip() and node.get("file"):
                found.append(excerpt)
            for key, value in node.items():
                if key != "excerpt":
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for report in reports:
        if isinstance(report, str):
            try:
                report = json.loads(report)
            except (TypeError, ValueError):
                continue
        walk(report)
    return tuple(found)


def _neutral(
    report: str | dict[str, Any] | None,
    alias_map: AliasMap,
) -> dict[str, Any] | None:
    """Strip a dirty-side report down to what may cross the boundary."""
    if report is None:
        return None
    if isinstance(report, str):
        try:
            report = json.loads(report)
        except json.JSONDecodeError:
            log.warning("Spec Synthesizer received unparseable report text; dropping it")
            return None
    return neutral_report(report, alias_map)


def _initial_payload() -> dict[str, Any]:
    return {
        "fragments": [],
    }


def _handle_narrow_error(
    aggregated: dict[str, Any],
    mini_task: dict[str, Any],
    output_field: str,
    task_id: str,
    sections: list[dict[str, Any]],
    reason: str,
) -> None:
    aggregated["fragments"].append(
        {
            "task_id": task_id,
            "output_field": "gaps_and_open_questions",
            "heading": f"Unresolved Mini Task {task_id}",
            "markdown": (
                f"- Status: missing\n"
                f"- Statement: Mini task `{task_id}` could not produce a valid specification fragment ({reason}).\n"
                "- Evidence: not available"
            ),
        }
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
        aggregated["fragments"].append(
            {
                "task_id": task_id,
                "output_field": "gaps_and_open_questions",
                "heading": f"Unresolved Mini Task {task_id}",
                "markdown": (
                    f"- Status: missing\n"
                    f"- Statement: Mini task `{task_id}` produced no specification fragment for `{output_field}`.\n"
                    "- Evidence: not available"
                ),
            }
        )
        return

    for item in items:
        if not isinstance(item, dict):
            continue
        heading = str(
            item.get("heading") or _SPEC_SECTION_TITLES.get(output_field, output_field)
        ).strip()
        markdown = str(item.get("markdown") or "").strip()
        if not markdown:
            continue
        aggregated["fragments"].append(
            {
                "task_id": task_id,
                "output_field": output_field,
                "heading": heading,
                "markdown": markdown,
            }
        )


def _handle_task_failure(
    aggregated: dict[str, Any],
    mini_task: dict[str, Any],
    output_field: str,
    task_id: str,
    exc: Exception,
) -> None:
    """Record an exhausted mini task as a TODO instead of losing the specification.

    The stage keeps every fragment that did succeed; the gap is stated in the document
    rather than hidden, so a reader can see exactly which section needs another pass.
    """
    aggregated["fragments"].append(
        {
            "task_id": task_id,
            "output_field": output_field,
            "heading": _SPEC_SECTION_TITLES.get(output_field, output_field),
            "markdown": (
                f"- Status: missing\n"
                f"- Statement: This section was not generated. Mini task `{task_id}` "
                f"failed validation after all retries ({type(exc).__name__}).\n"
                "- Evidence: not available"
            ),
            "todo": True,
        }
    )


def _make_finalizer(
    mini_task_list: list[dict[str, Any]],
    not_applicable: dict[str, str] | None = None,
):
    """Account for every section. There are three ways one can be empty, not two.

    - **Planned but produced nothing** - a failure, and it needs fixing.
    - **Excused with a justification** - a judgement the planner made on the record, which
      a reader can weigh because the reasoning travels with it.
    - **Absent with no reason at all** - a defect. Presence does not follow reproducibility:
      every section earns at least one task or an explicit excuse.

    Collapsing these into one marker would hide a considered decision among real problems,
    or worse, dress an unexplained hole up as a decision. The first live run did exactly
    that - two core sections excused with the same templated sentence.
    """

    def _finalize_payload(aggregated: dict[str, Any], files_seen: set[str]) -> dict[str, Any]:
        produced = {
            item.get("output_field")
            for item in aggregated.get("fragments", [])
            if isinstance(item, dict)
        }
        planned = {
            task.get("output_field")
            for task in mini_task_list
            if isinstance(task, dict) and task.get("output_field") in SPEC_OUTPUT_FIELDS
        }

        for field in planned - produced:
            aggregated["fragments"].append(
                {
                    "task_id": _task_id_for(mini_task_list, field),
                    "output_field": field,
                    "heading": _SPEC_SECTION_TITLES.get(field, field),
                    "markdown": (
                        "- Status: missing\n"
                        "- Statement: This section was planned but produced no content.\n"
                        "- Evidence: not available"
                    ),
                    "todo": True,
                }
            )

        excused = not_applicable or {}
        for field in (f for f in SPEC_OUTPUT_FIELDS if f not in planned and f not in produced):
            justification = excused.get(field)
            if justification:
                # Excused on the record. Carry the reasoning through so a reader can judge
                # whether the exemption was earned rather than taking it on trust.
                aggregated["fragments"].append(
                    {
                        "task_id": "-",
                        "output_field": field,
                        "heading": _SPEC_SECTION_TITLES.get(field, field),
                        "markdown": (
                            f"- Status: not applicable\n- Statement: {justification}\n"
                            "- Evidence: not applicable"
                        ),
                        "excused": True,
                    }
                )
            else:
                aggregated["fragments"].append(
                    {
                        "task_id": "-",
                        "output_field": field,
                        "heading": _SPEC_SECTION_TITLES.get(field, field),
                        "markdown": (
                            "- Status: missing\n"
                            "- Statement: No task was planned for this section and no reason "
                            "was given. Every section needs at least one task, or an explicit "
                            "justification for its absence.\n"
                            "- Evidence: not available"
                        ),
                        "unplanned": True,
                    }
                )
        return aggregated

    return _finalize_payload


def _declared_not_applicable(mini_tasks: Any) -> dict[str, str]:
    """Pull the planner's excused sections out of the plan, field -> justification."""
    payload = mini_tasks
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return {}
    if not isinstance(payload, dict):
        return {}
    excused: dict[str, str] = {}
    for entry in payload.get("not_applicable") or []:
        if isinstance(entry, dict):
            field = entry.get("output_field")
            justification = entry.get("justification")
            if isinstance(field, str) and isinstance(justification, str) and justification.strip():
                excused[field] = justification.strip()
    return excused


def _task_id_for(mini_task_list: list[dict[str, Any]], field: str) -> str:
    for task in mini_task_list:
        if isinstance(task, dict) and task.get("output_field") == field:
            return str(task.get("task_id") or "?")
    return "?"


def _serialize_spec_payload(payload: dict[str, Any]) -> str:
    fragments = payload.get("fragments") or []
    lines: list[str] = ["# Specification", ""]

    for output_field in SPEC_OUTPUT_FIELDS:
        section_fragments = [
            item
            for item in fragments
            if isinstance(item, dict) and item.get("output_field") == output_field
        ]
        if not section_fragments:
            continue

        base_title = _SPEC_SECTION_TITLES.get(output_field, output_field)
        title = base_title
        if all(item.get("todo") for item in section_fragments):
            title = f"{title} — TODO: generation failed, section incomplete"
        elif all(item.get("excused") for item in section_fragments):
            title = f"{title} — not applicable"
        elif all(item.get("unplanned") for item in section_fragments):
            title = f"{title} — MISSING: no task planned and no justification given"
        lines.append(f"## {title}")
        lines.append("")
        for item in section_fragments:
            heading = str(item.get("heading") or "").strip()
            markdown = _strip_code_fence(str(item.get("markdown") or "").strip())
            markdown = _drop_restated_heading(markdown, base_title)
            # A fragment heading that just repeats the section is noise too.
            if (
                heading
                and not markdown.lstrip().startswith("#")
                and not _same_heading(heading, base_title)
            ):
                lines.append(f"### {heading}")
                lines.append("")
            lines.append(markdown)
            lines.append("")

    if len(lines) <= 2:
        lines.extend(
            [
                "## Gaps And Open Questions",
                "",
                "- Status: missing",
                "- Statement: No specification fragments were produced.",
                "- Evidence: not available",
                "",
            ]
        )

    return "\n".join(lines).strip() + "\n"


def _same_heading(left: str, right: str) -> bool:
    """Whether two headings say the same thing, ignoring case and punctuation."""
    return _heading_key(left) == _heading_key(right)


def _heading_key(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


def _drop_restated_heading(markdown: str, section_title: str) -> str:
    """Remove a fragment's opening heading when it just restates its section.

    The assembler always writes the section heading itself, so a fragment that opens
    with the same heading produces it twice. Only an exact restatement is dropped - a
    fragment introducing a genuine sub-topic keeps its heading.
    """
    lines = markdown.splitlines()
    if not lines or not lines[0].lstrip().startswith("#"):
        return markdown
    if not _same_heading(lines[0].lstrip("# ").strip(), section_title):
        return markdown

    remainder = lines[1:]
    while remainder and not remainder[0].strip():
        remainder.pop(0)
    return "\n".join(remainder).strip()


def _strip_code_fence(markdown: str) -> str:
    text = markdown.strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) >= 2:
            return "\n".join(lines[1:-1]).strip()
    return text


def _make_narrow_prompt_builder(
    project_name: str,
    documentation_report: dict[str, Any] | None,
    code_facts_report: dict[str, Any] | None,
    behavior_report: dict[str, Any] | None,
):
    """Build prompts that contain no original identifiers.

    The reports arriving here have already been through `neutral_report`, so their
    evidence is opaque IDs and their original locations are gone.
    """

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
        return f"""
[task_id]
{task_id}

[output_field]
{output_field}

[target_section]
{_SPEC_SECTION_TITLES.get(output_field, output_field)}

[requirements]
{requirements_block}

[project]
- name: {project_name}   (the project to be BUILT; there is no other name for it)

[documentation_findings]
{_format_artifact(documentation_report)}

[code_findings]
{_format_artifact(code_facts_report)}

[behavior_findings]
{_format_artifact(behavior_report)}

Every finding above carries an evidence_id. Cite those identifiers verbatim and never
expand one. There are no file paths or names in this prompt, and there must be none in
your answer.

Produce ONE JSON object with exactly one top-level key: items.
Each item must contain source_ref, heading, and markdown. Set source_ref to null.
""".strip()

    return _build_narrow_user_prompt


def build_task_instruction(
    project_name: str,
    documentation_report: dict[str, Any] | None = None,
    code_facts_report: dict[str, Any] | None = None,
    behavior_report: dict[str, Any] | None = None,
    mini_tasks: str | dict[str, Any] | list[Any] | None = None,
) -> str:
    return f"""
<task>
Write a formal Markdown specification for {project_name}, a system to be implemented
from this specification alone.
</task>

<project>
- name: {project_name}
</project>

<documentation_findings>
{_format_artifact(documentation_report)}
</documentation_findings>

<code_findings>
{_format_artifact(code_facts_report)}
</code_findings>

<behavior_findings>
{_format_artifact(behavior_report)}
</behavior_findings>

<mini_tasks>
{format_mini_tasks(mini_tasks)}
</mini_tasks>

<constraints>
- If <mini_tasks> is provided, use those mini tasks as the primary structure for the Markdown specification.
- Findings carry evidence_id values. Cite them verbatim; never expand one into a location.
- No file paths, no original names, no code, no repository URL. See the clean-room rules
  in the system instruction.
</constraints>
""".strip()


def _format_artifact(value: str | dict[str, Any] | None) -> str:
    if value is None:
        return "<not provided>"

    if isinstance(value, str):
        return value

    return json.dumps(value, ensure_ascii=False, indent=2)

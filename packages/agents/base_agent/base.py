import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from marshmallow import Schema

from packages.agents.base_agent.client import ChatClient, ChatResponse, build_client
from packages.modules.skills.reading import Reader, ReadingError
from packages.modules.supervising import ArtifactPolicy, Supervisor
from packages.modules.supervising.policies import BaseSupervisorPolicy
from packages.modules.supervising.utils.diagnostics import IterationRecorder, NullRecorder
from packages.modules.supervising.verifiers import ArtifactVerifier

DEFAULT_MAX_VALIDATION_RETRIES = 6

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentConfig:
    """How one agent reaches its model.

    **This is the single place a shared agent field is declared.** Add a field here and
    to `BaseAgent.__init__`, and every agent has it - the children define no constructor
    of their own. The tool-era fields are gone with the tool surface: `base_url` (agents
    no longer address a provider host), `max_tool_rounds`, `max_tool_calls`, and
    `max_read_file_lines`, which only ever sized the source-reader tool.
    """

    model: str
    profile_path: str | None = None
    retry_profile_path: str | None = None
    max_validation_retries: int = DEFAULT_MAX_VALIDATION_RETRIES


class BaseAgent:
    """
    Shared template for all LLM-backed agents - the thick base (CLAUDE.md section 5).

    Children stay thin: they declare `agent_name`, `instruction`, and the one method
    that does their job. They do **not** define `__init__`. Construction, model wiring,
    source access and the artifact verifier all live here, so a new shared field is
    added in one place rather than five.

    Model access is injected: pass `chat_client` to run against a stub, or leave it out
    to go through AgentProvider.
    """

    agent_name = "Base Agent"
    instruction = ""

    def __init__(
        self,
        model: str,
        profile_path: str | None = None,
        retry_profile_path: str | None = None,
        max_validation_retries: int = DEFAULT_MAX_VALIDATION_RETRIES,
        chat_client: ChatClient | None = None,
        source_reader: Reader | None = None,
        alias_map: Any | None = None,
    ):
        self.config = AgentConfig(
            model=model,
            profile_path=profile_path,
            retry_profile_path=retry_profile_path,
            max_validation_retries=max_validation_retries,
        )
        self.chat_client = build_client(
            model=model,
            profile_path=profile_path,
            retry_profile_path=retry_profile_path,
            client=chat_client,
        )
        self.max_validation_retries = self.config.max_validation_retries

        # Agents that read source get a reader and a verifier; the ones that only
        # reshape artifacts (planning, spec synthesis) get None and never touch them.
        self.source_reader = source_reader
        self.artifact_verifier = ArtifactVerifier(source_reader) if source_reader else None

        # Dirty-side only. A plan cites opaque evidence ids, so the controller needs the
        # map to recover a file and line range before it can pre-read anything.
        self.alias_map = alias_map

    def build_messages(
        self,
        task_instruction: str,
        instruction: str | None = None,
    ) -> list[dict[str, Any]]:
        return [
            {"role": "system", "content": instruction or self.instruction},
            {"role": "user", "content": task_instruction},
        ]

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        attempt: int = 0,
    ) -> ChatResponse:
        return self.chat_client.chat(messages, tools=tools, attempt=attempt)

    def chat_content(self, messages: list[dict[str, Any]], attempt: int = 0) -> str:
        """Ask for one reply. `attempt` > 0 means this is a repair turn.

        The supervisor passes the retry number so the client can warm the temperature;
        at 0 the model just reproduces the same invalid answer.
        """
        return self.chat(messages, tools=[], attempt=attempt).content

    def require_content(self, content: str) -> str:
        content = content.strip()
        if not content:
            log.error("%s returned empty content", self.agent_name)
            raise ValueError(f"{self.agent_name} returned empty content")
        return content

    def supervise_content(
        self,
        content: str,
        messages: list[dict[str, Any]] | None = None,
        agent_name: str | None = None,
        schema: Schema | None = None,
        artifact_verifier: ArtifactVerifier | None = None,
        verifier_allowed_files: list[str] | None = None,
        supervisor_policy: BaseSupervisorPolicy | None = None,
        supervisor_context: dict[str, Any] | None = None,
        repo_local_path: str | None = None,
        recorder_scope: str = "agents",
        recorder_sub_scope: str | None = None,
    ) -> str:
        agent_name = agent_name or self.agent_name
        base_messages = list(messages or [])
        recorder = (
            IterationRecorder.for_repo(
                repo_local_path=repo_local_path,
                scope=recorder_scope,
                sub_scope=recorder_sub_scope or agent_name,
            )
            if repo_local_path
            else NullRecorder()
        )
        return self._finalize(
            agent_name=agent_name,
            messages=[*base_messages, {"role": "assistant", "content": content}],
            content=content,
            schema=schema,
            artifact_verifier=artifact_verifier,
            verifier_allowed_files=verifier_allowed_files,
            supervisor_policy=supervisor_policy,
            supervisor_context=supervisor_context,
            recorder=recorder,
            base_len=len(base_messages),
        )

    def execute_mini_tasks(
        self,
        *,
        mini_task_list: list[dict[str, Any]],
        allowed_files: list[str],
        source_reader: Reader | None,
        output_field_schemas: Mapping[str, type[Schema]],
        narrow_instruction: str,
        build_narrow_prompt: Callable[[dict[str, Any], str, str, list[dict[str, Any]]], str],
        initial_payload: Callable[[], dict[str, Any]],
        handle_narrow_result: Callable[
            [dict[str, Any], dict[str, Any], str, str, list[dict[str, Any]], dict[str, Any]],
            None,
        ],
        finalize_payload: Callable[[dict[str, Any], set[str]], dict[str, Any]],
        final_instruction: str,
        final_task_instruction: str,
        final_agent_name: str,
        final_schema: Schema | None,
        artifact_verifier: ArtifactVerifier | None,
        verifier_allowed_files: list[str],
        repo_local_path: str | None,
        serialize_payload: Callable[[Any], str] | None = None,
        handle_missing_sections: Callable[
            [dict[str, Any], dict[str, Any], str, str],
            None,
        ]
        | None = None,
        handle_narrow_error: Callable[
            [dict[str, Any], dict[str, Any], str, str, list[dict[str, Any]], str],
            None,
        ]
        | None = None,
        on_task_failure: Callable[
            [dict[str, Any], dict[str, Any], str, str, Exception],
            None,
        ]
        | None = None,
        read_all_sections: bool = True,
        run_with_empty_sections: bool = False,
        recorder_scope: str = "agents",
        recorder_sub_scope: str | None = None,
    ) -> str:
        """Run each mini task and aggregate the results.

        `on_task_failure` decides what an exhausted mini task costs. Without it, one
        failing task aborts the whole stage and every fragment already produced is
        thrown away - which is what happened on the first verification run, where task
        4 of 13 destroyed a specification whose first three sections were fine. Supply
        it to keep the good fragments and record the failure instead.
        """
        aggregated = initial_payload()
        files_seen: set[str] = set()
        allowed_files_set = set(allowed_files)

        for task_index, mini_task in enumerate(mini_task_list, start=1):
            task_id = str(mini_task.get("task_id") or f"task-{task_index}")
            output_field = mini_task.get("output_field")

            if not isinstance(output_field, str) or output_field not in output_field_schemas:
                log.warning(
                    "%s skipping mini task %s: unknown output_field %r",
                    final_agent_name,
                    task_id,
                    output_field,
                )
                continue

            log.info(
                "%s starting mini task %d/%d -> id=%s output_field=%s",
                final_agent_name,
                task_index,
                len(mini_task_list),
                task_id,
                output_field,
            )

            sections = (
                self._extract_mini_task_sections(
                    source_reader=source_reader,
                    mini_task=mini_task,
                    allowed_files=allowed_files_set,
                    read_all_sections=read_all_sections,
                )
                if source_reader is not None
                else []
            )

            if not sections and not run_with_empty_sections:
                if handle_missing_sections is not None:
                    handle_missing_sections(aggregated, mini_task, output_field, task_id)
                continue

            for section in sections:
                files_seen.add(section["file"])

            try:
                raw = self.run(
                    instruction=narrow_instruction,
                    task_instruction=build_narrow_prompt(
                        mini_task,
                        output_field,
                        task_id,
                        sections,
                    ),
                    agent_name=f"{final_agent_name} [{task_id} -> {output_field}]",
                    schema=output_field_schemas[output_field](),
                    artifact_verifier=None,
                    verifier_allowed_files=None,
                    repo_local_path=repo_local_path,
                )
            except Exception as exc:
                if on_task_failure is None:
                    raise
                log.error(
                    "%s mini task %s exhausted its retries (%s); continuing with the rest",
                    final_agent_name,
                    task_id,
                    type(exc).__name__,
                )
                on_task_failure(aggregated, mini_task, output_field, task_id, exc)
                continue

            try:
                narrow = json.loads(raw)
            except json.JSONDecodeError:
                log.warning(
                    "%s mini task %s returned non-JSON after supervision", final_agent_name, task_id
                )
                if handle_narrow_error is not None:
                    handle_narrow_error(
                        aggregated,
                        mini_task,
                        output_field,
                        task_id,
                        sections,
                        "non_json",
                    )
                continue

            if not isinstance(narrow, dict):
                log.warning("%s mini task %s returned non-object output", final_agent_name, task_id)
                if handle_narrow_error is not None:
                    handle_narrow_error(
                        aggregated,
                        mini_task,
                        output_field,
                        task_id,
                        sections,
                        "non_object",
                    )
                continue

            handle_narrow_result(
                aggregated,
                mini_task,
                output_field,
                task_id,
                sections,
                narrow,
            )

            log.info(
                "%s finished mini task %d/%d -> id=%s",
                final_agent_name,
                task_index,
                len(mini_task_list),
                task_id,
            )

        final_payload = finalize_payload(aggregated, files_seen)
        content = (
            serialize_payload(final_payload)
            if serialize_payload is not None
            else json.dumps(final_payload, ensure_ascii=False, indent=2)
        )
        supervision_messages = self.build_messages(
            task_instruction=final_task_instruction,
            instruction=final_instruction,
        )
        return self.supervise_content(
            content=content,
            messages=supervision_messages,
            agent_name=final_agent_name,
            schema=final_schema,
            artifact_verifier=artifact_verifier,
            verifier_allowed_files=verifier_allowed_files,
            repo_local_path=repo_local_path,
            recorder_scope=recorder_scope,
            recorder_sub_scope=recorder_sub_scope or f"{final_agent_name} [aggregated]",
        )

    def run(
        self,
        task_instruction: str,
        instruction: str | None = None,
        agent_name: str | None = None,
        schema: Schema | None = None,
        artifact_verifier: ArtifactVerifier | None = None,
        verifier_allowed_files: list[str] | None = None,
        supervisor_policy: BaseSupervisorPolicy | None = None,
        supervisor_context: dict[str, Any] | None = None,
        repo_local_path: str | None = None,
        recorder_scope: str = "agents",
        recorder_sub_scope: str | None = None,
    ) -> str:
        """One exchange with the model, then supervision.

        This used to be a tool-calling loop. AgentProvider is prompt to text and cannot
        return tool calls (CLAUDE.md section 3), and the loop was already a single pass
        on the live path: it asked once, got no tool calls, and finalised. Removing it
        changes nothing about what runs, only how much code says so.

        Source is pre-read deterministically by the caller and passed in the prompt.
        """
        agent_name = agent_name or self.agent_name

        recorder = (
            IterationRecorder.for_repo(
                repo_local_path=repo_local_path,
                scope=recorder_scope,
                sub_scope=recorder_sub_scope or agent_name,
            )
            if repo_local_path
            else NullRecorder()
        )

        messages = self.build_messages(task_instruction, instruction=instruction)
        response = self.chat(messages)
        messages.append(response.message)

        return self._finalize(
            agent_name=agent_name,
            messages=messages,
            content=response.content,
            schema=schema,
            artifact_verifier=artifact_verifier,
            verifier_allowed_files=verifier_allowed_files,
            supervisor_policy=supervisor_policy,
            supervisor_context=supervisor_context,
            recorder=recorder,
        )

    def _finalize(
        self,
        agent_name: str,
        messages: list[dict[str, Any]],
        content: str,
        schema: Schema | None,
        artifact_verifier: ArtifactVerifier | None,
        verifier_allowed_files: list[str] | None,
        supervisor_policy: BaseSupervisorPolicy | None,
        supervisor_context: dict[str, Any] | None,
        recorder: Any,
        base_len: int | None = None,
    ) -> str:
        policy = supervisor_policy
        if policy is None and (schema is not None or artifact_verifier is not None):
            policy = ArtifactPolicy(
                name=agent_name,
                schema=schema,
                verifier=artifact_verifier,
            )

        if policy is None:
            return content

        context = dict(supervisor_context or {})
        context.setdefault("allowed_files", verifier_allowed_files or [])

        supervisor = Supervisor(
            repair_callback=self.chat_content,
            max_validation_retries=self.max_validation_retries,
        )
        return supervisor.supervise(
            content=content,
            messages=messages,
            policy=policy,
            context=context,
            recorder=recorder,
            base_len=base_len if base_len is not None else max(0, len(messages) - 1),
        )

    def _resolve_ref(self, ref: dict[str, Any]) -> tuple[str, int | None, int | None] | None:
        """Turn one neutral input_ref into a real location, or None if it cannot be.

        An unresolvable id is skipped rather than repaired. There is no honest way to
        guess which evidence a planner meant, and inventing one would fabricate the very
        thing the boundary protects.
        """
        evidence_id = ref.get("evidence_id")
        if not isinstance(evidence_id, str) or not evidence_id.strip():
            log.warning("%s mini-task ref cites no evidence_id; skipping", self.agent_name)
            return None
        if self.alias_map is None:
            log.warning(
                "%s cannot resolve %s: no alias map was supplied to this agent",
                self.agent_name,
                evidence_id,
            )
            return None
        location = self.alias_map.location_for(evidence_id)
        if location is None:
            log.warning(
                "%s mini-task cites unknown evidence %s; skipping", self.agent_name, evidence_id
            )
        return location

    def _extract_mini_task_sections(
        self,
        *,
        source_reader: Reader,
        mini_task: dict[str, Any],
        allowed_files: set[str],
        read_all_sections: bool,
    ) -> list[dict[str, Any]]:
        sections: list[dict[str, Any]] = []
        for ref in mini_task.get("input_refs") or []:
            if not isinstance(ref, dict):
                continue

            # The plan is neutral: it cites an opaque id, never a location. Resolving it
            # is the dirty-side half of the boundary, and it happens here so the executing
            # agent still receives real source without the plan ever naming a file.
            resolved = self._resolve_ref(ref)
            if resolved is None:
                continue
            file_path, line_start, line_end = resolved

            if (
                file_path not in allowed_files
                or not isinstance(line_start, int)
                or not isinstance(line_end, int)
                or line_start < 1
                or line_end < line_start
            ):
                continue
            try:
                text = source_reader.read_lines(file_path, line_start, line_end)
            except (FileNotFoundError, OSError, ReadingError):
                log.warning(
                    "%s could not read mini-task source %s:%s-%s",
                    self.agent_name,
                    file_path,
                    line_start,
                    line_end,
                )
                continue
            sections.append(
                {
                    "source_ref": len(sections) + 1,
                    "ref_source": ref.get("source"),
                    "file": file_path,
                    "line_start": line_start,
                    "line_end": line_end,
                    "text": text,
                }
            )
            if not read_all_sections:
                break
        return sections

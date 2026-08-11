import json
import logging
from collections.abc import Iterable
from typing import Any

log = logging.getLogger(__name__)


def format_planning_input(value: str | dict[str, Any] | list[Any] | None) -> str:
    if value is None:
        return "<not provided>"

    if isinstance(value, str):
        return value

    return json.dumps(value, ensure_ascii=False, indent=2)


def format_mini_tasks(mini_tasks: str | dict[str, Any] | list[Any] | None) -> str:
    if mini_tasks is None:
        return "<no mini tasks provided>"

    if isinstance(mini_tasks, str):
        return mini_tasks

    return json.dumps(mini_tasks, ensure_ascii=False, indent=2)


def iter_mini_tasks(
    mini_tasks: str | dict[str, Any] | list[Any] | None,
) -> Iterable[dict[str, Any]]:
    if mini_tasks is None:
        return []

    payload: Any = mini_tasks
    if isinstance(mini_tasks, str):
        try:
            payload = json.loads(mini_tasks)
        except json.JSONDecodeError:
            return []

    if isinstance(payload, dict) and isinstance(payload.get("mini_tasks"), list):
        return [item for item in payload["mini_tasks"] if isinstance(item, dict)]

    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    return []


def log_mini_tasks(
    agent_name: str, mini_tasks: str | dict[str, Any] | list[Any] | None, phase: str
) -> None:
    tasks = list(iter_mini_tasks(mini_tasks))
    if not tasks:
        log.info("%s mini tasks -> none (%s)", agent_name, phase)
        return

    log.info("%s mini tasks -> count=%d phase=%s", agent_name, len(tasks), phase)
    # "queued" for execution-phase listing makes it obvious this is the pre-loop dump
    # of what *will be* executed, not what is happening right now. "planned" is the
    # corresponding verb when the planner just emitted the plan.
    verb = "queued" if "exec" in phase else "planned"
    for index, task in enumerate(tasks, start=1):
        log.info(
            "%s %s mini task %d/%d -> id=%s type=%s output=%s refs=%d",
            agent_name,
            verb,
            index,
            len(tasks),
            task.get("task_id", "<missing>"),
            task.get("task_type", "<missing>"),
            task.get("output_field", "<missing>"),
            len(task.get("input_refs") or []),
        )

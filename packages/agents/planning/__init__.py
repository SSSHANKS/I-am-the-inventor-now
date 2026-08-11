"""Planning package.

Creates small mini tasks from source indexes and previous reports. Also provides
helpers for formatting, reading, and logging those tasks. Plan checks live in
packages.modules.supervising.
"""

from packages.agents.planning.utils.common import (
    format_mini_tasks,
    format_planning_input,
    iter_mini_tasks,
    log_mini_tasks,
)


def __getattr__(name: str):
    if name in {"PlanningAgent", "build_task_instruction"}:
        from packages.agents.planning.planner import PlanningAgent, build_task_instruction

        exports = {
            "PlanningAgent": PlanningAgent,
            "build_task_instruction": build_task_instruction,
        }
        return exports[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "PlanningAgent",
    "build_task_instruction",
    "format_mini_tasks",
    "format_planning_input",
    "iter_mini_tasks",
    "log_mini_tasks",
]

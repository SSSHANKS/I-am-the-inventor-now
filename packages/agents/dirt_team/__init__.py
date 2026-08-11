from packages.agents.dirt_team.code_behavior_agent import BehaviorAnalyzerAgent
from packages.agents.dirt_team.code_facts_agent import CodeFactsAgent
from packages.agents.dirt_team.documentation_agent import DocumentationAgent
from packages.agents.dirt_team.plan_judge_agent import PlanJudgeAgent
from packages.agents.dirt_team.spec_synthesizer_agent import SpecSynthesizerAgent


def __getattr__(name: str):
    if name == "PlanningAgent":
        from packages.agents.planning import PlanningAgent

        return PlanningAgent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "BehaviorAnalyzerAgent",
    "CodeFactsAgent",
    "DocumentationAgent",
    "PlanJudgeAgent",
    "PlanningAgent",
    "SpecSynthesizerAgent",
]

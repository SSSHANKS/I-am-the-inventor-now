"""The planner/judge loop: draft, critique, revise, keep the best neutral version.

Shape of one round:

    planner drafts  ->  neutrality GATE  ->  judge scores + says what to change
                             |                          |
                        scrub & recheck            scrub feedback
                             |                          |
                        rejected if still      goes back to the planner
                        leaking after scrub

Two rules the loop exists to enforce, both from the Step 1.5 gate:

- **Neutrality is a gate, never a score.** A leaking plan is disqualified no matter how
  well it scores elsewhere; only neutral plans get ranked. If it were a fourth summed
  dimension, a strong plan could buy its way past a leak.
- **Judge feedback is scrubbed before it returns.** The judge reads the original, so its
  raw words name original things. Feeding those back unscrubbed would have the loop
  manufacture the very leak the gate then catches.

Bounded by construction: `MAX_ROUNDS` model rounds, then the best neutral version wins even
if the judge still has notes. Every round is real calls against a free quota.
"""

import logging
from dataclasses import dataclass, field
from typing import Any

from packages.modules.boundary import (
    AliasMap,
    find_residual_originals,
    scrub_identifiers,
)

log = logging.getLogger(__name__)

#: Hard cap on planner/judge rounds. Fixed, not configurable: an unbounded "until perfect"
#: loop is exactly the failure mode this design exists to avoid.
MAX_ROUNDS = 3


@dataclass
class PlanAttempt:
    """One drafted plan and what became of it."""

    round_number: int
    plan: str
    neutral: bool
    score: int = 0
    judgement: dict[str, Any] = field(default_factory=dict)
    leaks: list[str] = field(default_factory=list)
    scrubbed: bool = False

    @property
    def rankable(self) -> bool:
        return self.neutral


@dataclass
class PlanOutcome:
    """What the loop settled on, and how it got there."""

    plan: str
    score: int
    rounds_used: int
    attempts: list[PlanAttempt]
    border_review: list[str] = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        """True when no version passed the gate cleanly and we shipped a scrubbed one."""
        return bool(self.border_review)


def enforce_neutrality(plan: str, alias_map: AliasMap) -> tuple[str, bool, list[str], bool]:
    """Gate one plan. Returns (plan, neutral, leaks, was_scrubbed).

    Scrub-then-recheck: a first-pass leak is not fatal, because scrubbing is deterministic
    and usually fixes it. Only a plan still leaking *after* scrubbing is rejected.
    """
    leaks = find_residual_originals(plan, alias_map)
    if not leaks:
        return plan, True, [], False

    log.warning("Plan leaked %d original(s); scrubbing and re-checking", len(leaks))
    scrubbed = scrub_identifiers(plan, alias_map)
    remaining = find_residual_originals(scrubbed, alias_map)
    return scrubbed, not remaining, remaining, True


def select_best(attempts: list[PlanAttempt]) -> PlanAttempt | None:
    """Highest-scoring neutral attempt; ties go to the earliest.

    Earliest wins a tie because it cost fewer calls, and because later rounds tend to
    drift rather than improve once the obvious corrections are made.
    """
    rankable = [a for a in attempts if a.rankable]
    if not rankable:
        return None
    return max(rankable, key=lambda a: (a.score, -a.round_number))


def run_plan_loop(
    *,
    draft: Any,
    judge: Any,
    alias_map: AliasMap,
    stage: str,
    max_rounds: int = MAX_ROUNDS,
) -> PlanOutcome:
    """Drive the loop.

    `draft(feedback)` returns a plan string; `judge(plan)` returns a judgement dict with
    scores and actions. Both are callables so this stays testable without a model.
    """
    attempts: list[PlanAttempt] = []
    feedback: list[str] = []

    for round_number in range(1, max_rounds + 1):
        plan = draft(feedback)
        plan, neutral, leaks, was_scrubbed = enforce_neutrality(plan, alias_map)

        attempt = PlanAttempt(
            round_number=round_number,
            plan=plan,
            neutral=neutral,
            leaks=leaks,
            scrubbed=was_scrubbed,
        )

        if neutral:
            judgement = judge(plan)
            attempt.judgement = judgement
            attempt.score = judgement.get("_total_score", 0)
        else:
            log.error(
                "Round %d still leaks after scrubbing (%d finding(s)); not ranked",
                round_number,
                len(leaks),
            )

        attempts.append(attempt)
        log.info(
            "Plan round %d/%d -> neutral=%s score=%d",
            round_number,
            max_rounds,
            neutral,
            attempt.score,
        )

        if round_number == max_rounds:
            break

        feedback = _feedback_for_next_round(attempt, alias_map)
        if not feedback:
            log.info("Judge raised nothing actionable; stopping at round %d", round_number)
            break

    return _settle(attempts, stage)


def _feedback_for_next_round(attempt: PlanAttempt, alias_map: AliasMap) -> list[str]:
    """Turn a judgement into planner-safe instructions.

    Everything here crosses back into a prompt that produces a neutral artifact, so every
    line is scrubbed - the judge can see originals, its words cannot carry them.
    """
    if not attempt.neutral:
        return [
            "Your previous plan named something from the original. Cite evidence ids and "
            "describe things by role only."
        ]

    judgement = attempt.judgement or {}
    lines: list[str] = []

    objection = judgement.get("strongest_objection")
    if isinstance(objection, str) and objection.strip():
        lines.append(f"Strongest objection: {objection.strip()}")

    for action in judgement.get("actions") or []:
        if isinstance(action, str) and action.strip():
            lines.append(action.strip())

    return [scrub_identifiers(line, alias_map) for line in lines]


def _settle(attempts: list[PlanAttempt], stage: str) -> PlanOutcome:
    best = select_best(attempts)
    if best is not None:
        log.info(
            "Plan for %s settled on round %d with score %d/%d",
            stage,
            best.round_number,
            best.score,
            15,
        )
        return PlanOutcome(
            plan=best.plan,
            score=best.score,
            rounds_used=len(attempts),
            attempts=attempts,
        )

    # Nothing passed the gate. Degrade visibly rather than failing the stage: an
    # unusable-but-flagged plan is more useful than no plan, and the marker makes the
    # problem impossible to miss.
    fallback = attempts[-1] if attempts else PlanAttempt(0, "", False)
    notes = [
        f"BORDER-REVIEW: no plan for stage {stage!r} passed the neutrality gate in "
        f"{len(attempts)} round(s); shipping the scrubbed final attempt.",
        *fallback.leaks,
    ]
    log.error("No neutral plan for %s after %d round(s)", stage, len(attempts))
    return PlanOutcome(
        plan=fallback.plan,
        score=0,
        rounds_used=len(attempts),
        attempts=attempts,
        border_review=notes,
    )

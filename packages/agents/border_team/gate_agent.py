"""Border Gate: scan crossing artifacts, ask an LLM about soft findings, enforce.

Hard leaks (NAME-SHAPED, VERBATIM, paths, lifted prose) fail without a model call.
Soft leaks (DESCRIPTIVE / UNCERTAIN only) are adjudicated by the same BaseAgent
transport every other IATIN agent uses — AgentProvider → Gemini.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from typing import Any

from packages.agents.base_agent import BaseAgent
from packages.modules.border import (
    ADJUDICATED_POLICY,
    DECISION_DISMISS,
    DECISION_FAIL,
    BorderGateError,
    BorderVerdict,
    evaluate_crossing_artifacts,
    evidence_excerpts,
)
from packages.modules.border.gate import BorderFindingRecord
from packages.modules.boundary import AliasMap
from packages.modules.storing import Storage
from packages.modules.supervising.schemas.border import (
    BorderAdjudicationSchema,
    BorderVerdictSchema,
)

log = logging.getLogger(__name__)

ADJUDICATOR_INSTRUCTION = """
[Agent Role]
You are the Border Adjudicator. You sit between Dirty (which read the original) and
Clean (which must never see it). Scanners have already flagged candidate leaks. Your
job is ONLY to judge the SOFT findings you are given.

[Hard vs soft]
Hard findings never reach you. Soft findings are DESCRIPTIVE or UNCERTAIN readings —
ordinary English that happens to match a known original word, or an ambiguous command
shape. You decide whether each one is a real leak or a false positive.

[Decision rules]
- decision "fail": the flagged text still carries something from the original. Clean
  must not receive this artifact.
- decision "dismiss": the match is ordinary language / coincidence, not a leak. You
  must explain why in rationale.
- When unsure, choose "fail". Doubt never clears a flag.
- Never dismiss because the leak "looks minor" or "the Clean team could rewrite it".
- Never invent findings. Only decide on the finding_id values supplied.
- Decide on EVERY finding_id exactly once.

[Output]
Return ONLY JSON matching the schema. No markdown fences.
""".strip()


class BorderGateAgent(BaseAgent):
    agent_name = "Border Gate"
    instruction = ADJUDICATOR_INSTRUCTION

    def review(
        self,
        *,
        alias_map: AliasMap,
        specification: str,
        documentation_report: str | dict[str, Any] | None = None,
        code_facts_report: str | dict[str, Any] | None = None,
        behavior_report: str | dict[str, Any] | None = None,
        plans: dict[str, str] | None = None,
        evidence_catalogue: dict[str, Any] | str | None = None,
        neutral_manifest: dict[str, Any] | str | None = None,
        source_texts: tuple[str, ...] | list[str] | None = None,
        policy: str = ADJUDICATED_POLICY,
        repo_local_path: str | None = None,
    ) -> BorderVerdict:
        """Scan, adjudicate soft findings via the model, return a structured verdict."""
        corpus = (
            tuple(source_texts)
            if source_texts is not None
            else evidence_excerpts(documentation_report, code_facts_report, behavior_report)
        )

        def adjudicate(soft: list[BorderFindingRecord]) -> list[BorderFindingRecord]:
            return self._adjudicate_soft(soft, repo_local_path=repo_local_path)

        verdict = evaluate_crossing_artifacts(
            alias_map=alias_map,
            specification=specification,
            plans=plans,
            evidence_catalogue=evidence_catalogue,
            neutral_manifest=neutral_manifest,
            source_texts=corpus,
            policy=policy,
            adjudicate=adjudicate,
        )
        log.info(
            "%s -> status=%s failing=%d artifacts=%s",
            self.agent_name,
            verdict.status,
            verdict.finding_count,
            ",".join(verdict.artifacts_reviewed) or "<none>",
        )
        return verdict

    def enforce(
        self,
        *,
        storage: Storage,
        alias_map: AliasMap,
        specification: str,
        documentation_report: str | dict[str, Any] | None = None,
        code_facts_report: str | dict[str, Any] | None = None,
        behavior_report: str | dict[str, Any] | None = None,
        plans: dict[str, str] | None = None,
        evidence_catalogue: dict[str, Any] | str | None = None,
        neutral_manifest: dict[str, Any] | str | None = None,
        source_texts: tuple[str, ...] | list[str] | None = None,
        policy: str = ADJUDICATED_POLICY,
        raise_on_fail: bool = True,
        repo_local_path: str | None = None,
    ) -> BorderVerdict:
        """Review, store `border_verdict.json`, and optionally refuse a failed run."""
        verdict = self.review(
            alias_map=alias_map,
            specification=specification,
            documentation_report=documentation_report,
            code_facts_report=code_facts_report,
            behavior_report=behavior_report,
            plans=plans,
            evidence_catalogue=evidence_catalogue,
            neutral_manifest=neutral_manifest,
            source_texts=source_texts,
            policy=policy,
            repo_local_path=repo_local_path,
        )
        storage.save_artifact("border_verdict.json", verdict.to_dict(), BorderVerdictSchema())
        storage.save_private("border_verdict.json", verdict.to_dict())
        if raise_on_fail and not verdict.passed:
            raise BorderGateError(verdict)
        return verdict

    def _adjudicate_soft(
        self,
        soft: list[BorderFindingRecord],
        *,
        repo_local_path: str | None,
    ) -> list[BorderFindingRecord]:
        if not soft:
            return soft

        content = self.run(
            task_instruction=_build_adjudication_prompt(soft),
            agent_name=f"{self.agent_name} [adjudicate]",
            schema=BorderAdjudicationSchema(),
            repo_local_path=repo_local_path,
            recorder_scope="border",
            recorder_sub_scope="adjudicate",
        )
        decisions = _parse_decisions(content)
        return _apply_decisions(soft, decisions)


def load_plan_artifacts(storage: Storage, plan_files: list[str]) -> dict[str, str]:
    """Read named plan files from a run directory as text for content scanning."""
    loaded: dict[str, str] = {}
    for name in plan_files:
        try:
            payload = storage.read_json(name)
        except Exception:
            log.debug("Border could not load plan artifact %s", name, exc_info=True)
            continue
        loaded[name] = json.dumps(payload, ensure_ascii=False)
    return loaded


def _build_adjudication_prompt(soft: list[BorderFindingRecord]) -> str:
    findings_payload = [
        {
            "finding_id": record.finding_id,
            "artifact": record.artifact,
            "kind": record.kind,
            "original": record.original,
            "alias": record.alias,
            "classifications": list(record.classifications),
            "occurrence_count": record.occurrence_count,
            "examples": list(record.examples),
            "summary": record.summary,
        }
        for record in soft
    ]
    return f"""
<border_findings>
{json.dumps(findings_payload, ensure_ascii=False, indent=2)}
</border_findings>

Adjudicate every finding_id above. Return JSON:
{{
  "decisions": [
    {{"finding_id": "BF-001", "decision": "fail"|"dismiss", "rationale": "..."}}
  ]
}}
""".strip()


def _parse_decisions(content: str) -> dict[str, dict[str, str]]:
    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        log.warning("Border adjudicator returned unparseable JSON; failing all soft findings")
        return {}
    if not isinstance(payload, dict):
        return {}
    decisions: dict[str, dict[str, str]] = {}
    for item in payload.get("decisions") or []:
        if not isinstance(item, dict):
            continue
        finding_id = item.get("finding_id")
        decision = item.get("decision")
        if not isinstance(finding_id, str) or decision not in {DECISION_FAIL, DECISION_DISMISS}:
            continue
        rationale = item.get("rationale")
        decisions[finding_id] = {
            "decision": decision,
            "rationale": rationale if isinstance(rationale, str) else "",
        }
    return decisions


def _apply_decisions(
    soft: list[BorderFindingRecord],
    decisions: dict[str, dict[str, str]],
) -> list[BorderFindingRecord]:
    applied: list[BorderFindingRecord] = []
    for record in soft:
        choice = decisions.get(record.finding_id)
        if choice is None:
            applied.append(
                replace(
                    record,
                    decision=DECISION_FAIL,
                    rationale="adjudicator omitted this finding_id",
                )
            )
            continue
        applied.append(
            replace(
                record,
                decision=choice["decision"],
                rationale=choice["rationale"] or None,
            )
        )
    return applied

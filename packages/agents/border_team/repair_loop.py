"""Border ↔ Dirty repair loop.

When Border refuses a specification, Dirty rewrites the leaking passages and Border
judges again. Hard stop after ``max_repairs`` failed attempts so a stubborn leak still
exits non-zero instead of burning quota forever.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

from packages.agents.border_team.gate_agent import BorderGateAgent
from packages.modules.border import BorderGateError, BorderVerdict, strip_border_review_section
from packages.modules.boundary import AliasMap, scrub_identifiers
from packages.modules.storing import Storage
from packages.modules.supervising.schemas.border import BorderVerdictSchema

log = logging.getLogger(__name__)

DEFAULT_MAX_REPAIRS = 3

RepairFn = Callable[[str, list[dict[str, Any]]], str]


def failing_findings(verdict: BorderVerdict) -> list[dict[str, Any]]:
    return [item for item in verdict.findings if item.get("decision") == "fail"]


def scrub_failed_originals(
    text: str,
    findings: list[dict[str, Any]],
    alias_map: AliasMap,
) -> str:
    """Deterministic first aid: replace every failed original before the LLM rewrites.

    Known component aliases win. Unaliased originals (paths, Makefile, …) get a neutral
    noun so the model is not asked to invent a substitute while the leak is still visible.
    """
    body = strip_border_review_section(text)
    body = scrub_identifiers(body, alias_map)

    replacements: list[tuple[str, str]] = []
    for item in findings:
        original = item.get("original")
        if not isinstance(original, str) or not original.strip():
            continue
        alias = item.get("alias")
        if isinstance(alias, str) and alias.strip():
            replacement = alias
        else:
            replacement = _neutral_noun(original, str(item.get("kind") or ""))
        replacements.append((original, replacement))

    for original, replacement in sorted(replacements, key=lambda pair: len(pair[0]), reverse=True):
        if re.search(r"\W", original):
            body = body.replace(original, replacement)
        else:
            body = re.sub(rf"\b{re.escape(original)}\b", replacement, body)
    return body


def _neutral_noun(original: str, kind: str) -> str:
    lowered = original.lower()
    if "makefile" in lowered or lowered.endswith(".toml") or "dockerfile" in lowered:
        return "the build configuration"
    if kind in {"URL", "file:line reference", "source-like path"} or "/" in original or "\\" in original:
        return "a source location"
    if kind.startswith("command") or kind.startswith("verbatim"):
        return "the documented procedure"
    return "the referenced element"


def gate_with_repairs(
    *,
    border: BorderGateAgent,
    repair: RepairFn,
    storage: Storage,
    alias_map: AliasMap,
    specification: str,
    max_repairs: int = DEFAULT_MAX_REPAIRS,
    documentation_report: str | dict[str, Any] | None = None,
    code_facts_report: str | dict[str, Any] | None = None,
    behavior_report: str | dict[str, Any] | None = None,
    plans: dict[str, str] | None = None,
    evidence_catalogue: dict[str, Any] | str | None = None,
    neutral_manifest: dict[str, Any] | str | None = None,
    source_texts: tuple[str, ...] | list[str] | None = None,
    repo_local_path: str | None = None,
) -> tuple[str, BorderVerdict]:
    """Run Border; on fail, repair the spec and retry up to ``max_repairs`` times.

    Returns the final specification and the last verdict. Raises ``BorderGateError`` if
    still failing after the last attempt.
    """
    if max_repairs < 0:
        raise ValueError("max_repairs must be >= 0")

    current = specification
    last_verdict: BorderVerdict | None = None

    for attempt in range(max_repairs + 1):
        verdict = border.review(
            alias_map=alias_map,
            specification=current,
            documentation_report=documentation_report,
            code_facts_report=code_facts_report,
            behavior_report=behavior_report,
            plans=plans,
            evidence_catalogue=evidence_catalogue,
            neutral_manifest=neutral_manifest,
            source_texts=source_texts,
            repo_local_path=repo_local_path,
        )
        last_verdict = verdict
        _store_verdict(storage, verdict, attempt)

        if verdict.passed:
            storage.save_text("specification.md", current)
            log.info(
                "Border passed on attempt %d/%d",
                attempt + 1,
                max_repairs + 1,
            )
            return current, verdict

        failing = failing_findings(verdict)
        log.warning(
            "Border failed attempt %d/%d with %d finding(s); %s",
            attempt + 1,
            max_repairs + 1,
            len(failing),
            "repairing" if attempt < max_repairs else "no repairs left",
        )
        storage.save_text(f"specification.border-fail-{attempt + 1}.md", current)

        if attempt >= max_repairs:
            storage.save_text("specification.md", current)
            raise BorderGateError(verdict)

        current = repair(current, failing)
        storage.save_text(f"specification.border-repair-{attempt + 1}.md", current)
        storage.save_text("specification.md", current)

    assert last_verdict is not None
    raise BorderGateError(last_verdict)


def _store_verdict(storage: Storage, verdict: BorderVerdict, attempt: int) -> None:
    payload = verdict.to_dict()
    storage.save_artifact("border_verdict.json", payload, BorderVerdictSchema())
    storage.save_private("border_verdict.json", payload)
    storage.save_json(f"border_verdict.round-{attempt + 1}.json", payload)

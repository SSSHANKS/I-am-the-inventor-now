import inspect
import logging
from collections.abc import Callable
from typing import Any

from packages.modules.supervising.policies.base import BaseSupervisorPolicy
from packages.modules.supervising.schemas.validator import (
    get_validation_errors,
)
from packages.modules.supervising.utils.diagnostics.recorder import NullRecorder

log = logging.getLogger(__name__)


type RepairCallback = Callable[..., str]


def _accepts_attempt(callback: Callable[..., str]) -> bool:
    """Whether `callback` can be told which retry this is.

    Checked once, by signature, rather than by catching TypeError at call time - that
    would silently swallow a genuine TypeError raised inside the callback.
    """
    try:
        return "attempt" in inspect.signature(callback).parameters
    except (TypeError, ValueError):
        return False


class Supervisor:
    """
    Shared schema + verifier repair loop for agent outputs.

    The supervisor owns the retry loop. A policy owns domain-specific behavior.
    The repair callback is the only model-specific dependency: it receives the
    current message list and must return the model's next content string.
    """

    def __init__(
        self,
        repair_callback: RepairCallback,
        max_validation_retries: int = 3,
    ):
        self.repair_callback = repair_callback
        self.max_validation_retries = max_validation_retries
        self._callback_takes_attempt = _accepts_attempt(repair_callback)

    def supervise(
        self,
        content: str,
        messages: list[dict[str, Any]],
        policy: BaseSupervisorPolicy,
        context: dict[str, Any] | None = None,
        recorder: Any | None = None,
        base_len: int | None = None,
    ) -> str:
        context = context or {}
        recorder = recorder or NullRecorder()
        base_len = len(messages) if base_len is None else base_len

        for retry in range(self.max_validation_retries + 1):
            model_output = content

            # First validation. Lets fix deterministic issues first, before doing any validation
            autofix_result = policy.autofix(model_output, context)
            candidate = autofix_result.content
            repair_content = autofix_result.repair_content or model_output

            if autofix_result.fixes:
                log.info(
                    "%s autofix applied %d fix(es)",
                    policy.name,
                    len(autofix_result.fixes),
                )
                recorder.record(
                    retry=retry,
                    phase="autofix",
                    status="applied",
                    content=candidate,
                    extra={"applied_fixes": autofix_result.fixes},
                )

            # Second validation. If the policy has a schema, validate against it first and repair if needed before doing any further verification.
            schema = policy.schema(context)
            if schema is not None:
                schema_errors = get_validation_errors(candidate, schema)
                if schema_errors is not None:
                    recorder.record(
                        retry=retry,
                        phase="schema",
                        status="fail",
                        content=candidate,
                        errors=schema_errors,
                    )
                    if retry >= self.max_validation_retries:
                        log.error(
                            "%s schema validation failed after %d retries.",
                            policy.name,
                            self.max_validation_retries,
                        )
                        policy.raise_schema_error(schema_errors, candidate, context)

                    log.warning(
                        "%s schema validation failed (retry %d/%d)",
                        policy.name,
                        retry + 1,
                        self.max_validation_retries,
                    )
                    content = self._repair_turn(
                        messages=messages,
                        base_len=base_len,
                        last_content=repair_content,
                        correction_prompt=policy.build_schema_repair_prompt(
                            schema_errors,
                            context,
                        ),
                        attempt=retry + 1,
                    )
                    continue

            # Third validation. Policy-specific verification, we need to verifi if fields have  correct values, not just if they are present.
            verification = policy.verify(candidate, context)
            if verification is None:
                recorder.record(
                    retry=retry,
                    phase="schema",
                    status="pass",
                    content=candidate,
                )
                return candidate

            issues = verification.get("issues") or []
            blocking = policy.blocking_issues(verification, context)

            if not blocking:
                recorder.record(
                    retry=retry,
                    phase=policy.verification_phase,
                    status="pass",
                    content=candidate,
                    issues=issues,
                )
                if issues:
                    log.info(
                        "%s verification passed with %d non-blocking warning(s)",
                        policy.name,
                        len(issues),
                    )
                return candidate

            recorder.record(
                retry=retry,
                phase=policy.verification_phase,
                status="fail",
                content=candidate,
                issues=issues,
            )

            if retry >= self.max_validation_retries:
                log.error(
                    "%s verification failed after %d retries.",
                    policy.name,
                    self.max_validation_retries,
                )
                policy.raise_verification_error(issues, candidate, context)

            log.warning(
                "%s verification failed (retry %d/%d) -> %d blocking issue(s)",
                policy.name,
                retry + 1,
                self.max_validation_retries,
                len(blocking),
            )
            content = self._repair_turn(
                messages=messages,
                base_len=base_len,
                last_content=repair_content,
                correction_prompt=policy.build_verification_repair_prompt(
                    issues,
                    context,
                ),
                attempt=retry + 1,
            )

        log.critical(
            "%s supervision failed after %d retries. This should not happen.",
            policy.name,
            self.max_validation_retries,
        )
        raise RuntimeError(f"{policy.name} supervision failed unexpectedly.")

    def _repair_turn(
        self,
        messages: list[dict[str, Any]],
        base_len: int,
        last_content: str,
        correction_prompt: str,
        attempt: int = 1,
    ) -> str:
        """Append one assistant-output + repair-instruction pair and ask for a rewrite.

        `attempt` is forwarded so the model layer can warm the temperature. Without it a
        deterministic model answers every repair turn with the identical invalid output,
        which makes the whole retry budget useless.
        """
        while len(messages) > base_len:
            messages.pop()
        messages.append({"role": "assistant", "content": last_content})
        messages.append({"role": "user", "content": correction_prompt})
        if self._callback_takes_attempt:
            return self.repair_callback(messages, attempt=attempt)
        return self.repair_callback(messages)

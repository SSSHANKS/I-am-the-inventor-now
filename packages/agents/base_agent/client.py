"""Model access for every agent.

All model calls go through **AgentProvider** (CLAUDE.md section 4). There are no
provider SDK calls in this repo - the legacy direct-to-Ollama HTTP client is gone.

AgentProvider is `generate(prompt, *, system=None, model=None) -> str`. There is no
message list and no tool calling, so a conversation is split into its standing system
instruction plus a rendered prompt, and `ChatResponse` never carries tool calls.

Request pacing AND retrying are AgentProvider's job, configured through the
`rate_limit` block in config/model_profiles/*.json. IATIN implements neither. There is
exactly one retry layer and it is not this one: AgentProvider re-acquires its window
before every attempt and waits as long as the server's `retryDelay` asks. A second
ladder here would retry underneath its own accounting and put untracked requests on
the wire - the very fault that made rate limiting fail before.
"""

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

ROLE_HEADINGS = {
    "system": "INSTRUCTIONS",
    "user": "TASK",
    "assistant": "YOUR PREVIOUS ANSWER",
    "tool": "TOOL RESULT",
}


#: Failures no retry can fix: the credential or the model name is wrong. Reported as
#: fatal so a caller stops rather than burning quota AgentProvider is trying to protect.
FATAL_KINDS = frozenset({"auth", "not_found"})


class ModelCallError(Exception):
    """A prompt did not become text.

    Carries AgentProvider's classification so callers can route the failure without
    importing litellm:

    - ``kind`` - one of auth / not_found / rate_limit / server / other.
    - ``status_code`` - the provider's HTTP status, when there was one.
    - ``fatal`` - True when retrying cannot possibly help.

    The supervisor's retry budget is for outputs that failed *validation*. A wrong API
    key is not a validation problem, and spending six repair turns on it just wastes
    time and quota.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str = "other",
        status_code: int | None = None,
    ):
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code

    @property
    def fatal(self) -> bool:
        return self.kind in FATAL_KINDS


@dataclass(frozen=True)
class ChatResponse:
    """A single model turn, normalised."""

    content: str

    @property
    def message(self) -> dict[str, Any]:
        return {"role": "assistant", "content": self.content}

    @property
    def tool_calls(self) -> list[dict[str, Any]]:
        """Always empty: AgentProvider cannot return tool calls."""
        return []


class ChatClient(Protocol):
    """Anything an agent can talk to."""

    def chat(
        self,
        messages: Sequence[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResponse: ...


class BaseChatClient:
    """Shared conversation -> prompt -> conversation-turn plumbing."""

    def generate(
        self, prompt: str, system: str | None = None, attempt: int = 0
    ) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def chat(
        self,
        messages: Sequence[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        attempt: int = 0,
    ) -> ChatResponse:
        if tools:
            log.debug(
                "Tool declarations ignored: AgentProvider is prompt to text only (%d dropped)",
                len(tools),
            )
        system, prompt = split_system(messages)
        return ChatResponse(content=self.generate(prompt, system=system, attempt=attempt))


class AgentProviderClient(BaseChatClient):
    """Routes prompts through AgentProvider.

    `profile_path` selects an AgentProvider config file, which is the only way to set
    temperature, timeouts, or JSON mode - `generate()` takes no per-call options.
    See config/model_profiles/README.md.

    `retry_profile_path` is used from the second attempt onwards. A repair loop at
    temperature 0 is unable to escape a deterministic mistake - a verification run
    produced byte-identical output on all 7 attempts and then failed the stage - so
    retries move to a warmer profile. One provider instance is held per profile,
    because temperature can only be set through the config file.
    """

    def __init__(
        self,
        model: str,
        profile_path: str | Path | None = None,
        retry_profile_path: str | Path | None = None,
    ):
        self.model = model
        self.profile_path = Path(profile_path) if profile_path else None
        self.retry_profile_path = Path(retry_profile_path) if retry_profile_path else None
        self._providers: dict[str, Any] = {}

    def _provider_for(self, attempt: int) -> Any:
        path = self.profile_path
        if attempt > 0 and self.retry_profile_path is not None:
            path = self.retry_profile_path
        key = str(path)
        if key in self._providers:
            return self._providers[key]
        try:
            from agent_provider import AgentProvider
        except ImportError as exc:
            raise ModelCallError(
                "AgentProvider is not installed. Install it into this .venv with "
                "`pip install -e ../IATIN_AGENT_PROVIDER`, or run with StubTextClient."
            ) from exc
        self._providers[key] = AgentProvider(model=self.model, config_path=path)
        return self._providers[key]

    def generate(self, prompt: str, system: str | None = None, attempt: int = 0) -> str:
        provider = self._provider_for(attempt)
        from agent_provider import AgentProviderError

        if attempt > 0 and self.retry_profile_path is not None:
            log.info("Retry %d for %s using warmer profile", attempt, self.model)

        # One attempt. A rate limit that reaches here has already been retried by
        # AgentProvider for as long as the server asked, so trying again would only
        # spend quota on a wait we already know was not enough.
        try:
            return provider.generate(prompt, system=system)
        except AgentProviderError as exc:
            kind = getattr(exc, "kind", "other")
            status = getattr(exc, "status_code", None)
            if kind in FATAL_KINDS:
                log.error("Model call failed fatally (%s, status=%s)", kind, status)
            else:
                log.warning("Model call failed (%s, status=%s)", kind, status)
            raise ModelCallError(
                f"Model call failed for {self.model!r} ({kind}): {exc}",
                kind=kind,
                status_code=status,
            ) from exc


class StubTextClient(BaseChatClient):
    """Canned replies for tests. Makes no network call and costs nothing.

    Pass a list to replay responses in order, or a callable to compute one from the
    rendered prompt.
    """

    def __init__(self, responses: Sequence[str] | Callable[[str], str]):
        self._responses = responses
        self._index = 0
        self.prompts: list[str] = []
        self.systems: list[str | None] = []
        self.attempts: list[int] = []

    def generate(self, prompt: str, system: str | None = None, attempt: int = 0) -> str:
        self.prompts.append(prompt)
        self.systems.append(system)
        self.attempts.append(attempt)
        if callable(self._responses):
            return self._responses(prompt)
        if self._index >= len(self._responses):
            raise ModelCallError(
                f"StubTextClient ran out of responses after {self._index} call(s). "
                "The code under test made more model calls than the test scripted."
            )
        reply = self._responses[self._index]
        self._index += 1
        return reply

    @property
    def call_count(self) -> int:
        return len(self.prompts)


def split_system(messages: Sequence[dict[str, Any]]) -> tuple[str | None, str]:
    """Separate the system instruction from the rest of the conversation.

    AgentProvider now takes `system=` directly, so the standing instruction no longer
    has to be flattened into the user prompt. Gemini 3 also wants sampling guidance in
    the system message rather than in `temperature`, which is on litellm's deprecation
    path.
    """
    system_parts = [
        str(m.get("content", "")).strip()
        for m in messages
        if m.get("role") == "system" and str(m.get("content", "")).strip()
    ]
    rest = [m for m in messages if m.get("role") != "system"]
    return ("\n\n".join(system_parts) or None), render_conversation(rest)


def render_conversation(messages: Sequence[dict[str, Any]]) -> str:
    """Flatten a role-tagged conversation into one prompt.

    Roles become headings so the model can still tell an instruction from a task from
    its own rejected previous answer. This is what a repair turn depends on: the model
    has to see what it said before and why that was refused.
    """
    blocks: list[str] = []
    for message in messages:
        role = str(message.get("role", "user"))
        content = message.get("content", "")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        if not content.strip():
            continue
        blocks.append(f"### {ROLE_HEADINGS.get(role, role.upper())}\n{content.strip()}")
    return "\n\n".join(blocks)


def build_client(
    model: str,
    profile_path: str | Path | None = None,
    retry_profile_path: str | Path | None = None,
    client: Any | None = None,
) -> BaseChatClient:
    """Return the injected client if there is one, else a real AgentProvider client."""
    if client is not None:
        return client if isinstance(client, BaseChatClient) else _Adapter(client)
    return AgentProviderClient(
        model=model,
        profile_path=profile_path,
        retry_profile_path=retry_profile_path,
    )


class _Adapter(BaseChatClient):
    """Wraps a bare prompt->text callable object so it also speaks `chat`."""

    def __init__(self, inner: Any):
        self._inner = inner

    def generate(self, prompt: str, system: str | None = None, attempt: int = 0) -> str:
        return self._inner.generate(prompt)

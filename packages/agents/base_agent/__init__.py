"""Agent foundation: the thick base plus the model transport it speaks through."""

from packages.agents.base_agent.base import (
    DEFAULT_MAX_VALIDATION_RETRIES,
    AgentConfig,
    BaseAgent,
)
from packages.agents.base_agent.client import (
    AgentProviderClient,
    ChatClient,
    ChatResponse,
    ModelCallError,
    StubTextClient,
    build_client,
    render_conversation,
)

__all__ = [
    "DEFAULT_MAX_VALIDATION_RETRIES",
    "AgentConfig",
    "AgentProviderClient",
    "BaseAgent",
    "ChatClient",
    "ChatResponse",
    "ModelCallError",
    "StubTextClient",
    "build_client",
    "render_conversation",
]
